"""Injected, guarded GitHub artifact transport."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pipeline.config import IssueSink, Mode, PipelineConfig
from pipeline.schemas import Candidate, Tier


class GitHubTransport(Protocol):
    """Minimal transport required for GitHub writes."""

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
        return ArtifactLinks(issue_url, None)
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
    ):
        client.enable_auto_merge(pr_number)
    return ArtifactLinks(issue_url, pr_url)


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
    return ArtifactLinks(None, pr_url, comment_url)


__all__ = [
    "ArtifactLinks",
    "ArtifactUnavailableError",
    "GitHubClient",
    "GitHubRateLimitError",
    "GitHubTransport",
    "SimulationWriteError",
    "publish_artifacts",
    "publish_degraded",
]
