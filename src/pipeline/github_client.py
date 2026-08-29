"""Injected, guarded GitHub artifact transport."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from pipeline.config import CiEvidenceMode, IssueSink, Mode, PipelineConfig
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import Candidate, ReasonCode, Tier

REQUIRED_CONTEXTS = (
    "lint-check",
    "pre-commit (current)",
    "unit-tests-required",
    "test-postgres-required",
    "test-sqlite",
    "test-mysql",
    "test-postgres-hive",
    "test-postgres-presto",
    "frontend-build",
    "cypress-matrix-required",
    "playwright-tests-required",
    "dependency-review",
    "enforce-single-migration-head",
)


class PreflightError(RuntimeError):
    """Raised when a blocking LIVE capability precondition is unmet."""

    def __init__(self, reason: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class LivePreflight:
    """Read-only capability result used to configure a LIVE run."""

    has_issues: bool
    code_scanning_available: bool
    ci_evidence_mode: CiEvidenceMode
    token_login: str
    token_scopes: tuple[str, ...]
    notes: tuple[str, ...]
    code_scanning_alerts: object | None


@dataclass(frozen=True)
class CiModeTransition:
    """One-way local-to-GitHub CI evidence transition."""

    mode: CiEvidenceMode
    transitioned: bool
    reason: ReasonCode | None = None


@dataclass(frozen=True)
class CiWaitResult:
    """Result of waiting for required contexts on a generated PR."""

    mode: CiEvidenceMode
    reason: ReasonCode | None
    auto_merge_eligible: bool
    detail: str | None = None


class GitHubTransport(Protocol):
    """Minimal transport required for GitHub writes."""

    @property
    def response_headers(self) -> Mapping[str, str]:
        """Return non-sensitive headers from the latest response."""

    def get(self, path: str) -> object:
        """Read a GitHub resource."""

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Create an issue, pull request, comment, or label."""

    def patch(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Patch an existing issue or pull request."""


def _mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PreflightError(ReasonCode.CAPABILITY_UNAVAILABLE, f"{description} response invalid")
    return {str(key): item for key, item in value.items()}


def _workflow_count(value: object) -> int:
    data = _mapping(value, "workflow listing")
    total = data.get("total_count")
    return total if isinstance(total, int) else 0


def _has_completed_run(value: object) -> bool:
    data = _mapping(value, "workflow history")
    runs = data.get("workflow_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("status") == "completed"
        and isinstance(run.get("event"), str)
        for run in runs
    )


def _path(config: PipelineConfig, suffix: str) -> str:
    return f"/repos/{config.target_owner}/{config.target_repo}{suffix}"


def run_live_preflight(config: PipelineConfig, transport: GitHubTransport) -> LivePreflight:
    """Probe repository capabilities before any candidate work or mutation."""
    try:
        repository = _mapping(transport.get(_path(config, "")), "repository")
    except HttpTransportError as exc:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read target repository capabilities",
        ) from exc

    has_issues = repository.get("has_issues")
    if not isinstance(has_issues, bool):
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "repository has_issues is unavailable",
        )
    if not has_issues and config.issue_sink is not IssueSink.PR_COMMENT:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "target repository has issues disabled; use issue_sink=pr_comment",
        )

    code_scanning_available = True
    code_scanning_alerts: object | None = None
    notes: list[str] = []
    try:
        code_scanning_alerts = transport.get(_path(config, "/code-scanning/alerts"))
    except HttpTransportError as exc:
        if exc.status_code == 403:
            raise PreflightError(
                ReasonCode.TOKEN_CAPABILITY_MISSING,
                "token cannot read code-scanning alerts",
            ) from exc
        if exc.status_code == 404:
            code_scanning_available = False
            notes.append("code_scanning: capability_unavailable")
        else:
            raise PreflightError(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "cannot read code-scanning alerts",
            ) from exc

    try:
        identity = _mapping(transport.get("/user"), "token identity")
        response_headers = transport.response_headers
        workflows = transport.get(_path(config, "/actions/workflows"))
        pull_request_runs = transport.get(
            _path(
                config,
                "/actions/runs?"
                + urlencode({"event": "pull_request", "status": "completed", "per_page": "1"}),
            )
        )
        dispatch_runs = transport.get(
            _path(
                config,
                "/actions/runs?"
                + urlencode({"event": "workflow_dispatch", "status": "completed", "per_page": "1"}),
            )
        )
    except HttpTransportError as exc:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read Actions capability history",
        ) from exc

    login = identity.get("login")
    if not isinstance(login, str) or not login:
        raise PreflightError(ReasonCode.TOKEN_CAPABILITY_MISSING, "token identity is unavailable")
    scopes = tuple(
        scope.strip()
        for key, value in response_headers.items()
        if key.casefold() == "x-oauth-scopes"
        for scope in value.split(",")
        if scope.strip()
    )
    has_completed_actions = _workflow_count(workflows) > 0 and (
        _has_completed_run(pull_request_runs) or _has_completed_run(dispatch_runs)
    )
    ci_mode = CiEvidenceMode.GITHUB if has_completed_actions else CiEvidenceMode.LOCAL
    if ci_mode is CiEvidenceMode.LOCAL:
        notes.append("ci_evidence_mode: local (no completed pull_request/workflow_dispatch run)")
    else:
        notes.append("ci_evidence_mode: github (completed Actions history observed)")
    if not has_issues:
        notes.append("artifact_degraded: issues disabled; PR comments selected")
    return LivePreflight(
        has_issues=has_issues,
        code_scanning_available=code_scanning_available,
        ci_evidence_mode=ci_mode,
        token_login=login,
        token_scopes=scopes,
        notes=tuple(notes),
        code_scanning_alerts=code_scanning_alerts,
    )


def _required_context_statuses(
    config: PipelineConfig,
    client: GitHubTransport,
    sha: str,
) -> dict[str, str]:
    """Read check-run conclusions and legacy commit-status states for one SHA."""
    root = _path(config, f"/commits/{sha}")
    checks = _mapping(client.get(f"{root}/check-runs"), "check-runs")
    statuses = _mapping(client.get(f"{root}/status"), "status")
    result: dict[str, str] = {}
    raw_checks = checks.get("check_runs")
    if isinstance(raw_checks, list):
        for raw_check in raw_checks:
            if not isinstance(raw_check, Mapping):
                continue
            name = raw_check.get("name")
            conclusion = raw_check.get("conclusion")
            status = raw_check.get("status")
            if isinstance(name, str):
                if isinstance(conclusion, str):
                    result[name] = conclusion
                elif isinstance(status, str):
                    result[name] = status
    raw_statuses = statuses.get("statuses")
    if isinstance(raw_statuses, list):
        for raw_status in raw_statuses:
            if not isinstance(raw_status, Mapping):
                continue
            context = raw_status.get("context")
            state = raw_status.get("state")
            if isinstance(context, str) and isinstance(state, str) and context not in result:
                result[context] = state
    return result


def maybe_upgrade_ci_mode(
    current: CiEvidenceMode,
    *,
    reported_contexts: Sequence[str],
    awaiting_workflow_approval: bool = False,
    already_upgraded: bool = False,
) -> CiModeTransition:
    """Perform the one-way upgrade only from an observed required context."""
    if (
        current is CiEvidenceMode.LOCAL
        and not already_upgraded
        and not awaiting_workflow_approval
        and any(context in REQUIRED_CONTEXTS for context in reported_contexts)
    ):
        return CiModeTransition(CiEvidenceMode.GITHUB, True)
    if awaiting_workflow_approval:
        return CiModeTransition(CiEvidenceMode.LOCAL, False, ReasonCode.AWAITING_WORKFLOW_APPROVAL)
    return CiModeTransition(current, False)


def wait_for_required_contexts(
    config: PipelineConfig,
    *,
    client: GitHubTransport,
    elapsed_s: int,
    reported_contexts: Mapping[str, str] | None = None,
    sha: str = "HEAD",
    poll: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_interval_s: float = 15.0,
    on_mode_transition: Callable[[CiModeTransition], None] | None = None,
    ci_mode: CiEvidenceMode | None = None,
    is_fork: bool = False,
) -> CiWaitResult:
    """Resolve generated-PR CI evidence without treating missing reports as green."""
    started = clock()
    deadline = started + max(config.ci_wait_timeout_s - elapsed_s, 0)
    current_mode = ci_mode or config.ci_evidence_mode
    already_upgraded = current_mode is CiEvidenceMode.GITHUB
    while True:
        statuses = (
            _required_context_statuses(config, client, sha)
            if reported_contexts is None
            else dict(reported_contexts)
        )
        complete = all(statuses.get(context) == "success" for context in REQUIRED_CONTEXTS)
        awaiting_approval = any(
            statuses.get(context) in {"action_required", "awaiting_approval"}
            for context in REQUIRED_CONTEXTS
        )
        transition = maybe_upgrade_ci_mode(
            current_mode,
            reported_contexts=tuple(statuses),
            awaiting_workflow_approval=awaiting_approval,
            already_upgraded=already_upgraded,
        )
        if transition.transitioned:
            current_mode = transition.mode
            already_upgraded = True
            if on_mode_transition is not None:
                on_mode_transition(transition)
        if complete:
            return CiWaitResult(current_mode, None, config.auto_merge_enabled)
        if any(
            statuses.get(context) in {"failure", "cancelled", "timed_out", "error"}
            for context in REQUIRED_CONTEXTS
        ):
            return CiWaitResult(current_mode, ReasonCode.CI_CHECK_FAILED, False)
        if awaiting_approval:
            return CiWaitResult(
                current_mode,
                ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                False,
                "awaiting_workflow_approval",
            )
        if (
            is_fork
            and not any(context in statuses for context in REQUIRED_CONTEXTS)
            and clock() - started >= poll_interval_s
        ):
            return CiWaitResult(
                current_mode,
                ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                False,
                "awaiting_workflow_approval",
            )
        if not poll:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll_interval_s, remaining))
    return CiWaitResult(current_mode, ReasonCode.CI_EVIDENCE_UNAVAILABLE, False)


class SimulationWriteError(RuntimeError):
    """Raised if a SIMULATE path attempts a remote mutation."""


class ArtifactUnavailableError(RuntimeError):
    """Raised when the configured GitHub artifact sink is unavailable."""


class GitHubResponseError(ArtifactUnavailableError):
    """Raised when GitHub returns a structurally invalid response."""


class ClosedPullRequestError(ArtifactUnavailableError):
    """Raised when only a closed PR exists for a candidate branch."""

    def __init__(self, head: str, match: PullRequestMatch | None = None) -> None:
        super().__init__(f"only a closed pull request exists for head {head}")
        self.match = match


class MergedPullRequestError(ArtifactUnavailableError):
    """Raised when an existing branch PR already merged successfully."""

    def __init__(self, match: PullRequestMatch) -> None:
        super().__init__("pull request already merged")
        self.match = match


@dataclass(frozen=True)
class ArtifactLinks:
    """Links created by the mandated issue/PR ordering."""

    issue_url: str | None
    pr_url: str | None
    comment_url: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    merged_at: str | None = None
    auto_merge_requested: bool = False


@dataclass(frozen=True)
class PullRequestMatch:
    """Existing pull-request identity and terminal status for one branch."""

    number: int
    url: str
    state: str
    merged_at: str | None = None


class GitHubClient:
    """Perform guarded GitHub writes through one injectable transport."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        transport: GitHubTransport | None = None,
        write_guard: Callable[[], None] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._write_guard = write_guard
        self._ensured_labels: dict[str, bool] = {}
        if config.mode is Mode.LIVE and transport is None:
            raise ValueError("live GitHub writes require a transport")

    def _write(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Issue one bounded, server-timed write request."""
        if self._config.mode is Mode.SIMULATE:
            raise SimulationWriteError("SIMULATE forbids GitHub writes")
        if self._transport is None:
            raise GitHubResponseError("GitHub transport is unavailable")
        operation = self._transport.post if method == "post" else self._transport.patch
        if self._write_guard is not None:
            self._write_guard()
        return operation(path, payload)

    def _read(self, path: str) -> object:
        """Issue one guarded read request through the configured transport."""
        if self._transport is None:
            raise GitHubResponseError("GitHub transport is unavailable")
        return self._transport.get(path)

    @staticmethod
    def _url(response: Mapping[str, object]) -> str:
        """Extract the canonical URL from a GitHub mutation response."""
        value = response.get("html_url", response.get("url"))
        if not isinstance(value, str) or not value:
            raise GitHubResponseError("GitHub response lacks html_url")
        return value

    @staticmethod
    def _number(response: Mapping[str, object]) -> int:
        """Extract an issue or pull-request number."""
        value = response.get("number")
        if not isinstance(value, int):
            raise GitHubResponseError("GitHub response lacks number")
        return value

    def create_issue(self, title: str, body: str, labels: Sequence[str]) -> tuple[int, str]:
        """Create the companion issue before any pull request."""
        response = self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/issues",
            {"title": title, "body": body, "labels": list(labels)},
        )
        return self._number(response), self._url(response)

    def create_branch(self, branch: str, base_sha: str) -> None:
        """Create a candidate branch from the pinned target base SHA."""
        self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": base_sha},
        )

    def branch_sha(self, branch: str) -> str | None:
        """Read the current SHA for a candidate branch."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/git/ref/heads/{branch}"
        )
        if isinstance(response, Mapping):
            obj = response.get("object")
            if isinstance(obj, Mapping) and isinstance(obj.get("sha"), str):
                return str(obj["sha"])
        return None

    def pull_request_head_sha(self, number: int) -> str | None:
        """Read the actual head SHA from a pull request."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}"
        )
        if isinstance(response, Mapping):
            head = response.get("head")
            if isinstance(head, Mapping) and isinstance(head.get("sha"), str):
                return str(head["sha"])
        return None

    def pull_request_head_metadata(self, number: int) -> tuple[str | None, bool]:
        """Read the current head SHA and whether the PR originates from a fork."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}"
        )
        if not isinstance(response, Mapping):
            return None, False
        head = response.get("head")
        if not isinstance(head, Mapping):
            return None, False
        sha = head.get("sha")
        repository = head.get("repo")
        full_name = repository.get("full_name") if isinstance(repository, Mapping) else None
        is_fork = isinstance(full_name, str) and full_name != (
            f"{self._config.target_owner}/{self._config.target_repo}"
        )
        return (sha if isinstance(sha, str) else None), is_fork

    def commit_messages_between(self, base_sha: str, head_sha: str) -> list[str]:
        """Read every commit message introduced between two recorded SHAs."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/compare/"
            f"{base_sha}...{head_sha}"
        )
        if not isinstance(response, Mapping):
            return []
        commits = response.get("commits")
        if not isinstance(commits, list):
            return []
        messages: list[str] = []
        for item in commits:
            if not isinstance(item, Mapping):
                continue
            commit = item.get("commit")
            if isinstance(commit, Mapping) and isinstance(commit.get("message"), str):
                messages.append(str(commit["message"]))
        return messages

    def patch_issue(self, number: int, body: str) -> str:
        """Patch an issue after its linked PR exists."""
        response = self._write(
            "patch",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/issues/{number}",
            {"body": body},
        )
        return self._url(response)

    def create_pr(
        self,
        title: str,
        body: str,
        *,
        head: str,
        base: str,
    ) -> tuple[int, str]:
        """Create a PR after its companion issue."""
        response = self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls",
            {"title": title, "body": body, "head": head, "base": base},
        )
        return self._number(response), self._url(response)

    def pull_request_for_head(self, head: str) -> PullRequestMatch | None:
        """Find one existing pull request and classify its lifecycle state."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls?"
            + urlencode({"head": f"{self._config.target_owner}:{head}", "state": "all"})
        )
        if not isinstance(response, list):
            return None
        closed: PullRequestMatch | None = None
        merged: PullRequestMatch | None = None
        for item in response:
            if isinstance(item, Mapping):
                number = item.get("number")
                url = item.get("html_url")
                state = item.get("state")
                merged_at = item.get("merged_at")
                if isinstance(number, int) and isinstance(url, str) and isinstance(state, str):
                    match = PullRequestMatch(
                        number,
                        url,
                        state,
                        merged_at if isinstance(merged_at, str) else None,
                    )
                    if state == "open":
                        return match
                    if match.merged_at is not None:
                        merged = match
                    else:
                        closed = match
        return merged or closed

    def pull_request(self, number: int) -> PullRequestMatch | None:
        """Read one PR's current lifecycle state."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}"
        )
        if not isinstance(response, Mapping):
            return None
        url = response.get("html_url")
        state = response.get("state")
        merged_at = response.get("merged_at")
        if not isinstance(url, str) or not isinstance(state, str):
            return None
        return PullRequestMatch(
            number,
            url,
            state,
            merged_at if isinstance(merged_at, str) else None,
        )

    def patch_pr_body(self, number: int, body: str) -> None:
        """Update a PR body after retrieving its commit provenance."""
        self._write(
            "patch",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}",
            {"body": body},
        )

    def comment_pr(self, number: int, body: str) -> str:
        """Create a manager-facing degraded-path PR comment."""
        response = self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/issues/{number}/comments",
            {"body": body},
        )
        return self._url(response)

    def add_labels(self, number: int, labels: Sequence[str]) -> None:
        """Apply lifecycle labels to an issue or pull request."""
        self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/issues/{number}/labels",
            {"labels": list(labels)},
        )

    def ensure_label(self, label: str) -> bool:
        """Ensure a repository label exists, returning false on denied creation."""
        if label in self._ensured_labels:
            return self._ensured_labels[label]
        path = f"/repos/{self._config.target_owner}/{self._config.target_repo}/labels/{label}"
        try:
            self._read(path)
            self._ensured_labels[label] = True
            return True
        except HttpTransportError as exc:
            if exc.status_code != 404:
                self._ensured_labels[label] = False
                return False
        try:
            self._write(
                "post",
                f"/repos/{self._config.target_owner}/{self._config.target_repo}/labels",
                {"name": label},
            )
        except HttpTransportError:
            self._ensured_labels[label] = False
            return False
        self._ensured_labels[label] = True
        return True

    def enable_auto_merge(self, number: int, *, ci_mode: CiEvidenceMode) -> None:
        """Request auto-merge only after the caller has checked all gates."""
        if not self._config.auto_merge_enabled or ci_mode is not CiEvidenceMode.GITHUB:
            raise ValueError("auto-merge requires enabled GitHub CI evidence")
        self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}/auto-merge",
            {"merge_method": "squash"},
        )


def publish_artifacts(
    client: GitHubClient,
    candidate: Candidate,
    *,
    issue_title: str,
    issue_body: str,
    pr_title: str | None = None,
    pr_body: str | None = None,
    head: str | None = None,
    base: str = "master",
    labels: Sequence[str] = (),
    preflight: Callable[[], None] | None = None,
    ci_probe: Callable[[int], CiWaitResult] | None = None,
    existing_issue_number: int | None = None,
    existing_issue_url: str | None = None,
    existing_pr_number: int | None = None,
    existing_pr_url: str | None = None,
    after_issue: Callable[[int, str], None] | None = None,
    after_pr_created: Callable[[int, str], None] | None = None,
    after_ci: Callable[[int], None] | None = None,
    after_issue_patched: Callable[[str], None] | None = None,
) -> ArtifactLinks:
    """Publish artifacts in the mandated issue → PR → issue-patch order."""
    if not client._config.has_issues:
        if client._config.issue_sink is not IssueSink.PR_COMMENT:
            raise ArtifactUnavailableError("GitHub issues are unavailable")
        raise ArtifactUnavailableError("use publish_degraded for PR comments")
    if preflight is not None:
        preflight()
    issue_url: str | None
    if existing_issue_number is None:
        issue_number, issue_url = client.create_issue(issue_title, issue_body, labels)
    else:
        issue_number, issue_url = existing_issue_number, existing_issue_url
    if (
        after_issue is not None
        and existing_issue_number is None
        and issue_number is not None
        and issue_url is not None
    ):
        after_issue(issue_number, issue_url)
    if candidate.tier is Tier.MEDIUM or pr_title is None or pr_body is None or head is None:
        return ArtifactLinks(issue_url, None, issue_number=issue_number)
    if ci_probe is None:
        raise ValueError("PR publication requires an observed CI probe")
    if preflight is not None:
        preflight()
    created_pr = False
    if existing_pr_number is not None and existing_pr_url is not None:
        pr_number, pr_url = existing_pr_number, existing_pr_url
        existing_match = client.pull_request(pr_number)
        if existing_match is not None and existing_match.merged_at is not None:
            client.patch_issue(issue_number, f"{issue_body.rstrip()}\n\nPR: {pr_url}\n")
            if after_issue_patched is not None:
                after_issue_patched(pr_url)
            raise MergedPullRequestError(existing_match)
    else:
        try:
            pr_number, pr_url = client.create_pr(
                pr_title,
                f"{pr_body.rstrip()}\n\nCloses #{issue_number}\n",
                head=head,
                base=base,
            )
            created_pr = True
        except HttpTransportError as exc:
            if exc.status_code != 422:
                raise
            existing = client.pull_request_for_head(head)
            if existing is None:
                raise
            if existing.state == "open":
                pr_number, pr_url = existing.number, existing.url
            elif existing.merged_at is not None:
                client.patch_issue(
                    issue_number,
                    f"{issue_body.rstrip()}\n\nPR: {existing.url}\n",
                )
                if after_issue_patched is not None:
                    after_issue_patched(existing.url)
                raise MergedPullRequestError(existing) from exc
            else:
                raise ClosedPullRequestError(head, existing) from exc
    if after_pr_created is not None and created_pr:
        after_pr_created(pr_number, pr_url)
    ci_result = ci_probe(pr_number)
    if preflight is not None:
        preflight()
    if after_ci is not None:
        after_ci(pr_number)
    client.patch_issue(issue_number, f"{issue_body.rstrip()}\n\nPR: {pr_url}\n")
    if after_issue_patched is not None:
        after_issue_patched(pr_url)
    if (
        candidate.auto_merge_eligible
        and client._config.auto_merge_enabled
        and ci_result is not None
        and ci_result.mode is CiEvidenceMode.GITHUB
        and ci_result.auto_merge_eligible
        and ci_result.reason is None
        and (candidate.test_added is True or candidate.test_exempt_reason is not None)
    ):
        client.enable_auto_merge(pr_number, ci_mode=ci_result.mode)
        auto_merge_requested = True
    else:
        auto_merge_requested = False
    return ArtifactLinks(
        issue_url,
        pr_url,
        issue_number=issue_number,
        pr_number=pr_number,
        auto_merge_requested=auto_merge_requested,
    )


def publish_degraded(
    client: GitHubClient,
    candidate: Candidate,
    *,
    pr_title: str,
    pr_body: str,
    comment_body: str,
    head: str,
    base: str = "master",
    preflight: Callable[[], None] | None = None,
    after_pr_created: Callable[[int, str], None] | None = None,
    after_comment_created: Callable[[str], None] | None = None,
) -> ArtifactLinks:
    """Publish a PR and manager-facing comment when issues are disabled."""
    if client._config.has_issues or client._config.issue_sink is not IssueSink.PR_COMMENT:
        raise ArtifactUnavailableError("degraded sink requires has_issues=false and pr_comment")
    if preflight is not None:
        preflight()
    pr_number, pr_url = client.create_pr(
        pr_title,
        pr_body,
        head=head,
        base=base,
    )
    if after_pr_created is not None:
        after_pr_created(pr_number, pr_url)
    if preflight is not None:
        preflight()
    comment_url = client.comment_pr(pr_number, comment_body)
    if after_comment_created is not None:
        after_comment_created(comment_url)
    return ArtifactLinks(None, pr_url, comment_url, pr_number=pr_number)


__all__ = [
    "ArtifactLinks",
    "ArtifactUnavailableError",
    "CiModeTransition",
    "CiWaitResult",
    "ClosedPullRequestError",
    "GitHubClient",
    "GitHubResponseError",
    "GitHubTransport",
    "LivePreflight",
    "MergedPullRequestError",
    "PullRequestMatch",
    "PreflightError",
    "REQUIRED_CONTEXTS",
    "SimulationWriteError",
    "maybe_upgrade_ci_mode",
    "publish_artifacts",
    "publish_degraded",
    "run_live_preflight",
    "wait_for_required_contexts",
]
