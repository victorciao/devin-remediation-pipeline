"""§9 review loop: criterion mapping, convergence, the iteration cap and auto-merge."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import pytest

from pipeline.config import PipelineConfig
from pipeline.review_loop import (
    FindingSeverity,
    ReviewFinding,
    ReviewIteration,
    apply_review_result,
    evaluate_review_iteration,
    review_iteration_from_payload,
    run_review_loop,
)
from pipeline.schemas import (
    NEEDS_HUMAN_REVIEW_LABEL,
    Action,
    BaselineStatus,
    Candidate,
    CandidateState,
    ReasonCode,
    RetryDecision,
)
from pipeline.session_client import (
    OrchestrationResult,
    RoleRun,
    SessionAttempt,
    SessionRole,
    SessionSnapshot,
)
from pipeline.session_client import (
    _candidate_diff_review_matches as candidate_diff_review,
)
from tests.factories import codeql_candidate, lane2_candidate

CRITERIA = frozenset({"AC-1"})
EXPECTED_FAILURE = {
    "nodeid": "tests/x.py::a",
    "exception_type": "AssertionError",
    "message_pattern": "assert 400 == 200",
}
OBSERVED_FAILURE = {
    "nodeid": "tests/x.py::a",
    "outcome": "FAILED",
    "exception_type": "AssertionError",
    "message": "assert 400 == 200",
}


def iteration(**overrides: object) -> ReviewIteration:
    """A complete iteration: §12.1 makes a recorded `diff_reviewed` a convergence precondition."""
    fields: dict[str, object] = {
        "red_baseline": BaselineStatus.VALID,
        "green": True,
        "planner_criteria": CRITERIA,
        "reviewer_criteria": CRITERIA,
        "addressed_criteria": CRITERIA,
        "diff_reviewed": True,
    }
    fields.update(overrides)
    return ReviewIteration(**fields)  # type: ignore[arg-type]


def eligible_candidate() -> Candidate:
    """A candidate dispatch already found auto-merge eligible; §13 narrows monotonically."""
    return codeql_candidate(gate_passed=True, score=128.0, risk=1, auto_merge_eligible=True)


# -- §12.1 criterion mapping -------------------------------------------------------------


def test_reviewer_test_without_mapped_criterion_is_rejected() -> None:
    """§17/§12.1 — a reviewer test whose `criterion_id` is unknown blocks convergence."""
    unmapped = iteration(reviewer_criteria=frozenset({"AC-1", "AC-9"}))

    decision = evaluate_review_iteration(unmapped)

    assert decision is None


def test_unaddressed_planner_criterion_blocks_convergence() -> None:
    """§12.1 — every planner criterion must be addressed before the loop can settle."""
    unaddressed = iteration(
        planner_criteria=frozenset({"AC-1", "AC-2"}),
        reviewer_criteria=frozenset({"AC-1", "AC-2"}),
        addressed_criteria=frozenset({"AC-1"}),
    )

    assert evaluate_review_iteration(unaddressed) is None


def test_fully_mapped_reviewer_tests_are_accepted() -> None:
    decision = evaluate_review_iteration(iteration())

    assert decision is not None
    assert decision.converged is True
    assert decision.state is CandidateState.CONVERGED
    assert decision.reason is None


def test_convergence_preserves_candidate_auto_merge_eligibility() -> None:
    """§13 — the loop no longer decides eligibility; it may only leave dispatch's answer intact."""
    result = run_review_loop(PipelineConfig(), iteration())

    assert apply_review_result(eligible_candidate(), result).auto_merge_eligible is True


def test_missing_diff_review_blocks_convergence_without_spending_the_cap() -> None:
    """§9.3/§12.1 — no convergence without a recorded diff review, and the cap is not consumed."""
    incomplete = iteration(diff_reviewed=False)

    result = run_review_loop(PipelineConfig(iteration_cap=5), incomplete)

    assert result.converged is False
    assert result.iterations == 0
    assert result.needs_human_review is True
    assert result.disagreement_summary is not None
    assert "reviewer diff review incomplete" in result.disagreement_summary
    assert apply_review_result(eligible_candidate(), result).auto_merge_eligible is False


def test_a_rerun_that_records_its_diff_review_converges() -> None:
    """§9.3 — one incomplete iteration is rerun; the reviewed rerun converges."""
    reruns: list[int] = []

    def rerun(ordinal: int) -> ReviewIteration:
        reruns.append(ordinal)
        return iteration()

    result = run_review_loop(PipelineConfig(), iteration(diff_reviewed=False), rerun)

    assert result.converged is True
    assert reruns == [1]


def test_two_iterations_without_a_diff_review_escalate() -> None:
    """§12.1 — an unreviewed diff escalates as `diff_review_incomplete`, not a disagreement.

    A LIVE run terminated every candidate at `iterations=0` with `disagreement_unresolved`
    when no review had happened at all: there was no disagreement to read, and the reason
    code hid the fact that the reviewer never looked at the implementer's diff.
    """
    incomplete = iteration(diff_reviewed=False)

    result = run_review_loop(PipelineConfig(), incomplete, lambda _ordinal: incomplete)

    assert result.converged is False
    assert result.reason is ReasonCode.DIFF_REVIEW_INCOMPLETE
    assert result.needs_human_review is True


def test_an_implementer_test_edit_outranks_an_incomplete_diff_review() -> None:
    """§9.2/§12.1 — the structural violation is reported ahead of the missing review."""
    violation = iteration(
        diff_reviewed=False,
        findings=(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "implementer edited tests/x.py",
                reason=ReasonCode.IMPLEMENTER_TEST_EDIT,
            ),
        ),
    )

    result = run_review_loop(PipelineConfig(), violation, lambda _ordinal: violation)

    assert result.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_an_incomplete_diff_review_outranks_an_unresolved_major() -> None:
    """§12.1 — an unresolved major says nothing while the diff is still unreviewed."""
    unreviewed_major = iteration(
        diff_reviewed=False,
        findings=(ReviewFinding(FindingSeverity.MAJOR, "AC-1", "still red"),),
    )

    result = run_review_loop(PipelineConfig(), unreviewed_major, lambda _ordinal: unreviewed_major)

    assert result.reason is ReasonCode.DIFF_REVIEW_INCOMPLETE


def test_a_missing_role_commit_outranks_an_incomplete_diff_review() -> None:
    """§9.2 — with no role commit on the branch there is no diff for anyone to have read.

    Reporting `diff_review_incomplete` here would send a human looking for a review that
    could not have existed, when the branch itself never received the work.
    """
    missing = iteration(
        diff_reviewed=False,
        findings=(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "no production commit on devin/codeql-1",
                reason=ReasonCode.ROLE_COMMIT_MISSING,
            ),
        ),
    )

    result = run_review_loop(PipelineConfig(), missing, lambda _ordinal: missing)
    applied = apply_review_result(eligible_candidate(), result)

    assert result.reason is ReasonCode.ROLE_COMMIT_MISSING
    assert applied.state is CandidateState.TERMINAL
    assert applied.auto_merge_eligible is False


def test_an_implementer_test_edit_outranks_a_missing_role_commit() -> None:
    """§9.2 — the test-authorship violation is the most severe structural failure."""
    both = iteration(
        diff_reviewed=False,
        findings=(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "no test commit on devin/codeql-1",
                reason=ReasonCode.ROLE_COMMIT_MISSING,
            ),
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "implementer edited tests/x.py",
                reason=ReasonCode.IMPLEMENTER_TEST_EDIT,
            ),
        ),
    )

    result = run_review_loop(PipelineConfig(), both, lambda _ordinal: both)

    assert result.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_an_unreviewed_disagreement_is_incomplete_but_still_named() -> None:
    """§9 (l.426) — an iteration without a diff review is incomplete, whatever else it found.

    The empty-`committed_diff` gate raises a `disagreement_unresolved` finding without any diff
    review, so the two labels compete. "Incomplete" is the honest one — nobody read the diff, so
    no disagreement was adjudicated — and the §9.5 handoff summary has to say so, while the same
    detection *with* a review is a real adjudicated disagreement.
    """
    detected = iteration(
        diff_reviewed=False,
        findings=(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "implementer reported criteria addressed with an empty committed diff",
                reason=ReasonCode.DISAGREEMENT_UNRESOLVED,
            ),
        ),
    )

    result = run_review_loop(PipelineConfig(), detected, lambda _ordinal: detected)

    assert result.reason is ReasonCode.DIFF_REVIEW_INCOMPLETE
    assert result.disagreement_summary is not None
    assert "reviewer diff review incomplete" in result.disagreement_summary

    reviewed = replace(detected, diff_reviewed=True)
    adjudicated = run_review_loop(PipelineConfig(), reviewed, lambda _ordinal: reviewed)

    assert adjudicated.reason is ReasonCode.DISAGREEMENT_UNRESOLVED


def test_a_finding_carries_the_file_and_line_it_was_raised_against() -> None:
    """§12.1 — a rerun prompt can only cite a finding's location if the loop kept it."""
    normalized = review_iteration_from_payload(
        {"criteria": []},
        {
            "tests": [],
            "red_baseline": {"status": "valid"},
            "green_result": {"passed": True},
            "findings": [
                {
                    "severity": "blocking",
                    "criterion_id": None,
                    "note": "off-by-one",
                    "file": "superset/x.py",
                    "line": 42,
                }
            ],
        },
    )

    assert normalized.findings[0].file == "superset/x.py"
    assert normalized.findings[0].line == "42"


def test_a_reviewed_unresolved_major_still_reports_a_disagreement() -> None:
    """§12.1 — with `diff_reviewed` true the reviewer really did disagree."""
    reviewed_major = iteration(
        diff_reviewed=True,
        findings=(ReviewFinding(FindingSeverity.MAJOR, "AC-1", "unresolved"),),
    )

    result = run_review_loop(
        PipelineConfig(iteration_cap=1), reviewed_major, lambda _ordinal: reviewed_major
    )

    assert result.converged is False
    assert result.reason is ReasonCode.DISAGREEMENT_UNRESOLVED


def test_an_incomplete_diff_review_counts_as_an_unresolved_major() -> None:
    """§13 — an unreviewed diff is as blocking as a disagreement: nothing vouched for it."""
    incomplete = iteration(diff_reviewed=False)

    result = run_review_loop(PipelineConfig(), incomplete, lambda _ordinal: incomplete)
    applied = apply_review_result(eligible_candidate(), result)

    assert applied.reason is ReasonCode.DIFF_REVIEW_INCOMPLETE
    assert applied.state is CandidateState.TERMINAL
    assert applied.unresolved_major is True
    assert applied.auto_merge_eligible is False


def test_the_terminal_row_carries_the_disagreement_summary() -> None:
    """§11 — the escalation report reads `disagreement_summary` off the candidate row."""
    incomplete = iteration(diff_reviewed=False, failing_test="tests/x.py::a")

    result = run_review_loop(PipelineConfig(), incomplete, lambda _ordinal: incomplete)
    applied = apply_review_result(eligible_candidate(), result)

    assert applied.disagreement_summary == result.disagreement_summary
    assert applied.disagreement_summary is not None
    assert "reviewer diff review incomplete" in applied.disagreement_summary


def test_role_payloads_normalize_into_one_loop_input() -> None:
    """§12.1 — the loop consumes the three structured outputs, not free text.

    `diff_reviewed` is supplied by the caller because the authoritative, candidate-aware
    validator owns that judgement; normalization never re-derives a weaker version of it.
    """
    normalized = review_iteration_from_payload(
        {
            "criteria": [
                {
                    "id": "AC-1",
                    "statement": "returns indexes",
                    "expected_failure": EXPECTED_FAILURE,
                }
            ]
        },
        {
            "tests": [{"path": "tests/x.py", "nodeid": "tests/x.py::a", "criterion_id": "AC-1"}],
            "red_baseline": {"observed": {"per_item_outcomes": [OBSERVED_FAILURE]}},
            "green_result": {"passed": True},
            "findings": [{"severity": "minor", "criterion_id": "AC-1", "note": "naming"}],
            "diff_reviewed": {
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
                "files_read": ["superset/x.py"],
            },
        },
        {"files_changed": ["superset/x.py"], "criteria_addressed": ["AC-1"], "commands_run": []},
        diff_reviewed=True,
    )

    assert normalized.planner_criteria == CRITERIA
    assert normalized.reviewer_criteria == CRITERIA
    assert normalized.addressed_criteria == CRITERIA
    assert normalized.red_baseline is BaselineStatus.VALID
    assert normalized.green is True
    assert normalized.diff_reviewed is True
    assert normalized.findings[0].severity is FindingSeverity.MINOR


REVIEW_BASE_SHA = "a" * 40
REVIEW_HEAD_SHA = "b" * 40
COMMITTED_DIFF = """\
--- a/superset/x.py
+++ b/superset/x.py
@@
-    return None
+    return indexes
"""


def role_run(role: SessionRole, structured_output: Mapping[str, object]) -> RoleRun:
    """A finished role run carrying one structured output, with no transport involved."""
    session_id = f"{role.value}-1"
    return RoleRun(
        SessionAttempt(
            role=role,
            candidate_id="codeql-0",
            attempt=1,
            session_id=session_id,
            is_new_session_raw=True,
            retry_decision=RetryDecision.PROCEED,
        ),
        SessionSnapshot(session_id, "finished", {"structured_output": structured_output}),
    )


def diff_review(**overrides: object) -> Mapping[str, object]:
    """A structurally complete diff-review block over the implementer's changed path."""
    review: dict[str, object] = {
        "base_sha": REVIEW_BASE_SHA,
        "head_sha": REVIEW_HEAD_SHA,
        "files_read": ["superset/x.py"],
    }
    review.update(overrides)
    return review


DIFF_REVIEW_CLAIMS: tuple[tuple[str, object], ...] = (
    ("complete and on the candidate revision", diff_review()),
    ("a stale head", diff_review(head_sha="c" * 40)),
    ("a foreign base", diff_review(base_sha="d" * 40)),
    ("a file it never read", diff_review(files_read=[])),
    ("a blank head", diff_review(head_sha="")),
    ("no shas at all", {"files_read": ["superset/x.py"]}),
    ("a bare boolean", True),
)


@pytest.mark.parametrize(("description", "claim"), DIFF_REVIEW_CLAIMS)
def test_the_loop_never_sees_a_diff_review_the_validator_rejects(
    description: str,
    claim: object,
) -> None:
    """§9.3/§12.1 — one validator decides, and normalization has no second opinion.

    The loop's `diff_reviewed` comes from the authoritative candidate-aware validator and
    from nowhere else: a reviewer payload the validator rejects cannot reach the loop as a
    completed review, whatever the payload claims, and the default is fail-closed.
    """
    reviewer_output = {
        "tests": [{"path": "tests/x.py", "nodeid": "tests/x.py::a", "criterion_id": "AC-1"}],
        "red_baseline": {"observed": {"per_item_outcomes": [OBSERVED_FAILURE]}},
        "green_result": {"passed": True},
        "findings": [],
        "diff_reviewed": claim,
    }
    implementer_output = {
        "files_changed": ["superset/x.py"],
        "criteria_addressed": ["AC-1"],
        "commands_run": [],
        "committed_diff": COMMITTED_DIFF,
    }
    planner_output = {"criteria": [{"id": "AC-1", "expected_failure": EXPECTED_FAILURE}]}
    result = OrchestrationResult(
        role_run(SessionRole.PLANNER, planner_output),
        role_run(SessionRole.IMPLEMENTER, implementer_output),
        role_run(SessionRole.REVIEWER, reviewer_output),
    )
    reviewed = codeql_candidate(base_sha=REVIEW_BASE_SHA, head_sha=REVIEW_HEAD_SHA)
    authoritative = candidate_diff_review(result, reviewed)

    assert authoritative is (claim == diff_review())
    assert (
        review_iteration_from_payload(
            planner_output,
            reviewer_output,
            implementer_output,
            diff_reviewed=authoritative,
        ).diff_reviewed
        is authoritative
    )
    assert (
        review_iteration_from_payload(
            planner_output,
            reviewer_output,
            implementer_output,
        ).diff_reviewed
        is False
    )


def test_a_reviewer_baseline_without_observed_items_is_invalid() -> None:
    """§9.1 — a status claim with no per-item observation is not a red baseline."""
    normalized = review_iteration_from_payload(
        {"criteria": [{"id": "AC-1", "expected_failure": EXPECTED_FAILURE}]},
        {
            "tests": [],
            "red_baseline": {"status": "valid"},
            "green_result": {"passed": True},
            "findings": [],
        },
    )

    assert normalized.red_baseline is BaselineStatus.INVALID_RED_BASELINE


def test_unknown_finding_severity_is_treated_as_blocking() -> None:
    """An unparseable severity must never be silently downgraded to a nit."""
    normalized = review_iteration_from_payload(
        {"criteria": []},
        {
            "tests": [],
            "red_baseline": {"status": "valid"},
            "green_result": {"passed": True},
            "findings": [{"severity": "catastrophic", "criterion_id": None, "note": "?"}],
        },
    )

    assert normalized.findings[0].severity is FindingSeverity.BLOCKING


# -- §9 loop --------------------------------------------------------------------------


@pytest.mark.parametrize("cap", [3, 5])
def test_non_converging_loop_escalates_at_exactly_the_iteration_cap(cap: int) -> None:
    """§17 — escalation happens at exactly `iteration_cap`, parameterized over {3, 5}."""
    config = PipelineConfig(iteration_cap=cap)
    stuck = iteration(green=False)
    reruns: list[int] = []

    def rerun(ordinal: int) -> ReviewIteration:
        reruns.append(ordinal)
        return stuck

    result = run_review_loop(config, stuck, rerun)

    assert result.converged is False
    assert result.iterations == cap
    assert result.reason is ReasonCode.DISAGREEMENT_UNRESOLVED
    assert apply_review_result(eligible_candidate(), result).auto_merge_eligible is False
    assert result.needs_human_review is True
    assert len(reruns) == cap - 1


def test_shipped_default_iteration_cap_is_five(simulate_config: PipelineConfig) -> None:
    assert simulate_config.iteration_cap == 5

    stuck = iteration(green=False)
    result = run_review_loop(simulate_config, stuck, lambda _ordinal: stuck)

    assert result.iterations == 5


def test_still_red_join_never_auto_merges(simulate_config: PipelineConfig) -> None:
    """§9 step 5 — a still-red join goes straight to a human, with no adjudicating session."""
    stuck = iteration(green=False)

    result = run_review_loop(simulate_config, stuck, lambda _ordinal: stuck)

    assert apply_review_result(eligible_candidate(), result).auto_merge_eligible is False
    assert result.needs_human_review is True
    assert result.disagreement_summary is not None


def test_disagreement_summary_carries_the_four_required_facts() -> None:
    """§9 step 5 — failing test, mapped criterion, pre-fix signature and fix rationale."""
    stuck = iteration(
        green=False,
        failing_test="tests/x.py::a",
        pre_fix_signature="AssertionError: assert 400 == 200",
        fix_rationale="widened the accepted status codes",
    )

    result = run_review_loop(PipelineConfig(iteration_cap=1), stuck)

    assert result.disagreement_summary is not None
    for fact in (
        "tests/x.py::a",
        "AC-1",
        "AssertionError: assert 400 == 200",
        "widened the accepted status codes",
    ):
        assert fact in result.disagreement_summary


def test_loop_converges_once_findings_are_resolved(simulate_config: PipelineConfig) -> None:
    rounds = iter(
        [
            iteration(findings=(ReviewFinding(FindingSeverity.NIT, "AC-1", "naming"),)),
        ]
    )

    result = run_review_loop(
        simulate_config,
        iteration(findings=(ReviewFinding(FindingSeverity.MAJOR, "AC-1", "still red"),)),
        lambda _ordinal: next(rounds),
    )

    assert result.converged is True
    assert result.iterations == 2
    assert result.state is CandidateState.CONVERGED
    assert result.reason is None


def test_invalid_red_baseline_is_reauthored_once_then_escalates() -> None:
    """§9.1 — one re-author attempt, then a human handoff."""
    invalid = iteration(red_baseline=BaselineStatus.INVALID_RED_BASELINE)

    result = run_review_loop(PipelineConfig(iteration_cap=5), invalid, lambda _ordinal: invalid)

    assert result.converged is False
    assert result.reason is ReasonCode.DISAGREEMENT_UNRESOLVED
    assert result.iterations == 2


def test_unresolved_major_blocks_auto_merge_with_no_knob_to_relax_it() -> None:
    """§17 (l.1016) — the unresolved-`major` block is evaluated without consulting configuration.

    `evaluate_review_iteration` used to take the routing policy as a keyword, which is the only
    way a caller could have relaxed it; the clause holds now because the decision has no such
    input at all.
    """
    major = iteration(findings=(ReviewFinding(FindingSeverity.MAJOR, "AC-1", "unresolved"),))

    with pytest.raises(TypeError):
        evaluate_review_iteration(major, major_only_requires_human=False)  # type: ignore[call-arg]

    result = run_review_loop(PipelineConfig(iteration_cap=1), major)

    assert result.converged is False
    assert apply_review_result(eligible_candidate(), result).auto_merge_eligible is False


def test_stale_skip_is_a_converged_reviewer_only_outcome() -> None:
    """§9 (line 492) — `stale_skip` is a valid terminal outcome exempt from red→green."""
    stale = ReviewIteration(red_baseline=BaselineStatus.STALE_SKIP, green=False, diff_reviewed=True)

    result = run_review_loop(PipelineConfig(), stale)

    assert result.converged is True
    assert result.reviewer_only is True
    assert result.reason is ReasonCode.STALE_SKIP
    assert apply_review_result(eligible_candidate(), result).action is Action.REVIEWER_ONLY_DIFF


def test_stale_skip_result_ships_as_a_reviewer_only_diff() -> None:
    candidate = lane2_candidate(gate_passed=True, score=128.0, risk=1)
    stale = ReviewIteration(red_baseline=BaselineStatus.STALE_SKIP, green=False, diff_reviewed=True)

    applied = apply_review_result(candidate, run_review_loop(PipelineConfig(), stale))

    assert applied.action is Action.REVIEWER_ONLY_DIFF
    assert applied.state is CandidateState.TERMINAL
    assert applied.reason is ReasonCode.STALE_SKIP


def test_escalated_result_labels_the_candidate_for_humans() -> None:
    candidate = codeql_candidate(gate_passed=True, score=128.0, risk=2)
    stuck = iteration(green=False)

    applied = apply_review_result(
        candidate,
        run_review_loop(PipelineConfig(iteration_cap=1), stuck),
    )

    assert applied.action is Action.HUMAN_REVIEW
    assert NEEDS_HUMAN_REVIEW_LABEL in applied.labels
    assert applied.auto_merge_eligible is False
    assert applied.unresolved_major is True
