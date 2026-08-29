"""Layer 1 structured JSONL observability events."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pipeline.schemas import Candidate, CandidateState, EventRecord


class EventLog:
    """Append-only source-of-truth event log."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: EventRecord) -> None:
        """Append one validated event as JSONL."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")

    def read(self) -> list[EventRecord]:
        """Read all events without conflating them with candidate state."""
        if not self._path.exists():
            return []
        return [
            EventRecord.model_validate(json.loads(line), strict=False)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def event_from_candidate(candidate: Candidate, *, run_id: str) -> EventRecord:
    """Project all observability fields available on a candidate."""
    return EventRecord(
        run_id=run_id,
        lane=candidate.lane,
        candidate_id=candidate.candidate_id,
        gate_passed=candidate.gate_passed,
        failed_gate=candidate.failed_gate,
        gate_results=candidate.gate_results,
        score=candidate.score,
        business_impact=candidate.business_impact,
        verifiability=candidate.verifiability,
        automatability=candidate.automatability,
        signal_quality=candidate.signal_quality,
        risk=candidate.risk,
        factor_rows=candidate.factor_rows,
        tier=candidate.tier,
        action=candidate.action,
        planner_session_id=candidate.planner_session_id,
        implementer_session_id=candidate.implementer_session_id,
        reviewer_session_id=candidate.reviewer_session_id,
        iterations=candidate.iterations,
        pr_url=candidate.pr_url,
        issue_url=candidate.issue_url,
        test_added=candidate.test_added,
        test_paths=candidate.test_paths,
        test_author=candidate.test_author,
        test_exempt_reason=candidate.test_exempt_reason,
        terminal_outcome=(
            candidate.state
            if candidate.state in {CandidateState.TERMINAL, CandidateState.CONVERGED}
            else None
        ),
        reason=candidate.reason,
        red_baseline=candidate.red_baseline,
        enclosed_tests=candidate.enclosed_tests,
        parametrized=candidate.parametrized,
        collects_single_item=candidate.collects_single_item,
        lifted_markers=candidate.lifted_markers,
        related_candidate_id=candidate.related_candidate_id,
    )


def append_candidate_events(
    log: EventLog,
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    token_login: str | None = None,
    token_scopes: Iterable[str] = (),
) -> None:
    """Append one Layer 1 event for each candidate."""
    scopes = list(token_scopes)
    for candidate in candidates:
        log.append(
            event_from_candidate(candidate, run_id=run_id).model_copy(
                update={"token_login": token_login, "token_scopes": scopes}
            )
        )


__all__ = ["EventLog", "append_candidate_events", "event_from_candidate"]
