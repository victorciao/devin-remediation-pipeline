"""§11 observability: the Layer 1 event log, Layer 2 report, Layer 3 rollup and burn-down."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import PipelineConfig
from pipeline.observability.events import EventLog, append_candidate_events, event_from_candidate
from pipeline.observability.kpis import (
    NotApplicable,
    compute_burndown,
    compute_kpis,
    render_kpi_report,
    write_kpi_report,
)
from pipeline.observability.report import render_run_report
from pipeline.schemas import (
    Action,
    Candidate,
    CandidateState,
    EventRecord,
    Lane,
    ReasonCode,
    RetryDecision,
    Tier,
)
from tests.factories import codeql_candidate, lane2_candidate

MERGE_RATE_ALERT = "merge_rate_alert"
SESSION_FAILURE_ALERT = "session_failure_alert"


def event(
    candidate_id: str,
    *,
    lane: Lane = Lane.CODEQL,
    terminal_outcome: CandidateState | None = CandidateState.PR_CREATED,
    **fields: Any,  # noqa: ANN401
) -> EventRecord:
    """Build one Layer 1 event with the §12.2 retry fields defaulted."""
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
    log = EventLog(tmp_path / "events.jsonl")

    log.append(
        event(
            "codeql-1",
            attempt=2,
            is_new_session_raw=None,
            retry_decision=RetryDecision.PROCEED_ID_DIFFERS,
        )
    )
    log.append(event("codeql-2"))

    loaded = log.read()
    assert len(loaded) == 2
    assert loaded[0].attempt == 2
    assert loaded[0].is_new_session_raw is None
    assert loaded[0].retry_decision is RetryDecision.PROCEED_ID_DIFFERS
    assert loaded[0].lane is Lane.CODEQL
    assert loaded[1].retry_decision is RetryDecision.PROCEED


def test_event_log_is_append_only(tmp_path: Path) -> None:
    """§14.1 — the event log is append-only; a second run never truncates the first."""
    log = EventLog(tmp_path / "events.jsonl")
    log.append(event("codeql-1"))
    log.append(event("codeql-1", attempt=2))

    assert [record.attempt for record in log.read()] == [1, 2]


def test_events_project_every_candidate_observability_field() -> None:
    """§11 Layer 1 — the event carries the candidate's gate, score and artifact evidence."""
    candidate = codeql_candidate(
        gate_passed=True,
        score=64.0,
        tier=Tier.HIGH,
        action=Action.OPEN_PR,
        state=CandidateState.CONVERGED,
        pr_url="https://example.invalid/pr/1",
        issue_url="https://example.invalid/issue/1",
        test_added=True,
    )

    record = event_from_candidate(candidate, run_id="run-1")

    assert record.candidate_id == candidate.candidate_id
    assert record.lane is Lane.CODEQL
    assert record.score == 64.0
    assert record.tier is Tier.HIGH
    assert record.action is Action.OPEN_PR
    assert record.terminal_outcome is CandidateState.CONVERGED
    assert record.pr_url == "https://example.invalid/pr/1"
    assert record.test_added is True


def test_non_terminal_candidates_have_no_terminal_outcome(tmp_path: Path) -> None:
    """§11 — `terminal_outcome` is only set for terminal/converged rows."""
    log = EventLog(tmp_path / "events.jsonl")
    append_candidate_events(
        log,
        [
            codeql_candidate(candidate_id="codeql-1", state=CandidateState.PR_CREATED),
            codeql_candidate(candidate_id="codeql-2", state=CandidateState.CONVERGED),
        ],
        run_id="run-1",
    )

    outcomes = {record.candidate_id: record.terminal_outcome for record in log.read()}
    assert outcomes["codeql-1"] is None
    assert outcomes["codeql-2"] is CandidateState.CONVERGED


MERGED_AT = "2026-08-29T12:00:00Z"


def merged(candidate_id: str, **fields: Any) -> EventRecord:  # noqa: ANN401
    """A merged-clean PR event: §11 counts only pipeline-verified merges."""
    return event(
        candidate_id,
        terminal_outcome=CandidateState.CONVERGED,
        pr_url=f"https://example.invalid/pr/{candidate_id}",
        merged_at=fields.pop("merged_at", MERGED_AT),
        merge_verified=fields.pop("merge_verified", True),
        **fields,
    )


def rejected(candidate_id: str) -> EventRecord:
    """A PR event rejected for an unresolved disagreement."""
    return event(
        candidate_id,
        terminal_outcome=CandidateState.TERMINAL,
        pr_url=f"https://example.invalid/pr/{candidate_id}",
        reason=ReasonCode.DISAGREEMENT_UNRESOLVED,
    )


def test_merge_rate_alert_fires_below_the_floor(simulate_config: PipelineConfig) -> None:
    """§17 — a merge rate under 0.50 raises the alert."""
    events = [merged("c1"), rejected("c2"), rejected("c3"), rejected("c4")]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merge_rate"] == 0.25
    assert rollup[MERGE_RATE_ALERT] == 1


def test_merge_rate_at_the_floor_does_not_alert(simulate_config: PipelineConfig) -> None:
    """§11 — the alert is `< floor`, so exactly 0.50 is quiet."""
    events = [merged("c1"), merged("c2"), rejected("c3"), rejected("c4")]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merge_rate"] == 0.50
    assert rollup[MERGE_RATE_ALERT] == 0


def test_an_unverified_external_merge_is_not_counted_as_a_merge(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — `merged_clean`/`merge_rate` count only verified pipeline merges."""
    events = [
        merged(
            "c1",
            merge_verified=False,
            reason=ReasonCode.MERGED_EXTERNALLY_UNVERIFIED,
        ),
        merged("c2"),
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merged_clean"] == 1
    assert rollup["merge_rate"] == 0.5


def test_a_merge_without_a_merged_at_timestamp_is_not_counted(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a PR the pipeline never observed merged contributes nothing to the numerator."""
    events = [merged("c1", merged_at=None), merged("c2")]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merged_clean"] == 1
    assert rollup["merge_rate"] == 0.5


def test_a_merge_flagged_verified_without_the_reason_cleared_is_not_counted(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — `MERGED_EXTERNALLY_UNVERIFIED` disqualifies the event on its own."""
    events = [merged("c1", reason=ReasonCode.MERGED_EXTERNALLY_UNVERIFIED)]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merged_clean"] == 0
    assert rollup["merge_rate"] == 0.0


def test_merge_rate_alert_is_suppressed_without_pr_events(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a run that opened no PR has no merge rate to alert on."""
    events = [event("c1", terminal_outcome=CandidateState.TERMINAL)]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["merge_rate"] == 0.0
    assert rollup[MERGE_RATE_ALERT] == 0


def test_merge_rate_alert_is_suppressed_for_an_empty_run(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the zero-event run is quiet, not alerting."""
    rollup = compute_kpis([], [], {}, simulate_config)

    assert rollup[MERGE_RATE_ALERT] == 0
    assert rollup[SESSION_FAILURE_ALERT] == 0


def test_session_failure_alert_fires_above_the_ceiling(simulate_config: PipelineConfig) -> None:
    """§17 — a session-failure rate over 0.30 raises the alert."""
    events = [
        event("c1", reason=ReasonCode.SESSION_CEILING),
        event("c2", reason=ReasonCode.ROLE_COLLISION),
        event("c3"),
        event("c4"),
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["session_failure_rate"] == 0.50
    assert rollup[SESSION_FAILURE_ALERT] == 1


def test_session_failure_at_the_ceiling_does_not_alert(simulate_config: PipelineConfig) -> None:
    """§11 — the alert is `> ceiling`, so exactly 0.30 is quiet."""
    events = [event(f"c{index}", reason=ReasonCode.SESSION_CEILING) for index in range(3)]
    events += [event(f"ok{index}") for index in range(7)]

    rollup = compute_kpis([], events, {}, simulate_config)

    rate = rollup["session_failure_rate"]
    assert isinstance(rate, float)
    assert abs(rate - 0.30) < 1e-9
    assert rollup[SESSION_FAILURE_ALERT] == 0


def test_burndown_vs_baseline(baseline: Mapping[str, Any], simulate_config: PipelineConfig) -> None:
    """§11/§17 — burn-down is measured against the Phase 0c baseline totals."""
    candidates = [
        codeql_candidate(candidate_id="codeql-1", state=CandidateState.CONVERGED),
        lane2_candidate(candidate_id="lane2-1", state=CandidateState.CONVERGED),
    ]

    burndown = compute_burndown(candidates, dict(baseline))

    codeql = burndown[Lane.CODEQL]
    assert codeql.denominator == baseline["totals"]["codeql_open_alerts"]
    assert codeql.completed == 1
    assert codeql.remaining == int(baseline["totals"]["codeql_open_alerts"]) - 1
    assert burndown[Lane.SKIPPED_TESTS].denominator == baseline["totals"]["skipped_tests"]
    assert burndown[Lane.DEPRECATIONS].denominator == baseline["totals"]["deprecations"]


def test_suppressed_rows_count_only_in_the_denominator(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§11 — blocked/suppressed rows are non-terminal and never count as progress."""
    candidates = [
        lane2_candidate(candidate_id="lane2-1", state=CandidateState.SUPPRESSED_BY_CONTAINMENT),
        lane2_candidate(candidate_id="lane2-2", state=CandidateState.BLOCKED_BY_ENCLOSING_SKIP),
    ]

    skipped = compute_burndown(candidates, dict(baseline))[Lane.SKIPPED_TESTS]

    assert skipped.completed == 0
    assert skipped.denominator == baseline["totals"]["skipped_tests"]


def test_burndown_reports_na_for_invalid_baseline_lane(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§17 — a lane outside `baseline_valid_lanes` reports `n/a` with its reason."""
    degraded = dict(baseline)
    degraded["baseline_valid_lanes"] = [Lane.SKIPPED_TESTS.value, Lane.DEPRECATIONS.value]
    degraded["baseline_invalid_reasons"] = {
        Lane.CODEQL.value: ReasonCode.CAPABILITY_UNAVAILABLE.value
    }

    burndown = compute_burndown(
        [codeql_candidate(state=CandidateState.CONVERGED)],
        degraded,
    )

    codeql = burndown[Lane.CODEQL]
    assert isinstance(codeql.denominator, NotApplicable)
    assert codeql.denominator.reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert isinstance(codeql.completed, NotApplicable)
    assert isinstance(codeql.remaining, NotApplicable)
    assert isinstance(burndown[Lane.SKIPPED_TESTS].denominator, int)


def test_na_burndown_renders_as_na_in_the_report(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§11 — the `n/a` representation is explicit in the rendered rollup, not a zero."""
    degraded = dict(baseline)
    degraded["baseline_valid_lanes"] = [Lane.SKIPPED_TESTS.value]

    markdown = render_kpi_report([], [], degraded, simulate_config)

    assert f"- **{Lane.CODEQL.value}:** n/a" in markdown
    assert ReasonCode.CAPABILITY_UNAVAILABLE.value in markdown


def test_rollup_reports_the_plan_mandated_rates(simulate_config: PipelineConfig) -> None:
    """§11 — the rollup exposes every §11 rate, not just the alerting ones."""
    events = [
        merged("c1", action=Action.OPEN_PR, test_added=True),
        event("c2", lane=Lane.SKIPPED_TESTS, action=Action.OPEN_PR, test_added=False),
    ]

    rollup = compute_kpis([codeql_candidate()], events, {}, simulate_config)

    for key in (
        "test_inclusion_rate",
        "criterion_coverage_rate",
        "expected_reason_match_rate",
        "disagreement_unresolved_rate",
        "verification_pass_rate",
        "implementer_test_edit_violation_rate",
        "sessions_per_candidate_planner",
        "sessions_per_candidate_implementer",
        "sessions_per_candidate_reviewer",
    ):
        assert key in rollup
    assert rollup["test_inclusion_rate"] == 0.5


def test_alerts_are_rendered_visibly_in_the_markdown_rollup(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the rollup persisted to `reports/kpis.md` marks alerts distinctly."""
    events = [
        rejected("c1"),
        event("c2", reason=ReasonCode.SESSION_CEILING),
    ]

    markdown = render_kpi_report([], events, {}, simulate_config)

    assert "ALERT" in markdown.upper()
    assert "Merge Rate Alert" in markdown
    assert "Session Failure Alert" in markdown


def test_kpi_report_is_written_to_the_local_sink(
    tmp_path: Path, simulate_config: PipelineConfig
) -> None:
    """§11 — the rollup is persisted under `reports/`."""
    path = tmp_path / "reports" / "kpis.md"

    write_kpi_report(path, [], [merged("c1")], {}, simulate_config)

    assert path.read_text(encoding="utf-8").startswith("# Remediation KPI rollup")


def test_run_report_lists_dispatched_and_deferred() -> None:
    """§11 Layer 2 — the per-run report records tiers, deferrals and artifact links."""
    candidates = [
        codeql_candidate(
            candidate_id="codeql-1",
            tier=Tier.HIGH,
            action=Action.OPEN_PR,
            score=70.0,
            gate_passed=True,
            state=CandidateState.PR_CREATED,
            pr_number=1,
            pr_url="https://example.invalid/pr/1",
        ),
        codeql_candidate(
            candidate_id="codeql-2",
            tier=Tier.HIGH,
            action=Action.DEFERRED,
            score=65.0,
            gate_passed=True,
            state=CandidateState.DEFERRED,
            reason=ReasonCode.BUDGET_OVERFLOW,
        ),
        codeql_candidate(
            candidate_id="codeql-3",
            action=Action.LOG_ONLY,
            gate_passed=False,
            reason=ReasonCode.OUT_OF_SCOPE_FRONTEND,
            state=CandidateState.TERMINAL,
        ),
    ]

    report = render_run_report(candidates, run_id="run-1")

    assert "Run run-1" in report
    assert "codeql-1" in report
    assert "Deferred by budget: 1" in report
    assert ReasonCode.OUT_OF_SCOPE_FRONTEND.value in report
    assert Tier.HIGH.value in report


def dispatched_pr_candidate(candidate_id: str, **fields: Any) -> Candidate:  # noqa: ANN401
    """One high-tier candidate routed to a PR; `state` says whether it got there."""
    return codeql_candidate(
        candidate_id=candidate_id,
        tier=Tier.HIGH,
        action=Action.OPEN_PR,
        score=70.0,
        gate_passed=True,
        **fields,
    )


def test_dispatch_counts_derive_from_lifecycle_state_not_routing(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a candidate routed to a PR that never left `deferred` was not dispatched.

    A LIVE run reported `Dispatched Pr: 1` for a candidate whose only durable row was
    `deferred/capability_unavailable`; the routing decision alone is not evidence of an
    artifact.
    """
    candidates = [
        dispatched_pr_candidate(
            "codeql-1",
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        ),
        dispatched_pr_candidate(
            "codeql-2",
            state=CandidateState.PR_CREATED,
            pr_number=2,
            pr_url="https://example.invalid/pr/2",
        ),
        codeql_candidate(
            candidate_id="codeql-3",
            tier=Tier.MEDIUM,
            action=Action.OPEN_ISSUE,
            gate_passed=True,
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        ),
        codeql_candidate(
            candidate_id="codeql-4",
            tier=Tier.MEDIUM,
            action=Action.OPEN_ISSUE,
            gate_passed=True,
            state=CandidateState.ISSUE_CREATED,
            issue_number=4,
            issue_url="https://example.invalid/issues/4",
        ),
    ]

    rollup = compute_kpis(candidates, [], {}, simulate_config)

    assert rollup["dispatched_pr"] == 1
    assert rollup["dispatched_issue"] == 1


@pytest.mark.parametrize(
    "state",
    [
        CandidateState.DISPATCHING,
        CandidateState.PR_CREATED,
        CandidateState.ISSUE_PATCHED,
        CandidateState.COMMENT_CREATED,
        CandidateState.CONVERGED,
        CandidateState.TERMINAL,
    ],
)
def test_every_post_dispatch_state_counts_as_dispatched(
    state: CandidateState,
    simulate_config: PipelineConfig,
) -> None:
    """§11 — dispatch is counted from the point the run committed to the artifact."""
    rollup = compute_kpis(
        [dispatched_pr_candidate("codeql-1", state=state)], [], {}, simulate_config
    )

    assert rollup["dispatched_pr"] == 1


@pytest.mark.parametrize(
    "state",
    [
        CandidateState.ENUMERATED,
        CandidateState.GATED,
        CandidateState.SCORED,
        CandidateState.DEFERRED,
    ],
)
def test_no_pre_dispatch_state_counts_as_dispatched(
    state: CandidateState,
    simulate_config: PipelineConfig,
) -> None:
    """§11 — nothing before `dispatching` produced an artifact, whatever the routing says."""
    rollup = compute_kpis(
        [dispatched_pr_candidate("codeql-1", state=state)], [], {}, simulate_config
    )

    assert rollup["dispatched_pr"] == 0


def test_run_report_excludes_a_routed_but_deferred_candidate_from_its_tiers() -> None:
    """§11 Layer 2 — "Dispatched by tier" counts artifacts, not routing decisions."""
    candidates = [
        dispatched_pr_candidate(
            "codeql-1",
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        ),
        dispatched_pr_candidate(
            "codeql-2",
            state=CandidateState.PR_CREATED,
            pr_number=2,
            pr_url="https://example.invalid/pr/2",
        ),
    ]

    report = render_run_report(candidates, run_id="run-1")

    assert "## Dispatched by tier\n- `high`: 1\n" in report
    assert "`high`: 2" not in report
    assert "Deferred by capability/other: 1" in report


def test_run_report_reports_no_tier_when_every_dispatch_deferred() -> None:
    """§11 Layer 2 — a run that published nothing must not claim a dispatched tier."""
    candidates = [
        dispatched_pr_candidate(
            "codeql-1",
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        )
    ]

    report = render_run_report(candidates, run_id="run-1")

    assert "## Dispatched by tier\n- None\n" in report
    assert Tier.HIGH.value not in report


def test_run_report_separates_ceiling_and_capability_deferrals() -> None:
    """§11 Layer 2 — budget, session-ceiling and capability deferrals are counted apart."""
    candidates = [
        codeql_candidate(
            candidate_id="codeql-1",
            action=Action.DEFERRED,
            state=CandidateState.DEFERRED,
            reason=ReasonCode.SESSION_CEILING,
        ),
        codeql_candidate(
            candidate_id="codeql-2",
            action=Action.DEFERRED,
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        ),
    ]

    report = render_run_report(candidates, run_id="run-1")

    assert "Deferred by budget: 0" in report
    assert "Deferred by session ceiling: 1" in report
    assert "Deferred by capability/other: 1" in report
