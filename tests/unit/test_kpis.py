"""§11 observability: the Layer 1 event log, Layer 3 rollup, alerting and burn-down."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pipeline.config import PipelineConfig
from pipeline.schemas import (
    Action,
    CandidateState,
    EventRecord,
    Lane,
    ReasonCode,
    RetryDecision,
    Tier,
)
from tests import _api

MERGE_RATE_ALERT = "merge_rate"
SESSION_FAILURE_ALERT = "session_failure_rate"


def event(
    candidate_id: str,
    *,
    lane: Lane = Lane.CODEQL,
    terminal_outcome: CandidateState = CandidateState.PR_CREATED,
    **fields: Any,  # noqa: ANN401,
) -> EventRecord:
    return EventRecord(
        run_id="run-1",
        lane=lane,
        candidate_id=candidate_id,
        terminal_outcome=terminal_outcome,
        attempt=fields.pop("attempt", 1),
        is_new_session_raw=fields.pop("is_new_session_raw", True),
        retry_decision=fields.pop("retry_decision", RetryDecision.PROCEED),
        **fields,
    )


def test_event_log_round_trips_the_retry_fields(tmp_path: Path) -> None:
    """§12.2 — both the raw tri-state and the resolved decision are recorded."""
    events = _api.events()
    path = tmp_path / "events.jsonl"
    record = event(
        "codeql-1",
        attempt=2,
        is_new_session_raw=None,
        retry_decision=RetryDecision.PROCEED_ID_DIFFERS,
    )

    events.append_event(path, record)
    events.append_event(path, event("codeql-2"))

    loaded = events.read_events(path)
    assert len(loaded) == 2
    assert loaded[0].attempt == 2
    assert loaded[0].is_new_session_raw is None
    assert loaded[0].retry_decision == RetryDecision.PROCEED_ID_DIFFERS
    assert loaded[0].lane == Lane.CODEQL


def test_merge_rate_alert_fires_below_the_floor(simulate_config: PipelineConfig) -> None:
    """§17 — a merge rate under 0.50 raises the alert."""
    rollup = _api.kpis().compute_kpis(
        [event(f"codeql-{index}") for index in range(4)],
        config=simulate_config,
        merge_outcomes=["merged", "rejected", "rejected", "edited"],
    )

    assert rollup.merge_rate is not None and rollup.merge_rate < 0.50
    assert any(MERGE_RATE_ALERT in alert for alert in rollup.alerts)


def test_merge_rate_at_the_floor_does_not_alert(simulate_config: PipelineConfig) -> None:
    rollup = _api.kpis().compute_kpis(
        [event(f"codeql-{index}") for index in range(4)],
        config=simulate_config,
        merge_outcomes=["merged", "merged", "rejected", "rejected"],
    )

    assert rollup.merge_rate == 0.50
    assert not any(MERGE_RATE_ALERT in alert for alert in rollup.alerts)


def test_session_failure_alert_fires_above_the_ceiling(simulate_config: PipelineConfig) -> None:
    """§17 — a session-failure rate over 0.30 raises the alert."""
    rollup = _api.kpis().compute_kpis(
        [event(f"codeql-{index}") for index in range(4)],
        config=simulate_config,
        session_outcomes=["ok", "failed", "failed", "ok"],
    )

    assert rollup.session_failure_rate == 0.50
    assert any(SESSION_FAILURE_ALERT in alert for alert in rollup.alerts)


def test_session_failure_at_the_ceiling_does_not_alert(simulate_config: PipelineConfig) -> None:
    rollup = _api.kpis().compute_kpis(
        [event(f"codeql-{index}") for index in range(10)],
        config=simulate_config,
        session_outcomes=["failed"] * 3 + ["ok"] * 7,
    )

    assert rollup.session_failure_rate is not None
    assert abs(rollup.session_failure_rate - 0.30) < 1e-9
    assert not any(SESSION_FAILURE_ALERT in alert for alert in rollup.alerts)


def test_burndown_vs_baseline(baseline: Mapping[str, Any], simulate_config: PipelineConfig) -> None:
    """§11/§17 — burn-down is measured against the Phase 0c baseline totals."""
    events = [
        event("codeql-1", terminal_outcome=CandidateState.CONVERGED),
        event("lane2-1", lane=Lane.SKIPPED_TESTS, terminal_outcome=CandidateState.CONVERGED),
    ]

    rollup = _api.kpis().compute_kpis(events, config=simulate_config, baseline=baseline)

    codeql = rollup.burndown[Lane.CODEQL.value]
    assert isinstance(codeql, Mapping)
    assert codeql["denominator"] == baseline["totals"]["codeql_open_alerts"]
    assert codeql["numerator"] == 1
    skipped = rollup.burndown[Lane.SKIPPED_TESTS.value]
    assert isinstance(skipped, Mapping)
    assert skipped["denominator"] == baseline["totals"]["skipped_tests"]


def test_suppressed_rows_count_only_in_the_denominator(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§11 — blocked/suppressed rows never count as progress."""
    events = [
        event(
            "lane2-1",
            lane=Lane.SKIPPED_TESTS,
            terminal_outcome=CandidateState.SUPPRESSED_BY_CONTAINMENT,
        ),
        event(
            "lane2-2",
            lane=Lane.SKIPPED_TESTS,
            terminal_outcome=CandidateState.BLOCKED_BY_ENCLOSING_SKIP,
        ),
    ]

    rollup = _api.kpis().compute_kpis(events, config=simulate_config, baseline=baseline)

    skipped = rollup.burndown[Lane.SKIPPED_TESTS.value]
    assert isinstance(skipped, Mapping)
    assert skipped["numerator"] == 0
    assert skipped["denominator"] == baseline["totals"]["skipped_tests"]


def test_burndown_reports_na_for_invalid_baseline_lane(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§17 — a lane outside `baseline_valid_lanes` reports `n/a` with its reason."""
    degraded = dict(baseline)
    degraded["baseline_valid_lanes"] = [Lane.SKIPPED_TESTS.value, Lane.DEPRECATIONS.value]

    rollup = _api.kpis().compute_kpis(
        [event("codeql-1", terminal_outcome=CandidateState.CONVERGED)],
        config=simulate_config,
        baseline=degraded,
    )

    codeql = rollup.burndown[Lane.CODEQL.value]
    assert isinstance(codeql, Mapping)
    assert codeql["value"] == "n/a"
    assert codeql["reason"] == ReasonCode.CAPABILITY_UNAVAILABLE.value


def test_rollup_reports_the_plan_mandated_rates(simulate_config: PipelineConfig) -> None:
    rollup = _api.kpis().compute_kpis(
        [
            event("codeql-1", terminal_outcome=CandidateState.CONVERGED, test_added=True),
            event("lane2-1", lane=Lane.SKIPPED_TESTS, terminal_outcome=CandidateState.TERMINAL),
        ],
        config=simulate_config,
    )

    assert rollup.test_inclusion_rate is not None
    assert rollup.criterion_coverage_rate is not None
    assert rollup.expected_reason_match_rate is not None
    assert rollup.disagreement_unresolved_rate is not None


def test_alerts_are_rendered_visibly_in_the_markdown_rollup(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the rollup persisted to `reports/kpis.md` marks alerts distinctly."""
    kpis = _api.kpis()
    rollup = kpis.compute_kpis(
        [event("codeql-1")],
        config=simulate_config,
        merge_outcomes=["rejected", "rejected"],
        session_outcomes=["failed", "ok"],
    )

    markdown = kpis.render_markdown(rollup)

    assert MERGE_RATE_ALERT in markdown
    assert SESSION_FAILURE_ALERT in markdown
    assert "ALERT" in markdown.upper()


def test_run_report_lists_dispatched_and_deferred(simulate_config: PipelineConfig) -> None:
    """§11 Layer 2 — the per-run report records tiers, deferrals and artifact links."""
    events = [
        event("codeql-1", tier=Tier.HIGH, action=Action.OPEN_PR),
        event(
            "codeql-2",
            tier=Tier.HIGH,
            action=Action.DEFERRED,
            terminal_outcome=CandidateState.DEFERRED,
        ),
        event(
            "codeql-3",
            tier=Tier.LOW,
            action=Action.LOG_ONLY,
            terminal_outcome=CandidateState.TERMINAL,
            reason=ReasonCode.OUT_OF_SCOPE_FRONTEND,
        ),
    ]

    report = _api.report().render_run_report(events, simulate_config)

    assert "codeql-1" in report
    assert "codeql-2" in report
    assert ReasonCode.OUT_OF_SCOPE_FRONTEND.value in report
    assert Action.DEFERRED.value in report
