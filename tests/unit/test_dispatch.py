"""§6/§7 dispatch: tier actions, the per-run budget, auto-merge gating and dual artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline.config import CiEvidenceMode, IssueSink, Mode, PipelineConfig
from pipeline.dispatch import (
    DROPPED_REASONS,
    HUMAN_ROUTED_REASONS,
    dispatch_candidates,
)
from pipeline.gate import HARD_CONDITION_REASONS
from pipeline.schemas import (
    NEEDS_HUMAN_REVIEW_LABEL,
    Action,
    BaselineStatus,
    Candidate,
    CandidateState,
    DefinitionKind,
    GateName,
    GateResult,
    ReasonCode,
    RedBaselineResult,
    Tier,
)
from tests import _api
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate
from tests.fakes import FakeGitHubClient, NoWriteGitHubClient, ReviewFinding

PARENT_NODEID = "tests/integration_tests/charts/data/api_tests.py::TestPostChartDataApi"
CHILD_NODEID = f"{PARENT_NODEID}::test_chart_data_get"


def pipeline_config(
    **overrides: Any,  # noqa: ANN401
) -> PipelineConfig:
    """A configuration pointed at the shipped rubrics and templates."""
    return PipelineConfig(rubrics_path=RUBRICS_PATH, templates_dir=TEMPLATES_DIR, **overrides)


def live_config(
    **overrides: Any,  # noqa: ANN401
) -> PipelineConfig:
    """A LIVE configuration carrying placeholder credentials (never real secrets)."""
    return pipeline_config(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-github-token"),
        devin_api_key=SecretStr("placeholder-devin-key"),
        **overrides,
    )


def github_config(
    **overrides: Any,  # noqa: ANN401
) -> PipelineConfig:
    """A config whose CI evidence comes from GitHub, so auto-merge is permitted at all."""
    return pipeline_config(
        ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True, **overrides
    )


def high_candidate(
    **fields: Any,  # noqa: ANN401
) -> Candidate:
    """A gate-passing, high-tier LANE 1 candidate ready for dispatch."""
    fields.setdefault("score", 128.0)
    fields.setdefault("risk", 2)
    return codeql_candidate(gate_passed=True, **fields)


def gated_out(candidate: Candidate, gate_name: GateName, reason: ReasonCode) -> Candidate:
    """A candidate carrying the gate failure the §4 gate would have recorded."""
    return candidate.model_copy(
        update={
            "gate_passed": False,
            "failed_gate": gate_name,
            "gate_results": {gate_name: GateResult(passed=False, reason=reason)},
        }
    )


def decision_for(candidate_id: str, decisions: list[Candidate]) -> Candidate:
    """The decision row for one candidate ID."""
    return next(row for row in decisions if row.candidate_id == candidate_id)


# -- §6 tier actions and budget ----------------------------------------------------------


def test_high_tier_low_risk_opens_a_pull_request(simulate_config: PipelineConfig) -> None:
    """§6 — a high-tier, low-risk candidate opens a PR."""
    decisions = dispatch_candidates([high_candidate(risk=2)], simulate_config)

    assert decisions[0].tier is Tier.HIGH
    assert decisions[0].action is Action.OPEN_PR
    assert decisions[0].state is CandidateState.DISPATCHING


def test_medium_tier_opens_an_issue_only(simulate_config: PipelineConfig) -> None:
    """§6 — a medium-tier candidate opens an issue and no PR."""
    candidate = high_candidate(candidate_id="codeql-medium", score=40.0)

    decisions = dispatch_candidates([candidate], simulate_config)

    assert decisions[0].tier is Tier.MEDIUM
    assert decisions[0].action is Action.OPEN_ISSUE


def test_low_tier_is_logged_only(simulate_config: PipelineConfig) -> None:
    """§6 — a low-tier candidate is recorded and nothing is created."""
    candidate = high_candidate(candidate_id="codeql-low", score=12.0)

    decisions = dispatch_candidates([candidate], simulate_config)

    assert decisions[0].tier is Tier.LOW
    assert decisions[0].action is Action.LOG_ONLY
    assert decisions[0].state is CandidateState.TERMINAL


def test_budget_dispatches_exactly_ten_and_defers_the_rest(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — >10 gate-passing high-score candidates dispatch 10 and record the rest deferred."""
    candidates = [
        high_candidate(candidate_id=f"codeql-{index:02d}", score=190.0 - index)
        for index in range(14)
    ]

    decisions = dispatch_candidates(candidates, simulate_config)

    dispatched = [row for row in decisions if row.state is CandidateState.DISPATCHING]
    deferred = [row for row in decisions if row.state is CandidateState.DEFERRED]
    assert simulate_config.budget_N == 10
    assert len(dispatched) == 10
    assert [row.candidate_id for row in deferred] == [f"codeql-{i:02d}" for i in range(10, 14)]
    assert {row.action for row in deferred} == {Action.DEFERRED}


def test_non_default_budget_knob_takes_effect_in_simulate() -> None:
    """§17 — a changed knob changes behaviour with no code edit."""
    config = pipeline_config(budget_N=3)
    candidates = [
        high_candidate(candidate_id=f"codeql-{index}", score=190.0 - index) for index in range(6)
    ]

    decisions = dispatch_candidates(candidates, config)

    assert len([row for row in decisions if row.state is CandidateState.DISPATCHING]) == 3
    assert len([row for row in decisions if row.state is CandidateState.DEFERRED]) == 3


def test_budget_dispatches_highest_scores_first(simulate_config: PipelineConfig) -> None:
    """§6 — the budget is spent on the highest scores; ties break on candidate ID."""
    candidates = [
        high_candidate(candidate_id=f"codeql-{index:02d}", score=60.0 + index)
        for index in range(12)
    ]

    decisions = dispatch_candidates(candidates, simulate_config)

    dispatched = {row.candidate_id for row in decisions if row.state is CandidateState.DISPATCHING}
    deferred = {row.candidate_id for row in decisions if row.state is CandidateState.DEFERRED}
    assert dispatched == {f"codeql-{index:02d}" for index in range(2, 12)}
    assert deferred == {"codeql-00", "codeql-01"}


def test_dispatch_preserves_input_order_in_its_output(simulate_config: PipelineConfig) -> None:
    """The decision list is aligned with the input so the state store stays stable."""
    candidates = [
        high_candidate(candidate_id=f"codeql-{index}", score=100.0 - index) for index in range(4)
    ]

    decisions = dispatch_candidates(list(reversed(candidates)), simulate_config)

    assert [row.candidate_id for row in decisions] == [
        f"codeql-{index}" for index in reversed(range(4))
    ]


# -- gate-failure routing ----------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", sorted(HUMAN_ROUTED_REASONS, key=lambda code: ReasonCode(code).value)
)
def test_human_routed_reasons_open_a_human_review_row(
    simulate_config: PipelineConfig, reason: ReasonCode
) -> None:
    """§4.2 — a human-routed hard condition is labelled for review, never silently dropped."""
    candidate = gated_out(lane2_candidate(), GateName.AUTOMATABILITY, reason)

    decision = dispatch_candidates([candidate], simulate_config)[0]

    assert decision.action is Action.HUMAN_REVIEW
    assert decision.reason is reason
    assert NEEDS_HUMAN_REVIEW_LABEL in decision.labels
    assert decision.auto_merge_eligible is False


def test_internal_caller_is_human_routed_not_dropped(simulate_config: PipelineConfig) -> None:
    """§4.2 — the LANE 3 caller gate routes to a human; it is not a drop."""
    candidate = gated_out(
        lane3_candidate(internal_caller=True, caller_count=3),
        GateName.AUTOMATABILITY,
        ReasonCode.INTERNAL_CALLER,
    )

    decision = dispatch_candidates([candidate], simulate_config)[0]

    assert ReasonCode.INTERNAL_CALLER in HUMAN_ROUTED_REASONS
    assert ReasonCode.INTERNAL_CALLER not in DROPPED_REASONS
    assert decision.action is Action.HUMAN_REVIEW


def test_not_eol_candidates_are_dropped_for_their_own_reason(
    simulate_config: PipelineConfig,
) -> None:
    """§4.2 — a LANE 3 candidate that is not yet EOL is dropped as `not_eol`."""
    candidate = lane3_candidate(deprecated_in="6.0").model_copy(
        update={"gate_passed": False, "reason": ReasonCode.NOT_EOL}
    )

    decision = dispatch_candidates([candidate], simulate_config)[0]

    assert ReasonCode.NOT_EOL in DROPPED_REASONS
    assert ReasonCode.NOT_EOL not in HUMAN_ROUTED_REASONS
    assert decision.action is Action.LOG_ONLY
    assert decision.state is CandidateState.TERMINAL
    assert decision.reason is ReasonCode.NOT_EOL


def test_out_of_scope_frontend_is_dropped(simulate_config: PipelineConfig) -> None:
    """§5 — a non-Python alert path is out of scope for this iteration and is dropped."""
    candidate = gated_out(
        codeql_candidate(file_path="superset-frontend/src/x.ts"),
        GateName.VERIFIABILITY_EXISTS,
        ReasonCode.OUT_OF_SCOPE_FRONTEND,
    )

    decision = dispatch_candidates([candidate], simulate_config)[0]

    assert decision.action is Action.LOG_ONLY
    assert decision.reason is ReasonCode.OUT_OF_SCOPE_FRONTEND


def test_every_hard_condition_reason_has_a_dispatch_route(
    simulate_config: PipelineConfig,
) -> None:
    """No gate reason may reach dispatch without a route: §4's reasons are all covered."""
    assert HARD_CONDITION_REASONS <= (HUMAN_ROUTED_REASONS | DROPPED_REASONS)
    assert simulate_config.mode is Mode.SIMULATE


# -- §9 stale skip: a valid terminal remediation, not a drop -----------------------------


def stale_skip_candidate() -> Candidate:
    """A LANE 2 candidate whose reviewer red-baseline run passed at `base_sha`."""
    return lane2_candidate(
        candidate_id="lane2-stale",
        gate_passed=True,
        score=128.0,
        risk=1,
        reason=ReasonCode.STALE_SKIP,
        red_baseline=RedBaselineResult(
            status=BaselineStatus.STALE_SKIP,
            representative_nodeid=(
                "tests/integration_tests/sqllab_tests.py::TestSqlLab::test_run_sync_query"
            ),
        ),
    )


def test_stale_skip_is_not_a_dropped_reason() -> None:
    """§9 (line 492) — `stale_skip` is a *valid terminal outcome*, exempt from red->green.

    The remediation is deleting the dead marker, so the reason must not be routed as a drop.
    """
    assert ReasonCode.STALE_SKIP not in DROPPED_REASONS


def test_stale_skip_still_ships_a_reviewer_only_diff(simulate_config: PipelineConfig) -> None:
    """§9 — a stale skip is remediated: the candidate keeps its artifact-creating action."""
    decision = dispatch_candidates([stale_skip_candidate()], simulate_config)[0]

    assert decision.tier is Tier.HIGH
    assert decision.action is Action.OPEN_PR
    assert decision.state is CandidateState.DISPATCHING
    assert decision.reason is ReasonCode.STALE_SKIP


def test_stale_skip_is_exempt_from_the_red_to_green_requirement(
    simulate_config: PipelineConfig,
) -> None:
    """§9 — the exemption is the point: no red baseline is required to ship the marker deletion."""
    candidate = stale_skip_candidate().model_copy(update={"expected_failure": None})

    decision = dispatch_candidates([candidate], simulate_config)[0]

    assert decision.action is Action.OPEN_PR
    assert decision.auto_merge_eligible is not None


# -- auto-merge gating -------------------------------------------------------------------


def test_auto_merge_when_every_precondition_holds() -> None:
    """§6 — green CI, a reviewer test, no unresolved major: eligible."""
    config = github_config()

    decision = _api.dispatch().auto_merge_eligible(
        high_candidate(risk=2),
        config,
        findings=[ReviewFinding(severity="minor", resolved=True)],
        ci_green=True,
        test_added=True,
        existing_suite_green=True,
    )

    assert decision.eligible is True
    assert NEEDS_HUMAN_REVIEW_LABEL not in decision.labels


def test_unresolved_major_blocks_auto_merge_and_labels_for_human_review() -> None:
    """§17 — a converged PR with an unresolved `major` and no `blocking` is not eligible."""
    config = github_config()

    decision = _api.dispatch().auto_merge_eligible(
        high_candidate(risk=2),
        config,
        findings=[ReviewFinding(severity="major", note="unresolved", resolved=False)],
        ci_green=True,
        test_added=True,
        existing_suite_green=True,
    )

    assert decision.eligible is False
    assert NEEDS_HUMAN_REVIEW_LABEL in decision.labels


def test_unresolved_major_never_auto_merges_at_dispatch() -> None:
    """The structural invariant, enforced on the dispatch row itself."""
    config = github_config()
    candidate = high_candidate(risk=1, unresolved_major=True)

    decision = dispatch_candidates([candidate], config)[0]

    assert decision.auto_merge_eligible is False
    assert NEEDS_HUMAN_REVIEW_LABEL in decision.labels


def test_major_only_requires_human_false_still_blocks_auto_merge() -> None:
    """§17 — the knob may drop the human-review routing, never the auto-merge block."""
    config = github_config(major_only_requires_human=False)
    candidate = high_candidate(risk=1, unresolved_major=True)

    decision = dispatch_candidates([candidate], config)[0]

    assert decision.auto_merge_eligible is False


def test_high_risk_candidate_is_labelled_and_never_auto_merged() -> None:
    """§6 — high score with `risk >= 3` opens a PR labeled `needs-human-review`."""
    config = github_config()

    decision = dispatch_candidates([high_candidate(risk=4)], config)[0]

    assert decision.action is Action.OPEN_PR
    assert decision.auto_merge_eligible is False
    assert NEEDS_HUMAN_REVIEW_LABEL in decision.labels


def test_existing_suite_regression_blocks_auto_merge() -> None:
    """§17 — breaking a mocked existing test blocks the merge even with a green reviewer test."""
    config = github_config()

    decision = _api.dispatch().auto_merge_eligible(
        high_candidate(risk=1),
        config,
        findings=[],
        ci_green=False,
        test_added=True,
        existing_suite_green=False,
    )

    assert decision.eligible is False


def test_missing_reviewer_test_blocks_auto_merge() -> None:
    """§9 — no reviewer test means no auto-merge."""
    config = github_config()

    decision = _api.dispatch().auto_merge_eligible(
        high_candidate(risk=1),
        config,
        findings=[],
        ci_green=True,
        test_added=False,
        existing_suite_green=True,
    )

    assert decision.eligible is False


def test_local_ci_mode_forces_auto_merge_off() -> None:
    """§10/§17 — local evidence disables auto-merge at config level and at dispatch level."""
    config = pipeline_config(ci_evidence_mode=CiEvidenceMode.LOCAL, auto_merge_enabled=True)

    decision = dispatch_candidates([high_candidate(risk=1)], config)[0]

    assert config.auto_merge_enabled is False
    assert decision.auto_merge_eligible is False


# -- §7 preflight and dual artifacts -----------------------------------------------------


def test_dispatch_preflight_aborts_when_issues_disabled(simulate_config: PipelineConfig) -> None:
    """§7 — `has_issues == false` aborts before any write unless the sink is `pr_comment`."""
    client = FakeGitHubClient(has_issues=False)

    result = _api.dispatch().preflight(simulate_config, client=client)

    assert result.ok is False
    assert result.aborted_before_write is True
    assert result.reason == ReasonCode.CAPABILITY_UNAVAILABLE
    assert client.writes == []


def test_preflight_allows_degraded_pr_comment_sink() -> None:
    """§7 — the degraded sink keeps the run going and records `artifact_degraded`."""
    config = pipeline_config(issue_sink=IssueSink.PR_COMMENT)
    client = FakeGitHubClient(has_issues=False)

    result = _api.dispatch().preflight(config, client=client)

    assert result.ok is True
    assert result.issue_sink == IssueSink.PR_COMMENT
    assert result.reason == ReasonCode.ARTIFACT_DEGRADED
    assert client.writes == []


def test_crosslink_roundtrip(simulate_config: PipelineConfig) -> None:
    """§7 — issue first, then the PR carrying `Closes #<n>`, then the issue body is patched."""
    client = FakeGitHubClient()
    candidate = high_candidate(candidate_id="codeql-7")
    marker = _api.dedupe().marker(candidate.candidate_id)

    result = _api.dispatch().create_artifacts(
        candidate,
        live_config(),
        client=client,
        pr_title="fix: bound the generated range",
        pr_body=f"### SUMMARY\n\n{marker}\n",
        issue_body=f"### SUMMARY\n\n{marker}\n",
    )

    assert client.write_names == ["create_issue", "create_pull_request", "update_issue"]
    assert result.issue_number == 101
    assert result.state == CandidateState.PR_CREATED
    pr_body = dict(client.writes[1][1])["body"]
    patched_issue = dict(client.writes[2][1])["body"]
    assert f"Closes #{result.issue_number}" in str(pr_body)
    assert marker in str(pr_body)
    assert marker in str(patched_issue)
    assert result.pr_url is not None and result.pr_url in str(patched_issue)
    assert simulate_config.issue_sink == IssueSink.ISSUES


def test_resume_after_issue_created_pr_failed(simulate_config: PipelineConfig) -> None:
    """§14.1 — replaying the canonical resume state creates no second issue."""
    dispatch = _api.dispatch()
    candidate = high_candidate(candidate_id="codeql-9")
    marker = _api.dedupe().marker(candidate.candidate_id)
    live = live_config()
    client = FakeGitHubClient(fail_pr_creation=True)

    with pytest.raises(RuntimeError):
        dispatch.create_artifacts(
            candidate,
            live,
            client=client,
            pr_title="fix: bound the generated range",
            pr_body=f"### SUMMARY\n\n{marker}\n",
            issue_body=f"### SUMMARY\n\n{marker}\n",
        )
    assert client.write_names.count("create_issue") == 1

    client.fail_pr_creation = False
    replay = dispatch.create_artifacts(
        candidate,
        live,
        client=client,
        pr_title="fix: bound the generated range",
        pr_body=f"### SUMMARY\n\n{marker}\n",
        issue_body=f"### SUMMARY\n\n{marker}\n",
    )

    assert client.write_names.count("create_issue") == 1
    assert replay.issue_number == 101
    assert replay.state == CandidateState.PR_CREATED
    assert simulate_config.mode == Mode.SIMULATE


def test_pr_comment_sink_state_transition_and_validation(tmp_path: Path) -> None:
    """§7 degraded path — `dispatching -> pr_created -> comment_created` plus a local report."""
    config = live_config(issue_sink=IssueSink.PR_COMMENT)
    client = FakeGitHubClient(has_issues=False)
    candidate = high_candidate(candidate_id="codeql-11")
    render = _api.render()
    issue_body = render.render_issue_body(candidate)

    result = _api.dispatch().create_artifacts(
        candidate,
        config,
        client=client,
        pr_title="fix: bound the generated range",
        pr_body=render.render_pr_body(candidate, implementation_plan="plan", tests="tests"),
        issue_body=issue_body,
        reports_dir=tmp_path,
    )

    assert client.write_names == ["create_pull_request", "create_comment"]
    assert result.issue_number is None
    assert result.state == CandidateState.COMMENT_CREATED
    comment_body = str(dict(client.writes[1][1])["body"])
    validation = render.validate_issue_body(comment_body, render.select_issue_template(candidate))
    assert validation.valid is True
    assert (tmp_path / "issues" / f"{candidate.candidate_id}.md").is_file()


def test_simulate_mode_makes_no_remote_writes(simulate_config: PipelineConfig) -> None:
    """§17 — SIMULATE performs zero remote writes; the fake fails the test if one is attempted."""
    client = NoWriteGitHubClient()
    candidate = high_candidate(candidate_id="codeql-13")

    result = _api.dispatch().create_artifacts(
        candidate,
        simulate_config,
        client=client,
        pr_title="fix: bound the generated range",
        pr_body="### SUMMARY\n",
        issue_body="### SUMMARY\n",
    )

    assert client.writes == []
    assert result.issue_number is None
    assert result.pr_url is None


# -- containment and the blocked-child lifecycle -----------------------------------------


def test_containment_suppresses_child_and_records_related_ids(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — where the parent is dispatchable, the child is suppressed and both rows link."""
    parent = lane2_candidate(
        candidate_id="lane2-parent",
        nodeid=PARENT_NODEID,
        enclosed_tests=3,
        score=128.0,
        risk=2,
        gate_passed=True,
    )
    child = lane2_candidate(
        candidate_id="lane2-child",
        nodeid=CHILD_NODEID,
        enclosing_skip_nodeid=PARENT_NODEID,
        score=120.0,
        risk=2,
        gate_passed=True,
    )

    decisions = dispatch_candidates([parent, child], simulate_config)

    dispatched_parent = decision_for("lane2-parent", decisions)
    suppressed_child = decision_for("lane2-child", decisions)
    assert dispatched_parent.action is Action.OPEN_PR
    assert suppressed_child.state is CandidateState.SUPPRESSED_BY_CONTAINMENT
    assert suppressed_child.reason is ReasonCode.SUPPRESSED_BY_CONTAINMENT
    assert suppressed_child.related_candidate_id == "lane2-parent"
    assert dispatched_parent.related_candidate_id == "lane2-child"


def test_child_under_gate_failed_parent_not_dispatched(simulate_config: PipelineConfig) -> None:
    """§17 — a child of a breadth-gate failure is gated `blocked_by_enclosing_skip`.

    Where the parent passes instead, containment suppresses the child and both rows link.
    """
    broad_parent = gated_out(
        lane2_candidate(
            candidate_id="lane2-parent",
            nodeid=PARENT_NODEID,
            kind=DefinitionKind.CLASS,
            enclosed_tests=40,
        ),
        GateName.AUTOMATABILITY,
        ReasonCode.CLASS_SCOPE_TOO_BROAD,
    )
    child = lane2_candidate(
        candidate_id="lane2-child",
        nodeid=CHILD_NODEID,
        enclosing_skip_nodeid=PARENT_NODEID,
        score=120.0,
        risk=2,
        gate_passed=True,
    )

    decisions = dispatch_candidates([broad_parent, child], simulate_config)

    parent_row = decision_for("lane2-parent", decisions)
    child_row = decision_for("lane2-child", decisions)
    assert parent_row.action is Action.HUMAN_REVIEW
    assert child_row.state is CandidateState.BLOCKED_BY_ENCLOSING_SKIP
    assert child_row.reason is ReasonCode.BLOCKED_BY_ENCLOSING_SKIP
    assert child_row.action is not Action.OPEN_PR


def test_child_blocked_when_parent_not_dispatched_for_any_reason(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — dropped, low-tier or budget-deferred parents all block the child."""
    parent = lane2_candidate(
        candidate_id="lane2-parent",
        nodeid=PARENT_NODEID,
        enclosed_tests=3,
        score=10.0,
        risk=2,
        gate_passed=True,
    )
    child = lane2_candidate(
        candidate_id="lane2-child",
        nodeid=CHILD_NODEID,
        enclosing_skip_nodeid=PARENT_NODEID,
        score=120.0,
        risk=2,
        gate_passed=True,
    )

    decisions = dispatch_candidates([parent, child], simulate_config)

    blocked = decision_for("lane2-child", decisions)
    assert decision_for("lane2-parent", decisions).action is Action.LOG_ONLY
    assert blocked.state is CandidateState.BLOCKED_BY_ENCLOSING_SKIP
    assert blocked.action is Action.HUMAN_REVIEW
    assert NEEDS_HUMAN_REVIEW_LABEL in blocked.labels


def test_blocked_child_redispatches_once_ancestor_marker_gone(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — the blocked row is non-terminal: with the ancestor marker gone it dispatches."""
    blocked = lane2_candidate(
        candidate_id="lane2-child",
        nodeid=CHILD_NODEID,
        enclosing_skip_nodeid=PARENT_NODEID,
        score=120.0,
        risk=2,
        gate_passed=True,
        state=CandidateState.BLOCKED_BY_ENCLOSING_SKIP,
    )
    later = blocked.model_copy(update={"enclosing_skip_nodeid": None})

    decisions = dispatch_candidates([later], simulate_config)

    assert decisions[0].state is CandidateState.DISPATCHING
    assert decisions[0].action is Action.OPEN_PR
