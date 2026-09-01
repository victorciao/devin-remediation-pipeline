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
    pr_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.pr_url is not None and candidate.state is not CandidateState.DEFERRED
    }
    issue_ids = {
        candidate.candidate_id
        for candidate in candidates
        if candidate.issue_url is not None and candidate.state is not CandidateState.DEFERRED
    }
    by_candidate: dict[str, list[EventRecord]] = {}
    for event in events:
        by_candidate.setdefault(event.candidate_id, []).append(event)
        if event.pr_url is not None:
            pr_ids.add(event.candidate_id)
        if event.issue_url is not None:
            issue_ids.add(event.candidate_id)
    dispatched_pr = len(pr_ids)
    dispatched_issue = len(issue_ids)
    session_ids = {
        candidate_id
        for candidate_id, rows in by_candidate.items()
        if any(event.session_id is not None for event in rows)
    }
    verification_passes = sum(
        any(
            event.criterion_evidence is not None and event.criterion_evidence.satisfied is True
            for event in rows
        )
        for rows in by_candidate.values()
        if any(event.session_id is not None for event in rows)
    )
    stale_skips = len(
        {event.candidate_id for event in events if event.reason is ReasonCode.STALE_SKIP}
    )
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
    pr_events = _latest_pr_events(events)
    settled_pr_events = _settled_pr_events(events)
    merged_clean = sum(
        event.merged_at is not None
        and event.merge_verified
        and event.reason is not ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
        and event.test_exempt_reason is None
        for event in pr_events
    )
    rejected = sum(event.reason is ReasonCode.CI_CHECK_FAILED for event in pr_events)
    merged_total = sum(event.merged_at is not None for event in pr_events)
    edited = max(merged_total - merged_clean, 0)
    manual_merge_pending = len(
        {
            event.candidate_id
            for event in events
            if event.terminal_outcome is CandidateState.AWAITING_HUMAN_MERGE
        }
    )
    awaiting_merge = sum(
        event.terminal_outcome is CandidateState.AWAITING_HUMAN_MERGE for event in pr_events
    )
    issue_rows = _latest_issue_rows(candidates, events)
    issues_created = sum(
        issue_url is not None and not adopted for issue_url, adopted, _tier in issue_rows.values()
    )
    issues_adopted = sum(adopted for issue_url, adopted, _tier in issue_rows.values() if issue_url)
    issues_created_by_tier = _issue_counts_by_tier(issue_rows, adopted=False)
    issues_adopted_by_tier = _issue_counts_by_tier(issue_rows, adopted=True)
    high_issue_closure_by_merged_pr = len(
        {
            event.candidate_id
            for event in events
            if event.tier is not None
            and event.tier.value == "high"
            and event.issue_url is not None
            and event.merged_at is not None
            and event.merge_verified
        }
    )
    required_test_candidates = {
        event.candidate_id
        for event in events
        if event.lane is Lane.SKIPPED_TESTS and event.session_id is not None
    }
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
    verification_pass_rate = verification_passes / len(session_ids) if session_ids else None
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
        "verification_pass_rate": verification_pass_rate,
        "stale_skip_count": stale_skips,
        "test_inclusion_rate": (
            sum(
                any(event.test_added is True for event in by_candidate[candidate_id])
                for candidate_id in required_test_candidates
            )
            / len(required_test_candidates)
            if required_test_candidates
            else None
        ),
        "criterion_satisfaction_by_lane": _criterion_satisfaction_by_lane(events),
        "expected_reason_match_rate": _expected_reason_match_rate(events),
        "session_failure_rate": session_failures / len(events) if events else 0.0,
        "sessions_created": len(session_ids),
        "sessions_per_candidate": (len(session_ids) / len(candidates) if candidates else 0.0),
        "manual_merge_pending": manual_merge_pending,
        "awaiting_merge": awaiting_merge,
        "issues_created": issues_created,
        "issues_adopted": issues_adopted,
        "issues_created_by_tier": issues_created_by_tier,
        "issues_adopted_by_tier": issues_adopted_by_tier,
        "high_issue_closure_by_merged_pr": high_issue_closure_by_merged_pr,
        "burn_down_denominators": sum(
            value.denominator for value in valid_rows if isinstance(value.denominator, int)
        ),
        "merge_rate": merge_rate,
        "merged_clean": merged_clean,
        "edited": edited,
        "rejected": rejected,
        "merge_rate_alert": int(
            bool(settled_pr_events)
            and merge_rate is not None
            and merge_rate < config.merge_rate_floor
        ),
        "verification_pass_rate_alert": int(
            verification_pass_rate is not None
            and verification_pass_rate < config.verification_pass_rate_floor
        ),
        "publication_safety_alert": int(safety_undetermined > 0),
        "session_failure_alert": int(
            (session_failures / len(events) if events else 0.0) > config.session_failure_ceiling
        ),
    }


def _merge_rate(events: list[EventRecord]) -> float | None:
    """Compute observed human-merge rate over PRs with a human disposition."""
    pr_events = _settled_pr_events(events)
    merged = sum(
        event.terminal_outcome is CandidateState.MERGED
        and event.merged_at is not None
        and event.merge_verified
        and event.reason is not ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
        for event in pr_events
    )
    return merged / len(pr_events) if pr_events else None


def _latest_pr_events(events: list[EventRecord]) -> list[EventRecord]:
    """Return one artifact-bearing event per candidate."""
    latest: dict[str, EventRecord] = {}
    for event in events:
        if event.pr_url is not None:
            latest[event.candidate_id] = event
    return list(latest.values())


def _settled_pr_events(events: list[EventRecord]) -> list[EventRecord]:
    """Exclude pending human merges because the pipeline never merges or fails them."""
    return [
        event
        for event in _latest_pr_events(events)
        if event.terminal_outcome is not CandidateState.AWAITING_HUMAN_MERGE
    ]


def _latest_issue_rows(
    candidates: list[Candidate],
    events: list[EventRecord],
) -> dict[str, tuple[str | None, bool, str | None]]:
    """Collect one issue identity row per candidate across state and events."""
    rows: dict[str, tuple[str | None, bool, str | None]] = {
        candidate.candidate_id: (
            candidate.issue_url,
            candidate.issue_adopted,
            candidate.tier.value if candidate.tier is not None else None,
        )
        for candidate in candidates
        if candidate.issue_url is not None
    }
    for event in events:
        if event.issue_url is not None:
            rows[event.candidate_id] = (
                event.issue_url,
                event.issue_adopted,
                event.tier.value if event.tier is not None else None,
            )
    return rows


def _issue_counts_by_tier(
    rows: dict[str, tuple[str | None, bool, str | None]],
    *,
    adopted: bool,
) -> dict[str, int]:
    """Count distinct issue artifacts by their candidate tier."""
    result: dict[str, int] = {}
    for issue_url, was_adopted, tier in rows.values():
        if issue_url is None or was_adopted is not adopted:
            continue
        key = tier or "unknown"
        result[key] = result.get(key, 0) + 1
    return result


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


def _expected_reason_match_rate(events: list[EventRecord]) -> float | None:
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
        else None
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
