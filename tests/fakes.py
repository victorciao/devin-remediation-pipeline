"""Credential-free fakes for the GitHub and Devin surfaces (§17: no network in tests)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


class WriteAttempted(AssertionError):
    """Raised by :class:`NoWriteGitHubClient` when SIMULATE mode attempts a remote write."""


@dataclass
class ReviewFinding:
    """A §9 code-review finding (`blocking` / `major` / `minor` / `nit`)."""

    severity: str
    criterion_id: str | None = None
    note: str = ""
    resolved: bool = False


@dataclass
class FakeGitHubClient:
    """Records every call; write methods append to :attr:`writes`.

    ``has_issues``/``labels``/``issues`` model the target-repository state the §0d preflight
    and the §14.1 marker search read.
    """

    has_issues: bool = True
    existing_labels: list[str] = field(default_factory=list)
    issue_bodies: list[str] = field(default_factory=list)
    pull_request_bodies: list[str] = field(default_factory=list)
    reported_contexts: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    writes: list[tuple[str, Mapping[str, Any]]] = field(default_factory=list)
    next_issue_number: int = 101
    fail_pr_creation: bool = False

    # -- reads -------------------------------------------------------------------------
    def get_repo(self) -> Mapping[str, Any]:
        self.reads.append("get_repo")
        return {"has_issues": self.has_issues}

    def search_marker(self, marker: str) -> list[Mapping[str, Any]]:
        self.reads.append(f"search_marker:{marker}")
        hits: list[Mapping[str, Any]] = []
        for index, body in enumerate(self.issue_bodies):
            if marker in body:
                hits.append({"number": 100 + index, "body": body, "type": "issue"})
        for index, body in enumerate(self.pull_request_bodies):
            if marker in body:
                hits.append({"number": 200 + index, "body": body, "type": "pull_request"})
        return hits

    def list_labels(self) -> list[str]:
        self.reads.append("list_labels")
        return list(self.existing_labels)

    def list_required_contexts(self, sha: str) -> list[str]:
        self.reads.append(f"list_required_contexts:{sha}")
        return list(self.reported_contexts)

    # -- writes ------------------------------------------------------------------------
    def _write(self, name: str, payload: Mapping[str, Any]) -> None:
        self.writes.append((name, dict(payload)))

    def create_issue(self, *, title: str, body: str) -> Mapping[str, Any]:
        self._write("create_issue", {"title": title, "body": body})
        number = self.next_issue_number
        self.next_issue_number += 1
        self.issue_bodies.append(body)
        return {"number": number, "html_url": f"https://github.test/issues/{number}"}

    def update_issue(self, *, number: int, body: str) -> Mapping[str, Any]:
        self._write("update_issue", {"number": number, "body": body})
        return {"number": number, "html_url": f"https://github.test/issues/{number}"}

    def create_pull_request(self, *, title: str, body: str, head: str) -> Mapping[str, Any]:
        if self.fail_pr_creation:
            self._write("create_pull_request_failed", {"title": title, "head": head})
            raise RuntimeError("pull request creation failed")
        self._write("create_pull_request", {"title": title, "body": body, "head": head})
        self.pull_request_bodies.append(body)
        return {"number": 900, "html_url": "https://github.test/pull/900"}

    def create_comment(self, *, number: int, body: str) -> Mapping[str, Any]:
        self._write("create_comment", {"number": number, "body": body})
        return {"id": 1, "html_url": f"https://github.test/pull/{number}#comment"}

    def create_label(self, *, name: str) -> Mapping[str, Any]:
        self._write("create_label", {"name": name})
        self.existing_labels.append(name)
        return {"name": name}

    def add_labels(self, *, number: int, labels: Sequence[str]) -> Mapping[str, Any]:
        self._write("add_labels", {"number": number, "labels": list(labels)})
        return {"number": number}

    def merge_pull_request(self, *, number: int) -> Mapping[str, Any]:
        self._write("merge_pull_request", {"number": number})
        return {"merged": True}

    @property
    def write_names(self) -> list[str]:
        """Names of the write calls made, in order."""
        return [name for name, _ in self.writes]


@dataclass
class NoWriteGitHubClient(FakeGitHubClient):
    """A client whose every write method fails the test — proves SIMULATE writes nothing."""

    def _write(self, name: str, payload: Mapping[str, Any]) -> None:
        raise WriteAttempted(f"SIMULATE mode attempted a remote write: {name}({dict(payload)})")


@dataclass
class FakeResponse:
    """A canned HTTP response for the rate-limit backoff contract."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitedCall:
    """Returns 429 for the first ``limited_calls`` invocations, then 200."""

    limited_calls: int = 1
    reset_header: str = "1"
    calls: int = 0

    def __call__(self) -> FakeResponse:
        self.calls += 1
        if self.calls <= self.limited_calls:
            return FakeResponse(
                status_code=429,
                headers={"retry-after": self.reset_header, "x-ratelimit-reset": self.reset_header},
            )
        return FakeResponse(status_code=200, payload={"ok": True})


@dataclass
class SleepRecorder:
    """Captures the sleep durations the backoff helper asks for."""

    durations: list[float] = field(default_factory=list)

    def __call__(self, seconds: float) -> None:
        self.durations.append(seconds)
