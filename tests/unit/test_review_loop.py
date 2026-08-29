"""§9 review loop: criterion mapping, convergence, the iteration cap and auto-merge."""

from __future__ import annotations

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
    CandidateState,
    ReasonCode,
)
from tests.factories import codeql_candidate, lane2_candidate

CRITERIA = frozenset({"AC-1"})


def iteration(**overrides: object) -> ReviewIteration:
    fields: dict[str, object] = {
        "red_baseline": BaselineStatus.VALID,
        "green": True,
        "planner_criteria": CRITERIA,
        "reviewer_criteria": CRITERIA,
        "addressed_criteria": CRITERIA,
    }
    fields.update(overrides)
    return ReviewIteration(**fields)  # type: ignore[arg-type]


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
    assert decision.auto_merge_eligible is True


def test_role_payloads_normalize_into_one_loop_input() -> None:
    """§12.1 — the loop consumes the three structured outputs, not free text."""
    normalized = review_iteration_from_payload(
        {"criteria": [{"id": "AC-1", "statement": "returns indexes"}]},
        {
            "tests": [{"path": "tests/x.py", "nodeid": "tests/x.py::a", "criterion_id": "AC-1"}],
            "red_baseline": {"status": "valid"},
            "green_result": {"passed": True},
            "findings": [{"severity": "minor", "criterion_id": "AC-1", "note": "naming"}],
        },
        {"files_changed": ["superset/x.py"], "criteria_addressed": ["AC-1"], "commands_run": []},
    )

    assert normalized.planner_criteria == CRITERIA
    assert normalized.reviewer_criteria == CRITERIA
    assert normalized.addressed_criteria == CRITERIA
    assert normalized.red_baseline is BaselineStatus.VALID
    assert normalized.green is True
    assert normalized.findings[0].severity is FindingSeverity.MINOR


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
    assert result.auto_merge_eligible is False
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

    assert result.auto_merge_eligible is False
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


def test_unresolved_major_blocks_auto_merge_even_when_humans_are_not_required() -> None:
    """§17 — `major_only_requires_human = false` still blocks auto-merge."""
    config = PipelineConfig(iteration_cap=1, major_only_requires_human=False)
    major = iteration(findings=(ReviewFinding(FindingSeverity.MAJOR, "AC-1", "unresolved"),))

    result = run_review_loop(config, major)

    assert result.auto_merge_eligible is False


def test_stale_skip_is_a_converged_reviewer_only_outcome() -> None:
    """§9 (line 492) — `stale_skip` is a valid terminal outcome exempt from red→green."""
    stale = ReviewIteration(red_baseline=BaselineStatus.STALE_SKIP, green=False)

    result = run_review_loop(PipelineConfig(), stale)

    assert result.converged is True
    assert result.reviewer_only is True
    assert result.reason is ReasonCode.STALE_SKIP
    assert result.auto_merge_eligible is False


def test_stale_skip_result_ships_as_a_reviewer_only_diff() -> None:
    candidate = lane2_candidate(gate_passed=True, score=128.0, risk=1)
    stale = ReviewIteration(red_baseline=BaselineStatus.STALE_SKIP, green=False)

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
