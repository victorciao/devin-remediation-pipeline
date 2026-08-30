"""Layer 2 per-run summary reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pipeline.schemas import Action, Candidate, CandidateState


def render_run_report(
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    capability_notes: Iterable[str] = (),
) -> str:
    """Render a deterministic per-run summary in Markdown."""
    rows = list(candidates)
    notes = list(capability_notes)
    note_lines = [f"- {note}" for note in notes] if notes else ["- None"]
    gated = Counter(
        candidate.reason.value
        for candidate in rows
        if candidate.gate_passed is False and candidate.reason is not None
    )
    dispatched_states = {
        CandidateState.DISPATCHING,
        CandidateState.ISSUE_CREATED,
        CandidateState.PR_CREATED,
        CandidateState.ISSUE_PATCHED,
        CandidateState.COMMENT_CREATED,
        CandidateState.CONVERGED,
        CandidateState.TERMINAL,
    }
    tiers = Counter(
        candidate.tier.value
        for candidate in rows
        if candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
        and candidate.state in dispatched_states
        and candidate.tier is not None
    )
    deferred_by_reason = Counter(
        candidate.reason.value
        for candidate in rows
        if candidate.state is CandidateState.DEFERRED and candidate.reason is not None
    )
    deferred_other = sum(
        count
        for reason, count in deferred_by_reason.items()
        if reason not in {"budget_overflow", "session_ceiling"}
    )
    links = [
        f"- `{candidate.candidate_id}`: PR={candidate.pr_url or 'n/a'}, "
        f"issue={candidate.issue_url or 'n/a'}"
        for candidate in rows
        if candidate.pr_url is not None or candidate.issue_url is not None
    ]
    gated_lines = [f"- `{reason}`: {count}" for reason, count in sorted(gated.items())]
    tier_lines = [f"- `{tier}`: {count}" for tier, count in sorted(tiers.items())]
    return "\n".join(
        [
            f"# Run {run_id}",
            "",
            f"- Candidates seen: {len(rows)}",
            f"- Scored: {sum(candidate.score is not None for candidate in rows)}",
            f"- Deferred by budget: {deferred_by_reason.get('budget_overflow', 0)}",
            f"- Deferred by session ceiling: {deferred_by_reason.get('session_ceiling', 0)}",
            f"- Deferred by capability/other: {deferred_other}",
            "",
            "## Capability notes",
            *note_lines,
            "",
            "## Gated out",
            *(gated_lines or ["- None"]),
            "",
            "## Dispatched by tier",
            *(tier_lines or ["- None"]),
            "",
            "## Artifact links",
            *(links or ["- None"]),
            "",
        ]
    )


def write_run_report(
    path: Path,
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    capability_notes: Iterable[str] = (),
) -> None:
    """Write a Layer 2 report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_run_report(candidates, run_id=run_id, capability_notes=capability_notes),
        encoding="utf-8",
    )


__all__ = ["render_run_report", "write_run_report"]
