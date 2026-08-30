"""Layer 2 per-run summary reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pipeline.config import Mode
from pipeline.schemas import Action, Candidate, CandidateState


def render_run_report(
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    capability_notes: Iterable[str] = (),
    mode: Mode = Mode.SIMULATE,
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
        if candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE, Action.REVIEWER_ONLY_DIFF}
        and candidate.state in dispatched_states
        and candidate.state is not CandidateState.DEFERRED
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
    escalated = [
        candidate
        for candidate in rows
        if candidate.state is CandidateState.TERMINAL and candidate.reviewer_session_id is not None
    ]
    escalated_ids = {candidate.candidate_id for candidate in escalated}
    low_tier_count = sum(
        candidate.tier is not None and candidate.tier.value == "low" for candidate in rows
    )
    gated_count = sum(candidate.gate_passed is False for candidate in rows)
    unpublished_count = sum(candidate.state is CandidateState.DISPATCHING for candidate in rows)
    accounted = {
        candidate.candidate_id
        for candidate in rows
        if candidate.state is CandidateState.DEFERRED
        or candidate.gate_passed is False
        or candidate.candidate_id in escalated_ids
        or (
            candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE, Action.REVIEWER_ONLY_DIFF}
            and candidate.state in dispatched_states
        )
        or (
            candidate.tier is not None
            and candidate.tier.value == "low"
            and candidate.state is CandidateState.TERMINAL
        )
    }
    escalated_lines: list[str] = []
    for candidate in escalated:
        escalated_lines.extend(
            [
                f"- `{candidate.candidate_id}` ({candidate.lane.value}, "
                f"{candidate.tier.value if candidate.tier else 'n/a'}): "
                f"reason={candidate.reason.value if candidate.reason else 'n/a'}",
                f"  disagreement_summary={candidate.disagreement_summary or 'n/a'}",
                f"  head_branch={candidate.head_branch or 'n/a'}; "
                f"planner={candidate.planner_session_id or 'n/a'}; "
                f"implementer={candidate.implementer_session_id or 'n/a'}; "
                f"reviewer={candidate.reviewer_session_id or 'n/a'}",
            ]
        )
    unaccounted = len(rows) - len(accounted)
    return "\n".join(
        [
            f"# Run {run_id}",
            "",
            f"- mode: {mode.value}",
            f"- Candidates seen: {len(rows)}",
            f"- Scored: {sum(candidate.score is not None for candidate in rows)}",
            f"- Deferred by budget: {deferred_by_reason.get('budget_overflow', 0)}",
            f"- Deferred by session ceiling: {deferred_by_reason.get('session_ceiling', 0)}",
            f"- Deferred by capability/other: {deferred_other}",
            *[
                f"- Deferred ({reason}): {count}"
                for reason, count in sorted(deferred_by_reason.items())
            ],
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
            "## Routing totals",
            f"- Low-tier candidates: {low_tier_count}",
            f"- Gated candidates: {gated_count}",
            f"- Reached dispatching but unpublished: {unpublished_count}",
            f"- Role attempts: {sum(sum(candidate.role_attempts.values()) for candidate in rows)}",
            f"- Iterations: {sum(candidate.iterations for candidate in rows)}",
            "",
            "## Failed review escalation",
            *(escalated_lines or ["- None"]),
            "",
            "## Accounting",
            f"- Unaccounted: {unaccounted}",
            "",
            "",
        ]
    )


def write_run_report(
    path: Path,
    candidates: Iterable[Candidate],
    *,
    run_id: str,
    capability_notes: Iterable[str] = (),
    mode: Mode = Mode.SIMULATE,
) -> None:
    """Write a Layer 2 report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_run_report(
            candidates,
            run_id=run_id,
            capability_notes=capability_notes,
            mode=mode,
        ),
        encoding="utf-8",
    )


__all__ = ["render_run_report", "write_run_report"]
