"""Layer 2 per-run summary reports."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from pipeline.schemas import Action, Candidate, CandidateState


def render_run_report(candidates: Iterable[Candidate], *, run_id: str) -> str:
    """Render a deterministic per-run summary in Markdown."""
    rows = list(candidates)
    gated = Counter(
        candidate.reason.value
        for candidate in rows
        if candidate.gate_passed is False and candidate.reason is not None
    )
    tiers = Counter(
        candidate.tier.value
        for candidate in rows
        if candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE} and candidate.tier is not None
    )
    deferred = sum(candidate.state is CandidateState.DEFERRED for candidate in rows)
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
            f"- Deferred by budget: {deferred}",
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
) -> None:
    """Write a Layer 2 report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_run_report(candidates, run_id=run_id), encoding="utf-8")


__all__ = ["render_run_report", "write_run_report"]
