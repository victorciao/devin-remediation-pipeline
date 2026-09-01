"""Layer 1 structured JSONL observability events."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from pipeline.schemas import Candidate, CandidateState, EventRecord, RunEventRecord


class EventLog:
    """Append-only source-of-truth event log."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, event: EventRecord | RunEventRecord) -> None:
        """Append one validated event as JSONL."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True) + "\n")

    def read(self) -> list[EventRecord]:
        """Read all events without conflating them with candidate state."""
        if not self._path.exists():
            return []
        events: list[EventRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("event_type") in {
                "run_capabilities",
                "ci_mode_transition",
                "marker_search_failure",
            }:
                continue
            events.append(EventRecord.model_validate(payload, strict=False))
        return events

    def read_run_events(self) -> list[RunEventRecord]:
        """Read run-level capability evidence from the event stream."""
        if not self._path.exists():
            return []
        events: list[RunEventRecord] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict) and payload.get("event_type") in {
                "run_capabilities",
                "ci_mode_transition",
                "marker_search_failure",
            }:
                events.append(RunEventRecord.model_validate(payload, strict=False))
        return events


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
        session_id=candidate.session_id,
        session_attempts=candidate.session_attempts,
        success_criterion=candidate.success_criterion,
        criterion_evidence=candidate.criterion_evidence,
        suite_scope=candidate.suite_scope,
        check_run_conclusions=candidate.check_run_conclusions,
        pr_url=candidate.pr_url,
        issue_url=candidate.issue_url,
        issue_adopted=candidate.issue_adopted,
        issue_number=candidate.issue_number,
        pr_number=candidate.pr_number,
        merged_at=candidate.merged_at,
        merge_verified=candidate.merge_verified,
        ci_evidence_mode=candidate.ci_evidence_mode,
        test_added=candidate.test_added,
        test_nodeid=candidate.test_nodeid,
        test_paths=candidate.test_paths,
        test_author=candidate.test_author,
        test_exempt_reason=candidate.test_exempt_reason,
        terminal_outcome=(
            candidate.state
            if candidate.state
            in {
                CandidateState.TERMINAL,
                CandidateState.MERGED,
                CandidateState.AWAITING_HUMAN_MERGE,
            }
            else None
        ),
        reason=candidate.reason,
        reason_detail=candidate.reason_detail,
        red_baseline=candidate.red_baseline,
        enclosed_tests=candidate.enclosed_tests,
        parametrized=candidate.parametrized,
        collects_single_item=candidate.collects_single_item,
        lifted_markers=candidate.lifted_markers,
        related_candidate_id=candidate.related_candidate_id,
        marker_search_outcome=candidate.marker_search_outcome,
        artifact_simulated=candidate.artifact_simulated,
    )


def append_candidate_events(
    log: EventLog,
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    token_login: str | None = None,
    token_scopes: Iterable[str] = (),
    run_events: Iterable[RunEventRecord] = (),
) -> None:
    """Append candidate events and optional run-level capability evidence."""
    if token_login is not None or token_scopes:
        log.append(
            RunEventRecord(
                run_id=run_id,
                token_login=token_login,
                token_scopes=list(token_scopes),
            )
        )
    for run_event in run_events:
        log.append(run_event)
    for candidate in candidates:
        log.append(event_from_candidate(candidate, run_id=run_id))


__all__ = ["EventLog", "append_candidate_events", "event_from_candidate"]
