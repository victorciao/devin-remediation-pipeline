"""Layer 2 per-run summary reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pipeline.config import Mode
from pipeline.schemas import Action, Candidate, CandidateState


def _criterion_satisfied(candidate: Candidate) -> str:
    """Return the observed criterion verdict for one candidate."""
    evidence = candidate.criterion_evidence
    return "n/a" if evidence is None else str(evidence.satisfied)


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
        CandidateState.SESSION_DONE,
        CandidateState.VERIFIED,
        CandidateState.PR_CREATED,
        CandidateState.AWAITING_HUMAN_MERGE,
        CandidateState.MERGED,
        CandidateState.TERMINAL,
    }
    tiers = Counter(
        candidate.tier.value
        for candidate in rows
        if candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
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
    marker_outcomes = Counter(
        candidate.marker_search_outcome
        for candidate in rows
        if candidate.marker_search_outcome is not None
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
        if candidate.state is CandidateState.TERMINAL and candidate.session_id is not None
    ]
    escalated_ids = {candidate.candidate_id for candidate in escalated}
    low_tier_count = sum(
        candidate.tier is not None and candidate.tier.value == "low" for candidate in rows
    )
    gated_count = sum(candidate.gate_passed is False for candidate in rows)
    unpublished_count = sum(candidate.state is CandidateState.DISPATCHING for candidate in rows)
    safety_undetermined_count = sum(
        candidate.marker_search_outcome in {"failed", "orphaned", "unconfigured"}
        and candidate.issue_url is None
        and candidate.pr_url is None
        for candidate in rows
    )
    accounted = {
        candidate.candidate_id
        for candidate in rows
        if candidate.state is CandidateState.DEFERRED
        or candidate.gate_passed is False
        or candidate.candidate_id in escalated_ids
        or (
            candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
            and (
                candidate.state in dispatched_states
                or candidate.state is CandidateState.DISPATCHING
            )
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
                f"  criterion={candidate.success_criterion or 'n/a'}",
                f"  head_branch={candidate.head_branch or 'n/a'}; "
                f"session={candidate.session_id or 'n/a'}",
            ]
        )
    unaccounted = len(rows) - len(accounted)
    marker_lines = [
        f"- `{outcome}`: {count}" for outcome, count in sorted(marker_outcomes.items())
    ] or ["- None"]
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
            "## Marker search outcomes",
            *marker_lines,
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
            f"- Publication safety undetermined: {safety_undetermined_count}",
            f"- Session attempts: {sum(candidate.session_attempts for candidate in rows)}",
            f"- Sessions created: {sum(candidate.session_id is not None for candidate in rows)}",
            f"- Awaiting human merge: "
            f"{sum(candidate.state is CandidateState.AWAITING_HUMAN_MERGE for candidate in rows)}",
            f"- Merged: {sum(candidate.state is CandidateState.MERGED for candidate in rows)}",
            "",
            "## Lane success criteria",
            *(
                [
                    f"- `{candidate.candidate_id}` ({candidate.lane.value}): "
                    f"{candidate.success_criterion} — satisfied="
                    f"{_criterion_satisfied(candidate)}"
                    for candidate in rows
                    if candidate.success_criterion is not None
                ]
                or ["- None"]
            ),
            "",
            "## Check runs",
            *(
                [
                    f"- `{candidate.candidate_id}`: "
                    + ", ".join(
                        f"{check.name}={check.conclusion or check.status or 'pending'}"
                        for check in candidate.check_run_conclusions
                    )
                    for candidate in rows
                    if candidate.check_run_conclusions
                ]
                or ["- None"]
            ),
            "",
            "## Failed verification escalation",
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
