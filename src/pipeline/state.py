"""Append-only candidate state and resume storage."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pipeline.dedupe import find_drift_match
from pipeline.schemas import Candidate, CandidateState


class ResumeAction(str, Enum):
    """Action selected by the pure lifecycle-resume decision."""

    SKIP = "skip"
    RESUME_AT_STEP = "resume_at_step"
    DEFER = "defer"


class StatePreservationError(RuntimeError):
    """Raised when a resume write would discard durable artifact identity."""


class MarkerSearchOutcome(str, Enum):
    """Outcome of one candidate's marker lookup."""

    FOUND = "found"
    ABSENT = "absent"
    FAILED = "failed"
    ORPHANED = "orphaned"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class MarkerArtifact:
    """Unique GitHub artifact found by the candidate marker search."""

    number: int
    url: str
    is_pull_request: bool


@dataclass(frozen=True)
class ResumeDecision:
    """Pure decision for resuming one persisted candidate."""

    action: ResumeAction
    step: str | None = None


def has_local_artifact(candidate: Candidate | None) -> bool:
    """Return whether a persisted row proves an artifact already exists."""
    return candidate is not None and any(
        link is not None for link in (candidate.issue_url, candidate.pr_url, candidate.comment_url)
    )


def decide_resume(
    persisted: Candidate | None,
    *,
    artifacts_present: bool,
    marker_search_available: bool = True,
    marker_search_orphaned: bool = False,
) -> ResumeDecision:
    """Resolve lifecycle resume behavior without consulting external state."""
    if persisted is None:
        if (not marker_search_available or marker_search_orphaned) and not artifacts_present:
            return ResumeDecision(ResumeAction.DEFER)
        return ResumeDecision(ResumeAction.RESUME_AT_STEP, "publication")
    if persisted.state in {
        CandidateState.ISSUE_PATCHED,
        CandidateState.COMMENT_CREATED,
        CandidateState.TERMINAL,
    }:
        return ResumeDecision(ResumeAction.SKIP)
    if persisted.state is CandidateState.CONVERGED and artifacts_present:
        return ResumeDecision(ResumeAction.SKIP)
    if not has_local_artifact(persisted) and (
        artifacts_present or not marker_search_available or marker_search_orphaned
    ):
        return ResumeDecision(ResumeAction.DEFER)
    return ResumeDecision(ResumeAction.RESUME_AT_STEP, "publication")


def github_marker_search(
    query: Callable[[str], object],
) -> Callable[[str], MarkerArtifact | None]:
    """Return a unique marker lookup backed by GitHub search."""

    def find(marker: str) -> MarkerArtifact | None:
        result = query(marker)
        if not isinstance(result, dict):
            raise ValueError("marker search response is not an object")
        total = result.get("total_count")
        items = result.get("items")
        if not isinstance(total, int) or not isinstance(items, list):
            raise ValueError("marker search response lacks total_count/items")
        if total == 0:
            return None
        if total != 1 or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("marker search did not return a unique artifact")
        item = items[0]
        number = item.get("number")
        url = item.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str) or not url:
            raise ValueError("marker search artifact lacks number/html_url")
        return MarkerArtifact(
            number=number,
            url=url,
            is_pull_request=isinstance(item.get("pull_request"), dict) or "/pull/" in url,
        )

    return find


class CandidateStateStore:
    """Persist candidate lifecycle rows with last-write-wins reads."""

    def __init__(
        self,
        path: Path,
        *,
        marker_search: Callable[[str], MarkerArtifact | None] | None = None,
    ) -> None:
        self._path = path
        self._marker_search = marker_search
        self.marker_search_failed = False
        self.quarantined_rows = 0
        self._quarantine_seen: set[str] | None = None
        self._marker_results: dict[str, MarkerArtifact | None] = {}
        self._marker_outcomes: dict[str, MarkerSearchOutcome] = {}

    def _read_rows(self) -> list[Candidate]:
        if not self._path.exists():
            return []
        rows: list[Candidate] = []
        quarantine = self._path.with_suffix(self._path.suffix + ".corrupt")
        if self._quarantine_seen is None:
            self._quarantine_seen = (
                set(quarantine.read_text(encoding="utf-8").splitlines())
                if quarantine.exists()
                else set()
            )
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rows.append(Candidate.model_validate(json.loads(line), strict=False))
                except (json.JSONDecodeError, TypeError):
                    if line not in self._quarantine_seen:
                        with quarantine.open("a", encoding="utf-8") as handle:
                            handle.write(line + "\n")
                        self._quarantine_seen.add(line)
                        self.quarantined_rows += 1
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
        if has_local_artifact(current):
            return True
        return self.marker_artifact(candidate_id) is not None

    def resume_decision(self, candidate_id: str) -> ResumeDecision:
        """Resolve resume behavior from persisted state and one marker lookup."""
        persisted = self.resume(candidate_id)
        local_artifact = has_local_artifact(persisted)
        if persisted is None or not local_artifact:
            artifacts_present = self.existing_artifact(candidate_id)
            marker_search_available = not self.marker_search_unavailable(candidate_id)
        else:
            artifacts_present = True
            marker_search_available = True
        return decide_resume(
            persisted,
            artifacts_present=artifacts_present,
            marker_search_available=marker_search_available,
            marker_search_orphaned=self.marker_search_orphaned(candidate_id),
        )

    def marker_artifact(self, candidate_id: str) -> MarkerArtifact | None:
        """Search the target repository for one candidate's stable marker."""
        if candidate_id in self._marker_results:
            return self._marker_results[candidate_id]
        if self._marker_search is None:
            self._marker_outcomes[candidate_id] = MarkerSearchOutcome.UNCONFIGURED
            self._marker_results[candidate_id] = None
            return None
        try:
            result = self._marker_search(f"<!-- devin-remediation-id: {candidate_id} -->")
            self._marker_results[candidate_id] = result
            self._marker_outcomes[candidate_id] = (
                MarkerSearchOutcome.FOUND if result is not None else MarkerSearchOutcome.ABSENT
            )
            return result
        except ValueError:
            self._marker_outcomes[candidate_id] = MarkerSearchOutcome.ORPHANED
            self._marker_results[candidate_id] = None
            return None
        except Exception:
            self.marker_search_failed = True
            self._marker_outcomes[candidate_id] = MarkerSearchOutcome.FAILED
            self._marker_results[candidate_id] = None
            return None

    def marker_exists(self, candidate_id: str) -> bool:
        """Return whether a unique target artifact exists for a candidate."""
        return self.marker_artifact(candidate_id) is not None

    def marker_search_unavailable(self, candidate_id: str) -> bool:
        """Return whether the configured marker lookup failed for one candidate."""
        if candidate_id not in self._marker_results:
            self.marker_exists(candidate_id)
        return self._marker_outcomes.get(candidate_id) in {
            MarkerSearchOutcome.FAILED,
            MarkerSearchOutcome.UNCONFIGURED,
        }

    def marker_search_orphaned(self, candidate_id: str) -> bool:
        """Return whether marker search found an ambiguous or malformed artifact."""
        if candidate_id not in self._marker_results:
            self.marker_artifact(candidate_id)
        return self._marker_outcomes.get(candidate_id) is MarkerSearchOutcome.ORPHANED

    def marker_search_outcome(self, candidate_id: str) -> MarkerSearchOutcome:
        """Return the recorded marker lookup outcome for one candidate."""
        if candidate_id not in self._marker_results:
            self.marker_artifact(candidate_id)
        return self._marker_outcomes[candidate_id]

    def _append_locked(self, candidate: Candidate) -> None:
        """Append one row while the caller owns the state lock."""
        latest = self.latest().get(candidate.candidate_id)
        if latest is not None and latest.model_dump(mode="json") == candidate.model_dump(
            mode="json"
        ):
            return
        line = json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def append(self, candidate: Candidate) -> None:
        """Append one candidate lifecycle row after rereading current state."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            self._append_locked(candidate)

    def append_if_new_artifact(self, candidate: Candidate) -> bool:
        """Atomically reserve a candidate before the first artifact write."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        marker_exists = self.marker_exists(candidate.candidate_id)
        if (
            self.marker_search_unavailable(candidate.candidate_id)
            or self.marker_search_orphaned(candidate.candidate_id)
        ) and not has_local_artifact(self.latest().get(candidate.candidate_id)):
            return False
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            current = self.latest().get(candidate.candidate_id)
            if has_local_artifact(current) or marker_exists:
                return False
            self._append_locked(candidate)
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


__all__ = [
    "CandidateStateStore",
    "ResumeAction",
    "ResumeDecision",
    "StatePreservationError",
    "MarkerSearchOutcome",
    "MarkerArtifact",
    "decide_resume",
    "has_local_artifact",
    "github_marker_search",
]
