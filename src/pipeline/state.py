"""Append-only candidate state and resume storage."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Iterable
from pathlib import Path

from pipeline.dedupe import find_drift_match
from pipeline.schemas import Candidate


def github_marker_search(
    query: Callable[[str], object],
) -> Callable[[str], bool]:
    """Return a marker lookup backed by GitHub's issue and pull-request search."""

    def contains(marker: str) -> bool:
        result = query(marker)
        if isinstance(result, dict):
            total = result.get("total_count")
            return isinstance(total, int) and total > 0
        if isinstance(result, list):
            return bool(result)
        return False

    return contains


def repository_marker_search(repository: Path) -> Callable[[str], bool]:
    """Compatibility helper for local callers; LIVE uses GitHub search instead."""

    def contains(marker: str) -> bool:
        for path in repository.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                if marker in path.read_text(encoding="utf-8"):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
        return False

    return contains


class CandidateStateStore:
    """Persist candidate lifecycle rows with last-write-wins reads."""

    def __init__(
        self,
        path: Path,
        *,
        marker_search: Callable[[str], bool] | None = None,
    ) -> None:
        self._path = path
        self._marker_search = marker_search
        self.marker_search_failed = False

    def _read_rows(self) -> list[Candidate]:
        if not self._path.exists():
            return []
        rows: list[Candidate] = []
        quarantine = self._path.with_suffix(self._path.suffix + ".corrupt")
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(Candidate.model_validate(json.loads(line), strict=False))
                except (json.JSONDecodeError, TypeError, ValueError):
                    with quarantine.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
        return rows

    def rows(self) -> list[Candidate]:
        """Return all append-only rows in write order."""
        return self._read_rows()

    def latest(self) -> dict[str, Candidate]:
        """Return the last row for each candidate ID."""
        latest: dict[str, Candidate] = {}
        for candidate in self._read_rows():
            latest[candidate.candidate_id] = candidate
        return latest

    def get(self, candidate_id: str) -> Candidate | None:
        """Return the current last-write-wins row for one candidate."""
        return self.latest().get(candidate_id)

    def resume(self, candidate_id: str) -> Candidate | None:
        """Return persisted lifecycle state for retry or artifact recovery."""
        return self.get(candidate_id)

    def existing_artifact(self, candidate_id: str) -> bool:
        """Search persisted state and target artifacts before a write."""
        current = self.latest().get(candidate_id)
        if current is not None and (
            current.issue_url is not None
            or current.pr_url is not None
            or current.state.value
            in {"issue_created", "pr_created", "issue_patched", "comment_created"}
        ):
            return True
        return self.marker_exists(candidate_id)

    def marker_exists(self, candidate_id: str) -> bool:
        """Search the target repository for one candidate's stable marker."""
        if self._marker_search is None:
            return False
        try:
            return self._marker_search(f"<!-- devin-remediation-id: {candidate_id} -->")
        except Exception:
            self.marker_search_failed = True
            return False

    def append(self, candidate: Candidate) -> None:
        """Append one candidate lifecycle row after rereading current state."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def append_if_new_artifact(self, candidate: Candidate) -> bool:
        """Atomically reserve a candidate before the first artifact write."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        marker_exists = self.marker_exists(candidate.candidate_id)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            current = self.latest().get(candidate.candidate_id)
            local_artifact = current is not None and (
                current.issue_url is not None
                or current.pr_url is not None
                or current.state.value
                in {"issue_created", "pr_created", "issue_patched", "comment_created"}
            )
            if local_artifact or marker_exists:
                return False
            self.append(candidate)
            return True

    def supersede(self, previous: Candidate, current: Candidate) -> None:
        """Append the two immutable rows needed to record a drift supersession."""
        old = previous.model_copy(update={"superseded_by": current.candidate_id})
        new = current.model_copy(update={"supersedes": previous.candidate_id})
        self.append(old)
        self.append(new)

    def drift_match(
        self,
        current: Candidate,
        *,
        current_scan: Iterable[Candidate] | None = None,
    ) -> Candidate | None:
        """Find a unique persisted active row matching a current drifted alert."""
        return find_drift_match(
            self._read_rows(),
            current,
            current_scan=tuple(current_scan) if current_scan is not None else None,
        )


__all__ = ["CandidateStateStore", "github_marker_search", "repository_marker_search"]
