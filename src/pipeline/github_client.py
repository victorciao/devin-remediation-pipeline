"""Injected, guarded GitHub artifact transport."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

from pipeline.config import CiEvidenceMode, Mode, PipelineConfig
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import Candidate, CheckRunConclusion, MergeMode, ReasonCode, Tier
from pipeline.verify import SuiteResult

ACCEPTED_CONCLUSIONS = frozenset({"success", "skipped", "neutral"})
REJECTED_CONCLUSIONS = frozenset({"failure", "cancelled", "timed_out", "stale", "error"})
PENDING_STATUSES = frozenset({"queued", "in_progress", "pending", "requested"})
APPROVAL_STATUSES = frozenset({"action_required", "awaiting_approval", "waiting"})
_PAGE_SIZE = 100
_MAX_PAGES = 10


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
    """Result of waiting for check-run evidence on a generated PR."""

    mode: CiEvidenceMode
    reason: ReasonCode | None
    auto_merge_eligible: bool
    detail: str | None = None
    conclusions: tuple[CheckRunConclusion, ...] = ()


@dataclass(frozen=True)
class CheckRunEvidence:
    """Settled-ness and verdict of the check runs observed for one SHA."""

    conclusions: tuple[CheckRunConclusion, ...]
    settled: bool
    passed: bool
    reason: ReasonCode | None = None
    detail: str | None = None


def _check_run_state(item: CheckRunConclusion) -> str | None:
    """Return the state a check run reports, from its conclusion or its status."""
    if item.conclusion:
        return item.conclusion
    status = item.status or ""
    return status or None


def evaluate_check_runs(
    conclusions: Sequence[CheckRunConclusion],
    *,
    required_contexts: Sequence[str],
) -> CheckRunEvidence:
    """Apply the §12 conclusion rule to observed check runs.

    ``success``, ``skipped`` and ``neutral`` are accepted; ``failure``,
    ``cancelled`` and ``timed_out`` are rejected; anything still queued leaves
    the evidence unsettled. Every configured required context must be present
    and successful before the evidence counts as passing.
    """
    observed = tuple(conclusions)
    if not observed:
        return CheckRunEvidence(
            observed,
            settled=False,
            passed=False,
            reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
        )
    by_name = {item.name: item for item in observed}
    states = {item.name: _check_run_state(item) for item in observed}
    if any(state in APPROVAL_STATUSES for state in states.values()):
        return CheckRunEvidence(
            observed,
            settled=False,
            passed=False,
            reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            detail="awaiting_workflow_approval",
        )
    rejected = [name for name, state in states.items() if state in REJECTED_CONCLUSIONS]
    if rejected:
        return CheckRunEvidence(
            observed,
            settled=True,
            passed=False,
            reason=ReasonCode.CI_CHECK_FAILED,
            detail=", ".join(sorted(rejected)),
        )
    unsettled = [
        name for name, state in states.items() if state is None or state in PENDING_STATUSES
    ]
    if unsettled:
        return CheckRunEvidence(
            observed,
            settled=False,
            passed=False,
            reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            detail=", ".join(sorted(unsettled)),
        )
    if any(state not in ACCEPTED_CONCLUSIONS for state in states.values()):
        return CheckRunEvidence(
            observed,
            settled=True,
            passed=False,
            reason=ReasonCode.CI_CHECK_FAILED,
        )
    missing = [
        context
        for context in required_contexts
        if context not in by_name or by_name[context].conclusion != "success"
    ]
    if missing:
        return CheckRunEvidence(
            observed,
            settled=True,
            passed=False,
            reason=ReasonCode.CI_CHECK_FAILED,
            detail="required contexts missing or unsuccessful: " + ", ".join(missing),
        )
    return CheckRunEvidence(observed, settled=True, passed=True)


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


def read_named_suite(
    config: PipelineConfig,
    client: GitHubTransport,
    sha: str,
) -> SuiteResult:
    """Read one named Actions check conclusion at a revision."""
    context = config.suite_check_context
    command = f"GET {_path(config, f'/commits/{sha}/check-runs')} context={context}"
    try:
        conclusions = read_check_runs(config, client, sha)
    except (ArtifactUnavailableError, HttpTransportError, PreflightError):
        return SuiteResult(
            passed=False,
            command=command,
            reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
        )
    observed = next((item for item in conclusions if item.name == context), None)
    if observed is None:
        return SuiteResult(
            passed=False,
            command=command,
            reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
        )
    conclusion = observed.conclusion or observed.status
    if conclusion in ACCEPTED_CONCLUSIONS:
        return SuiteResult(
            passed=True,
            command=command,
            conclusion=conclusion,
        )
    reason = (
        ReasonCode.CI_EVIDENCE_UNAVAILABLE
        if conclusion in PENDING_STATUSES or conclusion is None
        else ReasonCode.CI_CHECK_FAILED
    )
    return SuiteResult(
        passed=False,
        command=command,
        failing_nodeids=(context,),
        reason=reason,
        conclusion=conclusion,
    )


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


def _paged_items(
    client: GitHubTransport,
    path: str,
    key: str,
) -> list[object]:
    """Read a bounded GitHub collection, preserving server order across pages."""
    items: list[object] = []
    for page in range(1, _MAX_PAGES + 1):
        response = _mapping(
            client.get(f"{path}?per_page={_PAGE_SIZE}&page={page}"),
            key,
        )
        page_items = response.get(key)
        if not isinstance(page_items, list):
            raise PreflightError(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                f"{key} response invalid",
            )
        items.extend(page_items)
        if len(page_items) < _PAGE_SIZE:
            return items
    raise PreflightError(
        ReasonCode.CAPABILITY_UNAVAILABLE,
        f"{key} pagination exceeded {_MAX_PAGES} pages",
    )


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
    permissions = repository.get("permissions")
    if isinstance(permissions, Mapping) and permissions.get("push") is False:
        raise PreflightError(
            ReasonCode.TOKEN_CAPABILITY_MISSING,
            "token does not have push access to the target repository",
        )
    if not has_issues:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "target repository has issues disabled; the issue sink is unavailable",
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
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read code-scanning alerts",
        ) from exc

    try:
        base_ref = _mapping(
            transport.get(_path(config, "/git/ref/heads/master")),
            "base branch reference",
        )
        base_object = _mapping(base_ref.get("object"), "base branch object")
        base_sha = base_object.get("sha")
        analyses = transport.get(_path(config, "/code-scanning/analyses?ref=refs/heads/master"))
    except HttpTransportError as exc:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read CodeQL analysis freshness",
        ) from exc
    python_analyses: list[Mapping[str, object]] = []
    if isinstance(analyses, list):
        python_analyses = [
            analysis
            for analysis in analyses
            if isinstance(analysis, Mapping)
            and analysis.get("ref") in ("refs/heads/master", "master")
            and analysis.get("category") == "/language:python"
        ]
    if not python_analyses:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "target repository has no master Python CodeQL analysis",
        )
    latest_python = python_analyses[0]
    if latest_python.get("commit_sha") != base_sha:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "latest master Python CodeQL analysis is not fresh at the target base SHA",
        )

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
    has_completed_actions = _workflow_count(workflows) > 0 and _has_completed_run(pull_request_runs)
    if not has_completed_actions:
        raise PreflightError(
            ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            "target repository has no completed pull_request Actions run",
        )
    ci_mode = CiEvidenceMode.ACTIONS
    notes.append("ci_evidence_mode: actions (completed pull_request Actions history observed)")
    return LivePreflight(
        has_issues=has_issues,
        code_scanning_available=code_scanning_available,
        ci_evidence_mode=ci_mode,
        token_login=login,
        token_scopes=scopes,
        notes=tuple(notes),
        code_scanning_alerts=code_scanning_alerts,
    )


def read_check_runs(
    config: PipelineConfig,
    client: GitHubTransport,
    sha: str,
) -> tuple[CheckRunConclusion, ...]:
    """Read every check run reported for one SHA, plus legacy commit statuses."""
    root = _path(config, f"/commits/{sha}")
    observed: list[CheckRunConclusion] = []
    seen: set[str] = set()
    for raw_check in _paged_items(client, f"{root}/check-runs", "check_runs"):
        if not isinstance(raw_check, Mapping):
            continue
        name = raw_check.get("name")
        if not isinstance(name, str) or name in seen:
            continue
        conclusion = raw_check.get("conclusion")
        status = raw_check.get("status")
        seen.add(name)
        observed.append(
            CheckRunConclusion(
                name=name,
                conclusion=conclusion if isinstance(conclusion, str) else None,
                status=status if isinstance(status, str) else None,
            )
        )
    for raw_status in _paged_items(client, f"{root}/status", "statuses"):
        if not isinstance(raw_status, Mapping):
            continue
        context = raw_status.get("context")
        state = raw_status.get("state")
        if not isinstance(context, str) or context in seen or not isinstance(state, str):
            continue
        seen.add(context)
        observed.append(
            CheckRunConclusion(
                name=context,
                conclusion=state if state != "pending" else None,
                status="pending" if state == "pending" else "completed",
            )
        )
    return tuple(observed)


def upgrade_ci_mode(
    current: CiEvidenceMode,
    *,
    evidence: CheckRunEvidence,
    already_upgraded: bool = False,
) -> CiModeTransition:
    """Upgrade local evidence to GitHub evidence once check runs are observed."""
    if evidence.detail == "awaiting_workflow_approval":
        return CiModeTransition(current, False, ReasonCode.AWAITING_WORKFLOW_APPROVAL)
    return CiModeTransition(current, False)


def wait_for_check_runs(
    config: PipelineConfig,
    *,
    client: GitHubTransport,
    elapsed_s: float,
    conclusions: Sequence[CheckRunConclusion] | None = None,
    sha: str = "HEAD",
    poll: bool = True,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    poll_interval_s: float = 15.0,
    on_mode_transition: Callable[[CiModeTransition], None] | None = None,
    ci_mode: CiEvidenceMode | None = None,
    is_fork: bool = False,
) -> CiWaitResult:
    """Poll check runs until they settle, bounded by ``ci_wait_timeout_s``."""
    started = clock()
    deadline = started + max(config.ci_wait_timeout_s - elapsed_s, 0)
    current_mode = ci_mode or config.ci_evidence_mode
    already_upgraded = current_mode is CiEvidenceMode.ACTIONS
    evidence = CheckRunEvidence((), settled=False, passed=False)
    while True:
        if conclusions is None:
            try:
                observed = read_check_runs(config, client, sha)
            except (ArtifactUnavailableError, HttpTransportError, PreflightError) as exc:
                return CiWaitResult(
                    current_mode,
                    ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                    False,
                    str(exc),
                )
        else:
            observed = tuple(conclusions)
        evidence = evaluate_check_runs(
            observed,
            required_contexts=config.required_contexts_min,
        )
        transition = upgrade_ci_mode(
            current_mode,
            evidence=evidence,
            already_upgraded=already_upgraded,
        )
        if transition.transitioned:
            current_mode = transition.mode
            already_upgraded = True
            if on_mode_transition is not None:
                on_mode_transition(transition)
        if evidence.passed:
            return CiWaitResult(
                current_mode,
                None,
                config.auto_merge_enabled,
                conclusions=evidence.conclusions,
            )
        if evidence.settled:
            return CiWaitResult(
                current_mode,
                evidence.reason,
                False,
                evidence.detail,
                evidence.conclusions,
            )
        if evidence.detail == "awaiting_workflow_approval":
            return CiWaitResult(
                current_mode,
                ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                False,
                "awaiting_workflow_approval",
                evidence.conclusions,
            )
        if is_fork and not evidence.conclusions and clock() - started >= poll_interval_s:
            workflow_response = client.get(_path(config, f"/actions/runs?head_sha={sha}"))
            workflow_runs = (
                workflow_response.get("workflow_runs")
                if isinstance(workflow_response, Mapping)
                else None
            )
            if not isinstance(workflow_runs, list) or not workflow_runs:
                return CiWaitResult(
                    current_mode,
                    ReasonCode.CI_WORKFLOWS_ABSENT,
                    False,
                    "ci_workflows_absent",
                    evidence.conclusions,
                )
            return CiWaitResult(
                current_mode,
                ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                False,
                "awaiting_workflow_approval",
                evidence.conclusions,
            )
        if not poll:
            break
        remaining = deadline - clock()
        if remaining <= 0:
            break
        sleep(min(poll_interval_s, remaining))
    return CiWaitResult(
        current_mode,
        ReasonCode.CI_EVIDENCE_UNAVAILABLE,
        False,
        None,
        evidence.conclusions,
    )


class SimulationWriteError(RuntimeError):
    """Raised if a SIMULATE path attempts a remote mutation."""


class ArtifactUnavailableError(RuntimeError):
    """Raised when the configured GitHub artifact sink is unavailable."""


class GitHubResponseError(ArtifactUnavailableError):
    """Raised when GitHub returns a structurally invalid response."""


class LabelCapabilityError(ArtifactUnavailableError):
    """Raised when a required lifecycle label cannot be read or created."""

    reason = ReasonCode.LABEL_CAPABILITY_UNAVAILABLE


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
    """Links reconciled or created by the two-write publication contract."""

    issue_url: str | None
    pr_url: str | None
    issue_number: int | None = None
    pr_number: int | None = None
    auto_merge_requested: bool = False
    issue_adopted: bool = False
    pr_adopted: bool = False


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

    def read_named_suite(self, sha: str) -> SuiteResult:
        """Read the configured Actions suite check at a revision."""
        context = self._config.suite_check_context
        if self._transport is None:
            return SuiteResult(
                passed=False,
                command=f"GET check-runs context={context}",
                reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            )
        return read_named_suite(self._config, self._transport, sha)

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

    def changed_paths_between(self, base_sha: str, head_sha: str) -> list[str]:
        """Read paths changed on the candidate branch from GitHub compare data."""
        response = self._read(
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/compare/"
            f"{base_sha}...{head_sha}"
        )
        if not isinstance(response, Mapping):
            return []
        files = response.get("files")
        if not isinstance(files, list):
            return []
        paths: list[str] = []
        for item in files:
            if isinstance(item, Mapping) and isinstance(item.get("filename"), str):
                paths.append(str(item["filename"]))
        return paths

    def issue_for_marker(self, marker: str) -> tuple[int, str] | None:
        """Find an existing tracking issue by its candidate marker."""
        if not self._config.marker_search_enabled:
            return None
        query = urlencode(
            {
                "q": (
                    f"repo:{self._config.target_owner}/{self._config.target_repo} "
                    f'is:issue in:body "{marker}"'
                )
            }
        )
        response = self._read(f"/search/issues?{query}")
        if not isinstance(response, Mapping):
            return None
        items = response.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, Mapping) or "pull_request" in item:
                continue
            body = item.get("body")
            number = item.get("number")
            url = item.get("html_url")
            if (
                isinstance(body, str)
                and marker in body
                and isinstance(number, int)
                and isinstance(url, str)
            ):
                return number, url
        return None

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
            if self._ensured_labels[label]:
                return True
            raise LabelCapabilityError(f"required label unavailable: {label}")
        path = f"/repos/{self._config.target_owner}/{self._config.target_repo}/labels/{label}"
        try:
            self._read(path)
            self._ensured_labels[label] = True
            return True
        except HttpTransportError as exc:
            if exc.status_code != 404:
                self._ensured_labels[label] = False
                raise LabelCapabilityError(
                    f"cannot read required label {label}: HTTP {exc.status_code}"
                ) from exc
        try:
            self._write(
                "post",
                f"/repos/{self._config.target_owner}/{self._config.target_repo}/labels",
                {"name": label},
            )
        except HttpTransportError as exc:
            self._ensured_labels[label] = False
            raise LabelCapabilityError(
                f"cannot create required label {label}: HTTP {exc.status_code}"
            ) from exc
        self._ensured_labels[label] = True
        return True

    def enable_auto_merge(self, number: int, *, ci_mode: CiEvidenceMode) -> None:
        """Request auto-merge only after the caller has checked all gates."""
        if (
            not self._config.auto_merge_enabled
            or self._config.ci_evidence_mode is not CiEvidenceMode.ACTIONS
            or ci_mode is not CiEvidenceMode.ACTIONS
        ):
            raise ValueError("auto-merge requires enabled GitHub CI evidence")
        self._write(
            "post",
            f"/repos/{self._config.target_owner}/{self._config.target_repo}/pulls/{number}/auto-merge",
            {"merge_method": "squash"},
        )


def reconcile_issue(
    client: GitHubClient,
    *,
    marker: str,
    title: str,
    body: str,
    labels: Sequence[str] = (),
    existing_issue_number: int | None = None,
    existing_issue_url: str | None = None,
) -> tuple[int, str, bool]:
    """Adopt the marker-matched tracking issue, or create it exactly once."""
    if existing_issue_number is not None and existing_issue_url is not None:
        return existing_issue_number, existing_issue_url, True
    if not client._config.has_issues:
        raise ArtifactUnavailableError("GitHub issues are unavailable")
    found = client.issue_for_marker(marker)
    if found is not None:
        return found[0], found[1], True
    number, url = client.create_issue(title, body, labels)
    return number, url, False


def reconcile_pull_request(
    client: GitHubClient,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
    existing_pr_number: int | None = None,
    existing_pr_url: str | None = None,
) -> tuple[int, str, bool]:
    """Adopt the pull request for this exact head branch, or create it once."""
    match = (
        client.pull_request(existing_pr_number)
        if existing_pr_number is not None
        else client.pull_request_for_head(head)
    )
    if match is not None:
        if match.merged_at is not None:
            raise MergedPullRequestError(match)
        if match.state != "open":
            raise ClosedPullRequestError(head, match)
        return match.number, match.url, True
    if existing_pr_number is not None and existing_pr_url is not None:
        return existing_pr_number, existing_pr_url, True
    try:
        number, url = client.create_pr(title, body, head=head, base=base)
    except HttpTransportError as exc:
        if exc.status_code != 422:
            raise
        existing = client.pull_request_for_head(head)
        if existing is None:
            raise
        if existing.merged_at is not None:
            raise MergedPullRequestError(existing) from exc
        if existing.state != "open":
            raise ClosedPullRequestError(head, existing) from exc
        return existing.number, existing.url, True
    return number, url, False


def auto_merge_eligible(
    candidate: Candidate,
    config: PipelineConfig,
    ci_result: CiWaitResult | None,
) -> bool:
    """Decide auto-merge from `merge_mode`, configuration and CI evidence."""
    return bool(
        candidate.merge_mode is MergeMode.AUTO
        and candidate.auto_merge_eligible
        and config.auto_merge_enabled
        and bool(config.required_contexts_min)
        and ci_result is not None
        and ci_result.mode is CiEvidenceMode.ACTIONS
        and ci_result.auto_merge_eligible
        and ci_result.reason is None
        and (candidate.test_added is True or candidate.test_exempt_reason is not None)
    )


def publish_artifacts(
    client: GitHubClient,
    candidate: Candidate,
    *,
    marker: str,
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
    after_pr_adopted: Callable[[int, str], None] | None = None,
    after_ci: Callable[[int], None] | None = None,
) -> ArtifactLinks:
    """Publish the tracking issue and, for high tier, exactly one pull request.

    The issue is the first write and the pull request is the last: nothing
    patches the issue afterwards and no comment is ever created.
    """
    if preflight is not None:
        preflight()
    issue_number, issue_url, issue_adopted = reconcile_issue(
        client,
        marker=marker,
        title=issue_title,
        body=issue_body,
        labels=labels,
        existing_issue_number=existing_issue_number,
        existing_issue_url=existing_issue_url,
    )
    if after_issue is not None and not issue_adopted:
        after_issue(issue_number, issue_url)
    if (
        candidate.tier is not Tier.HIGH
        or pr_title is None
        or pr_body is None
        or (head is None and existing_pr_number is None)
    ):
        return ArtifactLinks(
            issue_url,
            None,
            issue_number=issue_number,
            issue_adopted=issue_adopted,
        )
    if ci_probe is None:
        raise ValueError("PR publication requires an observed CI probe")
    if head is None:
        raise ArtifactUnavailableError("PR publication requires a candidate branch")
    if preflight is not None:
        preflight()
    pr_number, pr_url, pr_adopted = reconcile_pull_request(
        client,
        title=pr_title,
        body=pr_body,
        head=head,
        base=base,
        existing_pr_number=existing_pr_number,
        existing_pr_url=existing_pr_url,
    )
    if pr_adopted:
        if after_pr_adopted is not None:
            after_pr_adopted(pr_number, pr_url)
    elif after_pr_created is not None:
        after_pr_created(pr_number, pr_url)
    ci_result = ci_probe(pr_number)
    if preflight is not None:
        preflight()
    if after_ci is not None:
        after_ci(pr_number)
    auto_merge_requested = auto_merge_eligible(candidate, client._config, ci_result)
    if auto_merge_requested:
        client.enable_auto_merge(pr_number, ci_mode=ci_result.mode)
    return ArtifactLinks(
        issue_url,
        pr_url,
        issue_number=issue_number,
        pr_number=pr_number,
        auto_merge_requested=auto_merge_requested,
        issue_adopted=issue_adopted,
        pr_adopted=pr_adopted,
    )


__all__ = [
    "ACCEPTED_CONCLUSIONS",
    "ArtifactLinks",
    "ArtifactUnavailableError",
    "CheckRunEvidence",
    "CiModeTransition",
    "CiWaitResult",
    "ClosedPullRequestError",
    "GitHubClient",
    "GitHubResponseError",
    "LabelCapabilityError",
    "GitHubTransport",
    "LivePreflight",
    "MergedPullRequestError",
    "PullRequestMatch",
    "PreflightError",
    "REJECTED_CONCLUSIONS",
    "SimulationWriteError",
    "auto_merge_eligible",
    "evaluate_check_runs",
    "publish_artifacts",
    "read_check_runs",
    "reconcile_issue",
    "reconcile_pull_request",
    "run_live_preflight",
    "upgrade_ci_mode",
    "wait_for_check_runs",
]
