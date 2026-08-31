"""Layer 3 KPI computation and burn-down validity."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

from pipeline.config import Mode, PipelineConfig
from pipeline.schemas import Candidate, CandidateState, EventRecord, Lane, ReasonCode
from pipeline.state import has_local_artifact


@dataclass(frozen=True)
class NotApplicable:
    """A KPI value unavailable because its baseline capability was absent."""

    reason: ReasonCode


BurnDownValue: TypeAlias = int | NotApplicable
KpiValue: TypeAlias = float | int | None | NotApplicable | dict[str, int]


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
            has_local_artifact(candidate)
            and (
                candidate.state
                in {
                    CandidateState.PR_CREATED,
                    CandidateState.AWAITING_HUMAN_MERGE,
                    CandidateState.MERGED,
                }
                or (
                    candidate.state is CandidateState.TERMINAL
                    and candidate.reason is ReasonCode.STALE_SKIP
                )
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
) -> dict[str, KpiValue]:
    """Compute the §11 KPI set from candidate and event evidence."""
    dispatched_pr = sum(
        candidate.pr_url is not None and candidate.state is not CandidateState.DEFERRED
        for candidate in candidates
    )
    dispatched_issue = sum(
        candidate.issue_url is not None and candidate.state is not CandidateState.DEFERRED
        for candidate in candidates
    )
    session_events = [event for event in events if event.session_id is not None]
    verification_passes = sum(
        event.criterion_evidence is not None and event.criterion_evidence.satisfied is True
        for event in session_events
    )
    stale_skips = sum(event.reason is ReasonCode.STALE_SKIP for event in events)
    session_failures = sum(
        event.reason
        in {
            ReasonCode.SESSION_CEILING,
            ReasonCode.SESSION_FAILED,
            ReasonCode.SESSION_BLOCKED,
        }
        for event in events
    )
    burndown = compute_burndown(candidates, baseline)
    valid_rows = [value for value in burndown.values() if isinstance(value.denominator, int)]
    merge_rate = _merge_rate(events)
    pr_events = [event for event in events if event.pr_url is not None]
    merged_clean = sum(
        event.merged_at is not None
        and event.merge_verified
        and event.reason is not ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
        and event.test_exempt_reason is None
        for event in pr_events
    )
    rejected = sum(event.reason is ReasonCode.CI_CHECK_FAILED for event in pr_events)
    edited = max(len(pr_events) - merged_clean - rejected, 0)
    manual_merge_pending = sum(
        event.terminal_outcome is CandidateState.AWAITING_HUMAN_MERGE for event in events
    )
    marker_outcomes: dict[str, int] = {}
    for candidate in candidates:
        if candidate.marker_search_outcome is not None:
            marker_outcomes[candidate.marker_search_outcome] = (
                marker_outcomes.get(candidate.marker_search_outcome, 0) + 1
            )
    safety_undetermined = sum(
        candidate.marker_search_outcome in {"failed", "orphaned", "unconfigured"}
        and not has_local_artifact(candidate)
        for candidate in candidates
    )
    terminal_states = {
        CandidateState.TERMINAL,
        CandidateState.MERGED,
        CandidateState.AWAITING_HUMAN_MERGE,
    }
    return {
        "candidates_seen": len(candidates),
        "active": sum(candidate.state not in terminal_states for candidate in candidates),
        "completed": sum(candidate.state in terminal_states for candidate in candidates),
        "dispatched_pr": dispatched_pr,
        "dispatched_issue": dispatched_issue,
        "deferred": sum(candidate.state is CandidateState.DEFERRED for candidate in candidates),
        "deferred_by_reason": _deferred_by_reason(candidates),
        "marker_search_outcomes": marker_outcomes,
        "unpublished": sum(
            candidate.state is CandidateState.DISPATCHING for candidate in candidates
        ),
        "publication_safety_undetermined": safety_undetermined,
        "verification_pass_rate": (
            verification_passes / len(session_events) if session_events else None
        ),
        "stale_skip_count": stale_skips,
        "test_inclusion_rate": (
            sum(event.test_added is True for event in session_events) / len(session_events)
            if session_events
            else None
        ),
        "criterion_satisfaction_by_lane": _criterion_satisfaction_by_lane(events),
        "expected_reason_match_rate": _expected_reason_match_rate(events),
        "session_failure_rate": session_failures / len(events) if events else 0.0,
        "sessions_created": len(session_events),
        "sessions_per_candidate": (len(session_events) / len(candidates) if candidates else 0.0),
        "manual_merge_pending": manual_merge_pending,
        "burn_down_denominators": sum(
            value.denominator for value in valid_rows if isinstance(value.denominator, int)
        ),
        "merge_rate": merge_rate,
        "merged_clean": merged_clean,
        "edited": edited,
        "rejected": rejected,
        "merge_rate_alert": int(bool(pr_events) and merge_rate < config.merge_rate_floor),
        "session_failure_alert": int(
            (session_failures / len(events) if events else 0.0) > config.session_failure_ceiling
        ),
    }


def _merge_rate(events: list[EventRecord]) -> float:
    """Compute the merged-clean/edited/rejected aggregate merge rate."""
    pr_events = [event for event in events if event.pr_url is not None]
    merged = sum(
        event.merged_at is not None
        and event.merge_verified
        and event.reason is not ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
        for event in pr_events
    )
    return merged / len(pr_events) if pr_events else 0.0


def _criterion_satisfaction_by_lane(events: list[EventRecord]) -> dict[str, int]:
    """Count criteria the orchestrator observed as satisfied, per lane."""
    result: dict[str, int] = {}
    for event in events:
        evidence = event.criterion_evidence
        if evidence is not None and evidence.satisfied is True:
            result[event.lane.value] = result.get(event.lane.value, 0) + 1
    return result


def _deferred_by_reason(candidates: list[Candidate]) -> dict[str, int]:
    """Count deferred candidates by their durable reason."""
    result: dict[str, int] = {}
    for candidate in candidates:
        if candidate.state is CandidateState.DEFERRED and candidate.reason is not None:
            result[candidate.reason.value] = result.get(candidate.reason.value, 0) + 1
    return result


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
    title = (
        "SIMULATED Remediation KPI rollup"
        if config.mode is Mode.SIMULATE
        else "Remediation KPI rollup"
    )
    lines = [f"# {title}", "", f"- mode: {config.mode.value}", ""]
    for name, metric_value in metrics.items():
        if name in {
            "deferred_by_reason",
            "marker_search_outcomes",
            "criterion_satisfaction_by_lane",
        }:
            continue
        label = name.replace("_", " ").title()
        if name.endswith("_alert") and metric_value:
            lines.append(f"> **ALERT: {label}**")
        elif name.endswith("_alert"):
            continue
        else:
            lines.append(f"- **{label}:** {'n/a' if metric_value is None else metric_value}")
    lines.extend(["", "## Deferred by reason", ""])
    deferred_by_reason = metrics["deferred_by_reason"]
    if isinstance(deferred_by_reason, dict):
        lines.extend(
            f"- **{reason}:** {count}" for reason, count in sorted(deferred_by_reason.items())
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Marker search outcomes", ""])
    marker_outcomes = metrics["marker_search_outcomes"]
    if isinstance(marker_outcomes, dict):
        lines.extend(
            f"- **{outcome}:** {count}" for outcome, count in sorted(marker_outcomes.items())
        )
    else:
        lines.append("- None")
    lines.extend(["", "## Criterion satisfaction by lane", ""])
    by_lane = metrics["criterion_satisfaction_by_lane"]
    if isinstance(by_lane, dict) and by_lane:
        lines.extend(f"- **{lane}:** {count}" for lane, count in sorted(by_lane.items()))
    else:
        lines.append("- None")
    lines.extend(["", "## Burn-down — remediation PRs opened against baseline", ""])
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
