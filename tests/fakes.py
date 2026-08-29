"""Credential-free GitHub transport fakes (§17: no network and no secrets in tests).

The single fake here is a *transport*, not a client: §7/§10/§14.1 place the ordering, CI and
resume contracts in `github_client` and `__main__`, so a fake that stubbed the client would
test nothing that ships.  Every read is scripted from repository state and every write is
recorded in order, which is what the publication-order and idempotency assertions read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from pipeline.github_client import REQUIRED_CONTEXTS
from pipeline.http_transport import HttpTransportError

OWNER = "victorciao"
REPO = "superset"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
SIGNED_COMMIT = (
    "fix(mcp): bound the generated range\n\nSigned-off-by: devin <devin@example.invalid>\n"
)
MULTIWORD_SIGNED_COMMIT = (
    "fix(mcp): bound the generated range\n\n"
    "Signed-off-by: Devin Remediation <devin@example.invalid>\n"
)


class TransportInterrupted(BaseException):
    """Simulates a process death mid-publication; deliberately not an `Exception`.

    §14.1's crash windows are about the process disappearing between a durable write and the
    next one, so this must escape the per-candidate containment tuple rather than be reported
    as a capability deferral.
    """


@dataclass(frozen=True)
class WriteRecord:
    """One recorded mutation: the ordering assertions read these in sequence."""

    method: str
    path: str
    payload: Mapping[str, object]


def all_contexts(state: str = "success") -> dict[str, str]:
    """Every one of §10's 13 required contexts reporting the same state."""
    return {context: state for context in REQUIRED_CONTEXTS}


@dataclass
class FakeGitHubTransport:
    """Serve scripted GitHub reads and record every write in order."""

    owner: str = OWNER
    repo: str = REPO
    base_sha: str = BASE_SHA
    head_sha: str = HEAD_SHA
    context_states: Mapping[str, str] = field(default_factory=all_contexts)
    check_run_statuses: Mapping[str, str] = field(default_factory=dict)
    commit_messages: Sequence[str] = (SIGNED_COMMIT,)
    branch_shas: Mapping[str, str] = field(default_factory=dict)
    head_repo_full_name: str | None = None
    pr_state: str = "open"
    pr_merged_at: str | None = None
    marker_hits: int = 0
    existing_pull_requests: Sequence[Mapping[str, object]] = ()
    labels_present: bool = True
    create_pr_error: HttpTransportError | None = None
    create_branch_error: HttpTransportError | None = None
    next_number: int = 1
    reads: list[str] = field(default_factory=list)
    writes: list[WriteRecord] = field(default_factory=list)
    before_write: Callable[[WriteRecord], None] | None = None

    # -- reads ---------------------------------------------------------------------------

    @property
    def response_headers(self) -> Mapping[str, str]:
        return {"x-oauth-scopes": "repo"}

    def _prefix(self) -> str:
        return f"/repos/{self.owner}/{self.repo}"

    def get(self, path: str) -> object:
        self.reads.append(path)
        prefix = self._prefix()
        if path.startswith("/search/issues"):
            return {"total_count": self.marker_hits}
        if path.startswith(f"{prefix}/git/ref/heads/"):
            branch = path.rsplit("/", 1)[-1]
            sha = self.branch_shas.get(branch, self.base_sha if branch == "master" else None)
            if sha is None:
                raise HttpTransportError("no such ref", status_code=404)
            return {"object": {"sha": sha}}
        if path.startswith(f"{prefix}/labels/"):
            if self.labels_present:
                return {"name": path.rsplit("/", 1)[-1]}
            raise HttpTransportError("no such label", status_code=404)
        if path.startswith(f"{prefix}/compare/"):
            base, _, head = path.rsplit("/", 1)[-1].partition("...")
            if base == head:
                return {"commits": []}
            return {
                "commits": [{"commit": {"message": message}} for message in self.commit_messages]
            }
        if "/check-runs" in path:
            return {
                "check_runs": [
                    {"name": name, "conclusion": state}
                    for name, state in self.context_states.items()
                ]
                + [
                    {"name": name, "conclusion": None, "status": state}
                    for name, state in self.check_run_statuses.items()
                ]
            }
        if path.endswith("/status"):
            return {"statuses": []}
        if path.startswith(f"{prefix}/pulls?"):
            return list(self.existing_pull_requests)
        if path.startswith(f"{prefix}/pulls/"):
            number = int(path.rsplit("/", 1)[-1])
            return {
                "number": number,
                "html_url": f"https://github.test/{self.owner}/{self.repo}/pull/{number}",
                "state": self.pr_state,
                "merged_at": self.pr_merged_at,
                "head": {
                    "sha": self.head_sha,
                    "repo": {"full_name": self.head_repo_full_name or f"{self.owner}/{self.repo}"},
                },
            }
        if path == f"{prefix}":
            return {"has_issues": True}
        return {}

    # -- writes --------------------------------------------------------------------------

    def _write(self, method: str, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        record = WriteRecord(method, path, dict(payload))
        if self.before_write is not None:
            self.before_write(record)
        if path.endswith("/pulls") and self.create_pr_error is not None:
            raise self.create_pr_error
        if path.endswith("/git/refs") and self.create_branch_error is not None:
            raise self.create_branch_error
        self.writes.append(record)
        return self._response_for(method, path)

    def _response_for(self, method: str, path: str) -> Mapping[str, object]:
        """Answer a write the way the API does: patches echo the resource they touched."""
        host = f"https://github.test/{self.owner}/{self.repo}"
        if method == "patch":
            number = int(path.rsplit("/", 1)[-1])
            kind = "pull" if "/pulls/" in path else "issues"
            return {"number": number, "html_url": f"{host}/{kind}/{number}"}
        allocated = self.next_number
        self.next_number += 1
        if path.endswith("/comments"):
            issue = path.split("/")[-2]
            return {
                "id": allocated,
                "html_url": f"{host}/issues/{issue}#issuecomment-{allocated}",
            }
        kind = "pull" if path.endswith("/pulls") else "issues"
        return {"number": allocated, "html_url": f"{host}/{kind}/{allocated}"}

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._write("post", path, payload)

    def patch(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._write("patch", path, payload)

    # -- assertions helpers --------------------------------------------------------------

    @property
    def write_paths(self) -> list[str]:
        """The mutated paths in write order; the §14.1 ordering contract reads this."""
        return [record.path for record in self.writes]

    def payload_for(self, path: str) -> Mapping[str, object]:
        """The payload of the last write to one path."""
        return next(record.payload for record in reversed(self.writes) if record.path == path)

    def labels_for(self, number: int) -> list[str]:
        """Every label written onto one issue or pull request, in write order."""
        path = f"{self._prefix()}/issues/{number}/labels"
        labels: list[str] = []
        for record in self.writes:
            if record.path != path:
                continue
            written = record.payload.get("labels")
            if isinstance(written, list):
                labels.extend(str(label) for label in written)
        return labels
