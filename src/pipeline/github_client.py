"""Injected, guarded GitHub artifact transport."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast
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
class CapabilityReport:
    """Capability result for the pre-publication dispatch seam."""

    has_issues: bool
    ci_evidence_mode: CiEvidenceMode
    alert_source: str
    auto_merge_enabled: bool
    reasons: tuple[ReasonCode, ...]
    token_login: str | None
    token_scopes: tuple[str, ...]


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


class _BackoffResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]


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


class GitHubRateLimitError(RuntimeError):
    """Rate-limit response with server-provided retry timing."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        reset_at: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.reset_at = reset_at


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
    notes.append(f"token_identity: {login}")
    notes.append(f"token_scopes: {', '.join(scopes) if scopes else 'none reported'}")
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
            if isinstance(name, str) and isinstance(conclusion, str):
                result[name] = conclusion
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


def probe_capabilities(config: PipelineConfig, *, client: GitHubTransport) -> CapabilityReport:
    """Resolve dispatch capabilities from observed repository and CI state."""
    repository = _mapping(client.get(_path(config, "")), "repository")
    has_issues_value = repository.get("has_issues")
    has_issues = has_issues_value if isinstance(has_issues_value, bool) else False
    reported_contexts = tuple(
        name
        for name, conclusion in _required_context_statuses(config, client, "HEAD").items()
        if conclusion in {"success", "successful"}
    )
    transition = maybe_upgrade_ci_mode(
        config.ci_evidence_mode,
        reported_contexts=reported_contexts,
    )
    reasons: list[ReasonCode] = []
    if not has_issues and config.issue_sink is not IssueSink.PR_COMMENT:
        reasons.append(ReasonCode.CAPABILITY_UNAVAILABLE)
    return CapabilityReport(
        has_issues=has_issues,
        ci_evidence_mode=transition.mode,
        alert_source=config.alert_source.value,
        auto_merge_enabled=(
            config.auto_merge_enabled and transition.mode is CiEvidenceMode.GITHUB and not reasons
        ),
        reasons=tuple(reasons),
        token_login=None,
        token_scopes=(),
    )


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
    reported_contexts: Sequence[str] = (),
    sha: str = "HEAD",
) -> CiWaitResult:
    """Resolve generated-PR CI evidence without treating missing reports as green."""
    statuses = (
        _required_context_statuses(config, client, sha)
        if not reported_contexts
        else {context: "success" for context in reported_contexts}
    )
    if elapsed_s >= config.ci_wait_timeout_s:
        return CiWaitResult(
            CiEvidenceMode.LOCAL,
            ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            False,
        )
    complete = all(
        statuses.get(context) in {"success", "successful"} for context in REQUIRED_CONTEXTS
    )
    if complete:
        return CiWaitResult(
            CiEvidenceMode.GITHUB,
            None,
            config.auto_merge_enabled,
        )
    return CiWaitResult(CiEvidenceMode.LOCAL, ReasonCode.CI_EVIDENCE_UNAVAILABLE, False)


def request_with_backoff(
    call: Callable[[], object],
    *,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    max_retries: int = 3,
) -> object:
    """Retry rate-limited response objects using server-provided timing."""
    for attempt in range(max_retries + 1):
        response = call()
        result = cast(_BackoffResponse, response)
        if result.status_code not in {403, 429}:
            return response
        if attempt >= max_retries:
            raise GitHubRateLimitError("GitHub rate-limit retry budget exhausted")
        retry_after = next(
            (
                float(value)
                for key, value in result.headers.items()
                if key.casefold() == "retry-after"
            ),
            None,
        )
        reset_at = next(
            (
                float(value)
                for key, value in result.headers.items()
                if key.casefold() == "x-ratelimit-reset"
            ),
            None,
        )
        delay = retry_after
        if delay is None and reset_at is not None:
            delay = max(reset_at - now(), 0.0)
        sleep(1.0 if delay is None else delay)
    raise AssertionError("GitHub rate-limit retry loop exhausted")


class SimulationWriteError(RuntimeError):
    """Raised if a SIMULATE path attempts a remote mutation."""


class ArtifactUnavailableError(RuntimeError):
    """Raised when the configured GitHub artifact sink is unavailable."""


@dataclass(frozen=True)
class ArtifactLinks:
    """Links created by the mandated issue/PR ordering."""

    issue_url: str | None
    pr_url: str | None
    comment_url: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None


class GitHubClient:
    """Perform guarded GitHub writes through one injectable transport."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        transport: GitHubTransport | None = None,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
        write_guard: Callable[[], None] | None = None,
        max_wait_s: float = 300.0,
        max_attempts: int = 4,
    ) -> None:
        self._config = config
        self._transport = transport
        self._clock = clock
        self._sleeper = sleeper
        self._write_guard = write_guard
        self._max_wait_s = max_wait_s
        self._max_attempts = max_attempts
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
            raise ValueError("GitHub transport is unavailable")
        operation = self._transport.post if method == "post" else self._transport.patch
        waited = 0.0
        for attempt in range(self._max_attempts):
            try:
                if self._write_guard is not None:
                    self._write_guard()
                return operation(path, payload)
            except GitHubRateLimitError as exc:
                if attempt + 1 >= self._max_attempts:
                    raise
                delay = exc.retry_after
                if delay is None and exc.reset_at is not None:
                    delay = max(exc.reset_at - self._clock(), 0.0)
                delay = min(delay if delay is not None else 1.0, self._max_wait_s - waited)
                if delay <= 0:
                    raise
                self._sleeper(delay)
                waited += delay
        raise AssertionError("GitHub write loop exhausted")

    @staticmethod
    def _url(response: Mapping[str, object]) -> str:
        """Extract the canonical URL from a GitHub mutation response."""
        value = response.get("html_url", response.get("url"))
        if not isinstance(value, str) or not value:
            raise ValueError("GitHub response lacks html_url")
        return value

    @staticmethod
    def _number(response: Mapping[str, object]) -> int:
        """Extract an issue or pull-request number."""
        value = response.get("number")
        if not isinstance(value, int):
            raise ValueError("GitHub response lacks number")
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

    def enable_auto_merge(self, number: int) -> None:
        """Request auto-merge only after the caller has checked all gates."""
        if not self._config.auto_merge_enabled or self._config.ci_evidence_mode.value != "github":
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
    ci_green: bool = False,
) -> ArtifactLinks:
    """Publish artifacts in the mandated issue → PR → issue-patch order."""
    if not client._config.has_issues:
        if client._config.issue_sink is not IssueSink.PR_COMMENT:
            raise ArtifactUnavailableError("GitHub issues are unavailable")
        raise ArtifactUnavailableError("use publish_degraded for PR comments")
    if preflight is not None:
        preflight()
    issue_number, issue_url = client.create_issue(issue_title, issue_body, labels)
    if candidate.tier is Tier.MEDIUM or pr_title is None or pr_body is None or head is None:
        return ArtifactLinks(issue_url, None, issue_number=issue_number)
    if preflight is not None:
        preflight()
    pr_number, pr_url = client.create_pr(
        pr_title,
        f"{pr_body.rstrip()}\n\nCloses #{issue_number}\n",
        head=head,
        base=base,
    )
    if preflight is not None:
        preflight()
    client.patch_issue(issue_number, f"{issue_body.rstrip()}\n\nPR: {pr_url}\n")
    if (
        candidate.auto_merge_eligible
        and client._config.auto_merge_enabled
        and client._config.ci_evidence_mode.value == "github"
        and ci_green is True
    ):
        client.enable_auto_merge(pr_number)
    return ArtifactLinks(issue_url, pr_url, issue_number=issue_number, pr_number=pr_number)


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
    if preflight is not None:
        preflight()
    comment_url = client.comment_pr(pr_number, comment_body)
    return ArtifactLinks(None, pr_url, comment_url, pr_number=pr_number)


__all__ = [
    "ArtifactLinks",
    "ArtifactUnavailableError",
    "CapabilityReport",
    "CiModeTransition",
    "CiWaitResult",
    "GitHubClient",
    "GitHubRateLimitError",
    "GitHubTransport",
    "LivePreflight",
    "PreflightError",
    "REQUIRED_CONTEXTS",
    "SimulationWriteError",
    "maybe_upgrade_ci_mode",
    "probe_capabilities",
    "publish_artifacts",
    "publish_degraded",
    "request_with_backoff",
    "run_live_preflight",
    "wait_for_required_contexts",
]
