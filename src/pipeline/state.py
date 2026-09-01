"""Append-only candidate state and resume storage."""

from __future__ import annotations

import fcntl
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from pipeline.dedupe import find_drift_match
from pipeline.schemas import Candidate, CandidateState

DURABLE_IDENTITY_FIELDS = (
    "pr_url",
    "pr_number",
    "issue_url",
    "issue_number",
    "head_sha",
    "merged_at",
)
SETTLED_STATES = frozenset(
    {
        CandidateState.MERGED,
        CandidateState.AWAITING_HUMAN_MERGE,
        CandidateState.TERMINAL,
    }
)


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


class MarkerIndexBuildError(RuntimeError):
    """Raised when the batched marker index cannot be built safely."""


class DuplicateMarkerIndexError(ValueError):
    """Raised with the usable index and candidate IDs carrying duplicate issues."""

    def __init__(self, index: dict[str, MarkerArtifact], duplicates: set[str]) -> None:
        super().__init__("marker index contains duplicate candidate IDs")
        self.index = index
        self.duplicates = duplicates


_MARKER_PATTERN = re.compile(r"<!-- devin-remediation-id: ([^\s<>]+) -->")
_MARKER_INDEX_PAGE_SIZE = 100
_MARKER_INDEX_MAX_PAGES = 10


def _marker_failure_detail(exc: Exception) -> str:
    """Return a sanitized marker-search failure detail."""
    status_code = getattr(exc, "status_code", None)
    message = str(exc)
    if status_code is not None:
        return f"HTTP {status_code}: {message}"
    return message or type(exc).__name__


def build_marker_index(search_page: Callable[[int], object]) -> dict[str, MarkerArtifact]:
    """Build an issue-only candidate marker index from bounded search pages."""
    index: dict[str, MarkerArtifact] = {}
    duplicates: set[str] = set()
    for page in range(1, _MARKER_INDEX_MAX_PAGES + 1):
        response = search_page(page)
        if not isinstance(response, dict):
            raise MarkerIndexBuildError("marker search response is not an object")
        items = response.get("items")
        if not isinstance(items, list):
            raise MarkerIndexBuildError("marker search response lacks items")
        for item in items:
            if not isinstance(item, dict) or isinstance(item.get("pull_request"), dict):
                continue
            url = item.get("html_url")
            number = item.get("number")
            body = item.get("body")
            if (
                not isinstance(url, str)
                or not url
                or "/pull/" in url
                or not isinstance(number, int)
            ):
                continue
            if not isinstance(body, str):
                continue
            artifact = MarkerArtifact(number=number, url=url, is_pull_request=False)
            for match in _MARKER_PATTERN.finditer(body):
                candidate_id = match.group(1)
                previous = index.get(candidate_id)
                if previous is not None and previous.number != number:
                    duplicates.add(candidate_id)
                    index.pop(candidate_id, None)
                elif candidate_id not in duplicates:
                    index[candidate_id] = artifact
        if len(items) < _MARKER_INDEX_PAGE_SIZE:
            if duplicates:
                raise DuplicateMarkerIndexError(index, duplicates)
            return index
    raise MarkerIndexBuildError(
        f"marker search pagination exceeded {_MARKER_INDEX_MAX_PAGES} pages"
    )


@dataclass(frozen=True)
class ResumeDecision:
    """Pure decision for resuming one persisted candidate."""

    action: ResumeAction
    step: str | None = None


def has_local_artifact(candidate: Candidate | None) -> bool:
    """Return whether a persisted row proves an artifact already exists."""
    return candidate is not None and any(
        link is not None for link in (candidate.issue_url, candidate.pr_url)
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
        CandidateState.MERGED,
        CandidateState.AWAITING_HUMAN_MERGE,
        CandidateState.TERMINAL,
    }:
        if artifacts_present or has_local_artifact(persisted):
            return ResumeDecision(ResumeAction.SKIP)
        return ResumeDecision(ResumeAction.RESUME_AT_STEP, "publication")
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
        marker_index_search: Callable[[int], object] | None = None,
        require_marker_proof: bool = False,
        artifact_simulated: bool = False,
    ) -> None:
        self._path = path
        self._marker_search = marker_search
        self._marker_index_search = marker_index_search
        self._require_marker_proof = require_marker_proof
        self._artifact_simulated = artifact_simulated
        self.marker_search_failed = False
        self.marker_search_failure_detail: str | None = None
        self.quarantined_rows = 0
        self._quarantine_seen: set[str] | None = None
        self._marker_results: dict[str, MarkerArtifact | None] = {}
        self._marker_outcomes: dict[str, MarkerSearchOutcome] = {}
        self._marker_index: dict[str, MarkerArtifact] | None = None
        self._marker_index_orphans: set[str] = set()
        self._marker_index_failed = False

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
        if self._marker_index_search is not None:
            if self._marker_index is None:
                try:
                    self._marker_index = build_marker_index(self._marker_index_search)
                except DuplicateMarkerIndexError as exc:
                    self._marker_index = exc.index
                    self._marker_index_orphans = exc.duplicates
                except Exception as exc:
                    self.marker_search_failed = True
                    self._marker_index = {}
                    self._marker_index_failed = True
                    self.marker_search_failure_detail = _marker_failure_detail(exc)
                    self._marker_outcomes[candidate_id] = MarkerSearchOutcome.FAILED
                    self._marker_results[candidate_id] = None
                    return None
            if self._marker_index_failed:
                self._marker_outcomes[candidate_id] = MarkerSearchOutcome.FAILED
                self._marker_results[candidate_id] = None
                return None
            if candidate_id in self._marker_index_orphans:
                self._marker_outcomes[candidate_id] = MarkerSearchOutcome.ORPHANED
                self._marker_results[candidate_id] = None
                return None
            result = self._marker_index.get(candidate_id)
            self._marker_results[candidate_id] = result
            self._marker_outcomes[candidate_id] = (
                MarkerSearchOutcome.FOUND if result is not None else MarkerSearchOutcome.ABSENT
            )
            return result
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
        except Exception as exc:
            self.marker_search_failed = True
            self.marker_search_failure_detail = _marker_failure_detail(exc)
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
        } or (
            self._marker_outcomes.get(candidate_id) is MarkerSearchOutcome.UNCONFIGURED
            and self._require_marker_proof
        )

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
        if self._artifact_simulated:
            candidate = candidate.model_copy(update={"artifact_simulated": True})
        latest = self.latest().get(candidate.candidate_id)
        if latest is not None:
            previous_row = latest.model_dump(mode="json")
            current_row = candidate.model_dump(mode="json")
            for field in DURABLE_IDENTITY_FIELDS:
                if previous_row.get(field) is not None and current_row.get(field) is None:
                    raise StatePreservationError(f"state append discarded persisted {field}")
            if latest.state in SETTLED_STATES and has_local_artifact(latest):
                if candidate.state not in SETTLED_STATES or (
                    latest.state is CandidateState.MERGED
                    and candidate.state is not CandidateState.MERGED
                ):
                    raise StatePreservationError(
                        f"state append attempted transition "
                        f"{latest.state.value} -> {candidate.state.value}"
                    )
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
        """Append a dispatch row only when no artifact already exists for it."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with lock_path.open("a", encoding="utf-8") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            # The absence proof must be made while the state lock is held.
            self._marker_results.pop(candidate.candidate_id, None)
            self._marker_outcomes.pop(candidate.candidate_id, None)
            marker = self.marker_artifact(candidate.candidate_id)
            current = self.latest().get(candidate.candidate_id)
            if has_local_artifact(current) or marker is not None:
                return False
            if self.marker_search_unavailable(
                candidate.candidate_id
            ) or self.marker_search_orphaned(candidate.candidate_id):
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
    "DURABLE_IDENTITY_FIELDS",
    "SETTLED_STATES",
    "ResumeAction",
    "ResumeDecision",
    "StatePreservationError",
    "MarkerSearchOutcome",
    "MarkerArtifact",
    "MarkerIndexBuildError",
    "DuplicateMarkerIndexError",
    "build_marker_index",
    "decide_resume",
    "has_local_artifact",
    "github_marker_search",
]
