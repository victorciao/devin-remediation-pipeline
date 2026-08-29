"""Layer 3 KPI computation and burn-down validity."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from pipeline.config import PipelineConfig
from pipeline.schemas import Action, Candidate, CandidateState, EventRecord, Lane, ReasonCode


@dataclass(frozen=True)
class NotApplicable:
    """A KPI value unavailable because its baseline capability was absent."""

    reason: ReasonCode


BurnDownValue: TypeAlias = int | NotApplicable


@dataclass(frozen=True)
class BurnDown:
    """Burn-down denominator and progress for one lane."""

    denominator: BurnDownValue
    completed: BurnDownValue
    remaining: BurnDownValue


def _baseline_key(lane: Lane) -> str:
    return {
        Lane.CODEQL: "codeql_open_alerts",
        Lane.SKIPPED_TESTS: "skipped_tests",
        Lane.DEPRECATIONS: "deprecations",
    }[lane]


def compute_burndown(
    candidates: list[Candidate],
    baseline: dict[str, object],
) -> dict[Lane, BurnDown]:
    """Compute burn-down only for lanes with valid Phase 0c baselines."""
    valid = baseline.get("baseline_valid_lanes")
    valid_lanes = {str(item) for item in valid} if isinstance(valid, list) else set()
    result: dict[Lane, BurnDown] = {}
    for lane in Lane:
        if lane.value not in valid_lanes:
            reasons = baseline.get("baseline_invalid_reasons")
            reason = ReasonCode.CAPABILITY_UNAVAILABLE
            if isinstance(reasons, dict):
                raw_reason = reasons.get(lane.value)
                if raw_reason == ReasonCode.CAPABILITY_UNAVAILABLE.value:
                    reason = ReasonCode.CAPABILITY_UNAVAILABLE
            unavailable = NotApplicable(reason)
            result[lane] = BurnDown(unavailable, unavailable, unavailable)
            continue
        totals = baseline.get("totals")
        total_value = totals.get(_baseline_key(lane), 0) if isinstance(totals, dict) else 0
        denominator = int(total_value)
        lane_rows = [candidate for candidate in candidates if candidate.lane is lane]
        progress = sum(
            candidate.state is CandidateState.CONVERGED
            or (
                candidate.state is CandidateState.TERMINAL
                and candidate.reason is ReasonCode.STALE_SKIP
            )
            for candidate in lane_rows
        )
        result[lane] = BurnDown(denominator, progress, max(denominator - progress, 0))
    return result


def compute_kpis(
    candidates: list[Candidate],
    events: list[EventRecord],
    baseline: dict[str, object],
    config: PipelineConfig,
) -> dict[str, float | int | NotApplicable]:
    """Compute the §11 KPI set from candidate and event evidence."""
    actions = Counter(candidate.action for candidate in candidates)
    unresolved = sum(event.reason is ReasonCode.DISAGREEMENT_UNRESOLVED for event in events)
    verified = sum(event.red_baseline is not None for event in events)
    passing = sum(
        event.red_baseline is not None and event.reason is not ReasonCode.DISAGREEMENT_UNRESOLVED
        for event in events
    )
    session_failures = sum(
        event.reason
        in {
            ReasonCode.SESSION_CEILING,
            ReasonCode.ROLE_COLLISION,
        }
        for event in events
    )
    burndown = compute_burndown(candidates, baseline)
    valid_rows = [value for value in burndown.values() if isinstance(value.denominator, int)]
    merge_rate = _merge_rate(events)
    pr_events = [event for event in events if event.pr_url is not None]
    merged_clean = sum(
        event.terminal_outcome is CandidateState.CONVERGED and event.test_exempt_reason is None
        for event in pr_events
    )
    rejected = sum(event.reason is ReasonCode.DISAGREEMENT_UNRESOLVED for event in pr_events)
    edited = max(len(pr_events) - merged_clean - rejected, 0)
    test_applicable = [event for event in events if event.action is Action.OPEN_PR]
    sessions_by_role = {
        "planner": sum(event.planner_session_id is not None for event in events),
        "implementer": sum(event.implementer_session_id is not None for event in events),
        "reviewer": sum(event.reviewer_session_id is not None for event in events),
    }
    return {
        "candidates_seen": len(candidates),
        "active": sum(
            candidate.state not in {CandidateState.TERMINAL, CandidateState.CONVERGED}
            for candidate in candidates
        ),
        "completed": sum(
            candidate.state in {CandidateState.TERMINAL, CandidateState.CONVERGED}
            for candidate in candidates
        ),
        "dispatched_pr": actions[Action.OPEN_PR],
        "dispatched_issue": actions[Action.OPEN_ISSUE],
        "deferred": actions[Action.DEFERRED],
        "verification_pass_rate": passing / verified if verified else 0.0,
        "test_inclusion_rate": (
            sum(event.test_added is True for event in test_applicable) / len(test_applicable)
            if test_applicable
            else 0.0
        ),
        "criterion_coverage_rate": _criterion_coverage(candidates, events),
        "expected_reason_match_rate": _expected_reason_match_rate(events),
        "disagreement_unresolved_rate": unresolved / len(events) if events else 0.0,
        "session_failure_rate": session_failures / len(events) if events else 0.0,
        "implementer_test_edit_violation_rate": (
            sum(event.reason is ReasonCode.IMPLEMENTER_TEST_EDIT for event in events) / len(events)
            if events
            else 0.0
        ),
        "sessions_per_candidate_planner": sessions_by_role["planner"] / len(candidates)
        if candidates
        else 0.0,
        "sessions_per_candidate_implementer": sessions_by_role["implementer"] / len(candidates)
        if candidates
        else 0.0,
        "sessions_per_candidate_reviewer": sessions_by_role["reviewer"] / len(candidates)
        if candidates
        else 0.0,
        "burn_down_denominators": sum(
            value.denominator for value in valid_rows if isinstance(value.denominator, int)
        ),
        "merge_rate": merge_rate,
        "merged_clean": merged_clean,
        "edited": edited,
        "rejected": rejected,
        "merge_rate_alert": int(merge_rate < config.merge_rate_floor),
        "session_failure_alert": int(
            (session_failures / len(events) if events else 0.0) > config.session_failure_ceiling
        ),
    }


def _merge_rate(events: list[EventRecord]) -> float:
    """Compute the merged-clean/edited/rejected aggregate merge rate."""
    pr_events = [event for event in events if event.pr_url is not None]
    merged = sum(event.terminal_outcome is CandidateState.CONVERGED for event in pr_events)
    return merged / len(pr_events) if pr_events else 0.0


def _criterion_coverage(candidates: list[Candidate], events: list[EventRecord]) -> float:
    """Return the rate of candidates with reviewer-owned test evidence."""
    del candidates
    applicable = [event for event in events if event.action is not Action.LOG_ONLY]
    covered = sum(event.test_added is True for event in applicable)
    return covered / len(applicable) if applicable else 0.0


def _expected_reason_match_rate(events: list[EventRecord]) -> float:
    """Return expected-red-reason agreement over recorded item outcomes."""
    outcomes = [
        outcome
        for event in events
        if event.red_baseline is not None
        for outcome in event.red_baseline.per_item_outcomes
        if outcome.expected_reason_match is not None
    ]
    return (
        sum(outcome.expected_reason_match is True for outcome in outcomes) / len(outcomes)
        if outcomes
        else 0.0
    )


def render_kpi_report(
    candidates: list[Candidate],
    events: list[EventRecord],
    baseline: dict[str, object],
    config: PipelineConfig,
) -> str:
    """Render the rolling KPI report with visually distinct alert lines."""
    metrics = compute_kpis(candidates, events, baseline, config)
    lines = ["# Remediation KPI rollup", ""]
    for name, metric_value in metrics.items():
        label = name.replace("_", " ").title()
        if name.endswith("_alert") and metric_value:
            lines.append(f"> **ALERT: {label}**")
        elif name.endswith("_alert"):
            continue
        else:
            lines.append(f"- **{label}:** {metric_value}")
    lines.extend(["", "## Burn-down", ""])
    for lane, value in compute_burndown(candidates, baseline).items():
        if isinstance(value.denominator, NotApplicable):
            lines.append(f"- **{lane.value}:** n/a ({value.denominator.reason.value})")
        else:
            lines.append(
                f"- **{lane.value}:** {value.completed}/{value.denominator} complete; "
                f"{value.remaining} remaining"
            )
    lines.append("")
    return "\n".join(lines)


def write_kpi_report(
    path: Path,
    candidates: list[Candidate],
    events: list[EventRecord],
    baseline: dict[str, object],
    config: PipelineConfig,
) -> None:
    """Write the rolling KPI report to the configured local sink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_kpi_report(candidates, events, baseline, config),
        encoding="utf-8",
    )


__all__ = [
    "BurnDown",
    "BurnDownValue",
    "NotApplicable",
    "compute_burndown",
    "compute_kpis",
    "render_kpi_report",
    "write_kpi_report",
]
