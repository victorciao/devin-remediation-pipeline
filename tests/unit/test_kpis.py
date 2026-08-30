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
    BaselineStatus,
    Candidate,
    CandidateState,
    EventRecord,
    Lane,
    ReasonCode,
    RedBaselineResult,
    RetryDecision,
    Tier,
)
from tests.factories import codeql_candidate, lane2_candidate

VALID_RED_BASELINE = RedBaselineResult(status=BaselineStatus.VALID)
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


def role_loop(candidate_id: str, **fields: Any) -> dict[str, Any]:  # noqa: ANN401
    """The three session ids that mark a candidate as having entered the role loop."""
    return {
        "planner_session_id": f"devin-planner-{candidate_id}",
        "implementer_session_id": f"devin-implementer-{candidate_id}",
        "reviewer_session_id": f"devin-reviewer-{candidate_id}",
        **fields,
    }


def test_rollup_reports_the_plan_mandated_rates(simulate_config: PipelineConfig) -> None:
    """§11 — the rollup exposes every §11 rate, not just the alerting ones."""
    events = [
        merged("c1", action=Action.OPEN_PR, test_added=True, **role_loop("c1")),
        event(
            "c2",
            lane=Lane.SKIPPED_TESTS,
            action=Action.OPEN_PR,
            test_added=False,
            **role_loop("c2"),
        ),
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


def test_role_loop_rates_are_denominated_over_candidates_that_entered_the_loop(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a candidate gated or budget-deferred before the loop cannot dilute its rates.

    The LIVE dry run entered the loop for exactly one of fifty candidates and reported
    `criterion_coverage_rate: 0.023` and `test_inclusion_rate: 0.0`, describing the
    forty-nine candidates that never had a planner at all.
    """
    events = [
        event(
            "codeql-1",
            action=Action.OPEN_PR,
            test_added=True,
            red_baseline=VALID_RED_BASELINE,
            terminal_outcome=CandidateState.CONVERGED,
            diff_reviewed=True,
            **role_loop("codeql-1"),
        ),
        *(
            event(
                f"codeql-{index}",
                action=Action.DEFERRED,
                terminal_outcome=CandidateState.DEFERRED,
                reason=ReasonCode.BUDGET_OVERFLOW,
            )
            for index in range(2, 51)
        ),
    ]
    candidates = [
        codeql_candidate(
            candidate_id="codeql-1",
            planner_criteria=["AC-1"],
            reviewer_criterion_ids=["AC-1"],
        ),
        *(codeql_candidate(candidate_id=f"codeql-{index}") for index in range(2, 51)),
    ]

    rollup = compute_kpis(candidates, events, {}, simulate_config)

    assert rollup["test_inclusion_rate"] == 1.0
    assert rollup["criterion_coverage_rate"] == 1.0
    assert rollup["verification_pass_rate"] == 1.0


def test_role_loop_rates_are_none_when_no_candidate_entered_the_loop(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — an empty denominator is unknown, not a zero rate to be read as failure."""
    events = [
        event(
            f"codeql-{index}",
            action=Action.DEFERRED,
            terminal_outcome=CandidateState.DEFERRED,
            reason=ReasonCode.BUDGET_OVERFLOW,
        )
        for index in range(1, 4)
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["test_inclusion_rate"] is None
    assert rollup["verification_pass_rate"] is None


def test_one_role_session_id_is_enough_to_enter_the_loop_denominator(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a candidate whose planner ran but whose reviewer never did still counts."""
    events = [
        event(
            "codeql-1",
            action=Action.OPEN_PR,
            test_added=False,
            planner_session_id="devin-planner-codeql-1",
        )
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["test_inclusion_rate"] == 0.0


def test_an_unknown_role_loop_rate_renders_as_na_not_zero(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the markdown rollup distinguishes "no loop ran" from "the loop failed"."""
    deferred = event(
        "codeql-1",
        action=Action.DEFERRED,
        terminal_outcome=CandidateState.DEFERRED,
        reason=ReasonCode.BUDGET_OVERFLOW,
    )

    markdown = render_kpi_report([], [deferred], {}, simulate_config)

    assert "- **Test Inclusion Rate:** n/a" in markdown
    assert "- **Verification Pass Rate:** n/a" in markdown


def test_criterion_coverage_is_unknown_when_no_candidate_entered_the_loop(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — criterion coverage shares the role-loop denominator with the other two rates."""
    deferred = event(
        "codeql-1",
        action=Action.DEFERRED,
        terminal_outcome=CandidateState.DEFERRED,
        reason=ReasonCode.BUDGET_OVERFLOW,
    )

    rollup = compute_kpis([], [deferred], {}, simulate_config)
    markdown = render_kpi_report([], [deferred], {}, simulate_config)

    assert rollup["criterion_coverage_rate"] is None
    assert "- **Criterion Coverage Rate:** n/a" in markdown


def test_burn_down_denominators_stay_keyed_to_candidates_seen(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§11 — the role-loop denominator change does not touch burn-down accounting."""
    candidates = [
        codeql_candidate(candidate_id="codeql-1", state=CandidateState.PR_CREATED),
        codeql_candidate(
            candidate_id="codeql-2",
            state=CandidateState.DEFERRED,
            reason=ReasonCode.BUDGET_OVERFLOW,
        ),
    ]

    burndown = compute_burndown(candidates, dict(baseline))

    assert burndown[Lane.CODEQL].denominator == baseline["totals"]["codeql_open_alerts"]
    assert burndown[Lane.CODEQL].completed == 0
    assert burndown[Lane.CODEQL].remaining == baseline["totals"]["codeql_open_alerts"]


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
            pr_number=1,
            pr_url="https://example.invalid/pr/1",
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
    """§11 — dispatch is counted from the artifact the run holds, in any live state."""
    rollup = compute_kpis(
        [
            dispatched_pr_candidate(
                "codeql-1",
                state=state,
                pr_number=1,
                pr_url="https://example.invalid/pr/1",
            )
        ],
        [],
        {},
        simulate_config,
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
def test_a_candidate_holding_no_artifact_url_is_never_dispatched(
    state: CandidateState,
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the artifact URL is the evidence; routing and state alone are not."""
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


def failed_loop_candidate(**fields: Any) -> Candidate:  # noqa: ANN401
    """A candidate the three-session loop escalated instead of converging."""
    return codeql_candidate(
        candidate_id="codeql-1",
        tier=Tier.HIGH,
        score=70.0,
        gate_passed=True,
        action=Action.HUMAN_REVIEW,
        state=CandidateState.TERMINAL,
        reason=ReasonCode.DIFF_REVIEW_INCOMPLETE,
        disagreement_summary="reviewer diff review incomplete",
        head_branch="devin/codeql-1",
        planner_session_id="devin-planner-1",
        implementer_session_id="devin-implementer-1",
        reviewer_session_id="devin-reviewer-1",
        **fields,
    )


def test_escalated_counts_the_candidates_a_human_has_to_look_at(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the rollup must say how much of the run needs a human."""
    candidates = [
        failed_loop_candidate(),
        dispatched_pr_candidate(
            "codeql-2",
            state=CandidateState.PR_CREATED,
            pr_number=2,
            pr_url="https://example.invalid/pr/2",
        ),
    ]

    rollup = compute_kpis(candidates, [], {}, simulate_config)

    assert rollup["escalated"] == 1


def test_the_escalation_section_carries_the_whole_failed_loop_trace() -> None:
    """§11 Layer 2 — a reader must reach the three sessions from the report alone.

    The LIVE dry run's report showed `Dispatched by tier: None` and `Artifact links: None`
    with no trace at all that a three-session loop had run and escalated.
    """
    report = render_run_report([failed_loop_candidate()], run_id="run-1")

    section = report.split("## Failed review escalation", 1)[1]
    assert "codeql-1" in section
    assert f"reason={ReasonCode.DIFF_REVIEW_INCOMPLETE.value}" in section
    assert "disagreement_summary=reviewer diff review incomplete" in section
    assert "head_branch=devin/codeql-1" in section
    assert "planner=devin-planner-1" in section
    assert "implementer=devin-implementer-1" in section
    assert "reviewer=devin-reviewer-1" in section


def test_a_converged_candidate_is_not_listed_as_escalated() -> None:
    """§11 Layer 2 — the section is empty for a run that needed no human."""
    candidates = [
        dispatched_pr_candidate(
            "codeql-1",
            state=CandidateState.PR_CREATED,
            pr_number=1,
            pr_url="https://example.invalid/pr/1",
        )
    ]

    report = render_run_report(candidates, run_id="run-1")

    assert "## Failed review escalation\n- None\n" in report


def test_an_escalated_candidate_is_accounted_for_in_the_report() -> None:
    """§11 Layer 2 — `Unaccounted` is the report's own completeness check."""
    report = render_run_report([failed_loop_candidate()], run_id="run-1")

    assert "Unaccounted: 0" in report


def test_a_candidate_routed_to_human_review_is_not_a_failed_loop() -> None:
    """§11 Layer 2 — a needs-human-review PR converged; the section is for loops that did not.

    Counting every `human_review` routing as an escalation made the section unreadable: a
    high-risk PR that the loop actually agreed on looked identical to a terminated loop.
    """
    routed = dispatched_pr_candidate(
        "codeql-1",
        state=CandidateState.PR_CREATED,
        pr_number=1,
        pr_url="https://example.invalid/pr/1",
        labels=["needs-human-review"],
    )

    report = render_run_report([routed], run_id="run-1")

    assert "## Failed review escalation\n- None\n" in report
    assert "Unaccounted: 0" in report


def test_a_candidate_routed_to_human_review_is_not_counted_as_escalated(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — `escalated` counts terminated loops, which is what a human must pick up."""
    routed = dispatched_pr_candidate(
        "codeql-1",
        state=CandidateState.PR_CREATED,
        pr_number=1,
        pr_url="https://example.invalid/pr/1",
        labels=["needs-human-review"],
    )

    rollup = compute_kpis([routed], [], {}, simulate_config)

    assert rollup["escalated"] == 0


def test_the_rollup_reports_incomplete_diff_reviews_separately(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a run whose reviewers never read a diff is a distinct failure from a disagreement."""
    events = [
        event(
            "codeql-1",
            action=Action.HUMAN_REVIEW,
            terminal_outcome=CandidateState.TERMINAL,
            reason=ReasonCode.DIFF_REVIEW_INCOMPLETE,
            **role_loop("codeql-1"),
        ),
        event(
            "codeql-2",
            action=Action.HUMAN_REVIEW,
            terminal_outcome=CandidateState.TERMINAL,
            reason=ReasonCode.DISAGREEMENT_UNRESOLVED,
            **role_loop("codeql-2"),
        ),
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["diff_review_incomplete_rate"] == 0.5
    assert rollup["disagreement_unresolved_rate"] == 0.5


def test_a_terminal_candidate_without_a_role_loop_is_not_an_escalation(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — escalation means a loop ran and failed; a gated candidate has no loop to hand over.

    A terminal row is reached by paths that never created a session (a stale skip, a
    capability deferral promoted to terminal), and counting those as escalations
    overstates how much of the run a human must read.
    """
    candidates = [
        codeql_candidate(
            candidate_id="codeql-1",
            state=CandidateState.TERMINAL,
            reason=ReasonCode.STALE_SKIP,
        ),
        failed_loop_candidate(),
    ]

    rollup = compute_kpis(candidates, [], {}, simulate_config)

    assert rollup["escalated"] == 1


def test_deferral_counts_come_from_durable_state_not_the_routing_action(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a candidate routed for dispatch that ended `deferred` is a deferral."""
    candidates = [
        dispatched_pr_candidate(
            "codeql-1",
            state=CandidateState.DEFERRED,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE,
        ),
        codeql_candidate(
            candidate_id="codeql-2",
            action=Action.DEFERRED,
            state=CandidateState.DEFERRED,
            reason=ReasonCode.BUDGET_OVERFLOW,
        ),
        dispatched_pr_candidate(
            "codeql-3",
            state=CandidateState.PR_CREATED,
            pr_number=3,
            pr_url="https://example.invalid/pr/3",
        ),
    ]

    rollup = compute_kpis(candidates, [], {}, simulate_config)

    assert rollup["deferred"] == 2
    assert rollup["deferred_by_reason"] == {
        ReasonCode.CAPABILITY_UNAVAILABLE.value: 1,
        ReasonCode.BUDGET_OVERFLOW.value: 1,
    }


def test_the_rollup_renders_the_deferral_reasons_it_counted(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — a deferred run has to say why, in the report a human actually reads."""
    deferred = codeql_candidate(
        candidate_id="codeql-1",
        state=CandidateState.DEFERRED,
        reason=ReasonCode.ARTIFACT_ORPHANED,
    )

    markdown = render_kpi_report([deferred], [], {}, simulate_config)

    assert f"- **{ReasonCode.ARTIFACT_ORPHANED.value}:** 1" in markdown


def test_verification_passes_only_on_convergence_with_a_reviewed_diff(
    simulate_config: PipelineConfig,
) -> None:
    """§11 — the three verification preconditions are conjunctive, not alternatives.

    A converged candidate whose reviewer never read the diff, and one whose red baseline
    was invalid, are both unverified: counting either inflates the one rate that claims
    the pipeline's changes were checked.
    """
    events = [
        event(
            "codeql-1",
            action=Action.OPEN_PR,
            terminal_outcome=CandidateState.CONVERGED,
            red_baseline=VALID_RED_BASELINE,
            diff_reviewed=True,
            **role_loop("codeql-1"),
        ),
        event(
            "codeql-2",
            action=Action.OPEN_PR,
            terminal_outcome=CandidateState.CONVERGED,
            red_baseline=VALID_RED_BASELINE,
            diff_reviewed=False,
            **role_loop("codeql-2"),
        ),
        event(
            "codeql-3",
            action=Action.HUMAN_REVIEW,
            terminal_outcome=CandidateState.TERMINAL,
            red_baseline=VALID_RED_BASELINE,
            diff_reviewed=True,
            **role_loop("codeql-3"),
        ),
        event(
            "codeql-4",
            action=Action.OPEN_PR,
            terminal_outcome=CandidateState.CONVERGED,
            red_baseline=RedBaselineResult(status=BaselineStatus.INVALID_RED_BASELINE),
            diff_reviewed=True,
            **role_loop("codeql-4"),
        ),
    ]

    rollup = compute_kpis([], events, {}, simulate_config)

    assert rollup["verification_pass_rate"] == pytest.approx(0.25)
