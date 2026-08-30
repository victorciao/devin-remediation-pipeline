"""§9.1 red-baseline contract: four fields, per-item aggregate, stale skips, descendants."""

from __future__ import annotations

import pytest

from pipeline.config import PipelineConfig
from pipeline.red_baseline import (
    apply_red_baseline,
    classify_red_baseline,
    should_reauthor_baseline,
    validate_nested_marker_lifts,
)
from pipeline.review_loop import (
    ReviewIteration,
    ReviewLoopResult,
    apply_review_result,
    run_review_loop,
)
from pipeline.schemas import (
    NEEDS_HUMAN_REVIEW_LABEL,
    Action,
    BaselineStatus,
    Candidate,
    CandidateState,
    ExpectedFailure,
    ItemOutcome,
    PerItemOutcome,
    ReasonCode,
    RedBaselineResult,
)
from tests.factories import lane2_candidate

CLASS_NODEID = "tests/integration_tests/charts/data/api_tests.py::TestPostChartDataApi"
ITEM = f"{CLASS_NODEID}::test_chart_data_get"
SIBLING = f"{CLASS_NODEID}::test_chart_data_post"
DESCENDANT = f"{CLASS_NODEID}::test_chart_data_cache"

EXPECTED = ExpectedFailure(
    nodeid=ITEM,
    exception_type="AssertionError",
    message_pattern=r"assert 400 == 200",
    assert_location="tests/integration_tests/charts/data/api_tests.py:812",
)


def outcome(
    nodeid: str,
    result: ItemOutcome,
    *,
    exception_type: str | None = "AssertionError",
    message: str | None = "assert 400 == 200",
) -> PerItemOutcome:
    if result is not ItemOutcome.FAILED:
        exception_type = None
        message = None
    return PerItemOutcome(
        nodeid=nodeid,
        outcome=result,
        exception_type=exception_type,
        message=message,
        assert_location=EXPECTED.assert_location if result is ItemOutcome.FAILED else None,
    )


# -- single-item rows, the special case of the aggregate ----------------------------------


def test_single_item_failure_with_matching_signature_is_valid() -> None:
    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)])

    assert result.status == BaselineStatus.VALID
    assert result.representative_nodeid == ITEM


def test_reviewer_owns_lane2_baseline_classification() -> None:
    """§17 — failed / passed / skipped map to valid / `stale_skip` / `invalid_red_baseline`."""
    mapping = {
        ItemOutcome.FAILED: BaselineStatus.VALID,
        ItemOutcome.PASSED: BaselineStatus.STALE_SKIP,
        ItemOutcome.SKIPPED: BaselineStatus.INVALID_RED_BASELINE,
        ItemOutcome.ERROR: BaselineStatus.INVALID_RED_BASELINE,
    }

    observed = {
        result: classify_red_baseline(EXPECTED, [outcome(ITEM, result)]).status
        for result in mapping
    }

    assert observed == mapping


@pytest.mark.parametrize(
    ("exception_type", "message"),
    [
        ("ValueError", "assert 400 == 200"),
        ("AssertionError", "connection refused"),
    ],
)
def test_invalid_red_baseline_signature_mismatch(exception_type: str, message: str) -> None:
    """§17 — a pre-fix failure whose signature misses either field is an invalid baseline."""
    result = classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED, exception_type=exception_type, message=message)],
    )

    assert result.status == BaselineStatus.INVALID_RED_BASELINE
    assert result.representative_nodeid is None


def test_assert_location_is_optional_but_enforced_when_supplied() -> None:
    """§9.1 — `assert_location` is optional; a supplied location that moved is a mismatch."""
    moved = outcome(ITEM, ItemOutcome.FAILED).model_copy(
        update={"assert_location": "tests/integration_tests/charts/data/api_tests.py:999"}
    )

    assert classify_red_baseline(EXPECTED, [moved]).status == (BaselineStatus.INVALID_RED_BASELINE)

    without_location = ExpectedFailure(
        nodeid=ITEM,
        exception_type="AssertionError",
        message_pattern=r"assert 400 == 200",
    )
    assert classify_red_baseline(without_location, [moved]).status == BaselineStatus.VALID


def test_all_four_expected_fields_are_logged() -> None:
    """§9.1 — the four expected fields and their observed counterparts are recorded.

    The plan requires the observed counterparts to make the §11 expected-reason-match KPI
    computable, so each logged item must carry its own match verdict.
    """
    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)])

    assert result.expected_failure == EXPECTED
    logged = result.per_item_outcomes[0]
    assert logged.nodeid == EXPECTED.nodeid
    assert logged.exception_type == EXPECTED.exception_type
    assert logged.message is not None
    assert logged.assert_location == EXPECTED.assert_location
    assert logged.expected_reason_match is True


def test_signature_mismatch_is_logged_as_an_expected_reason_mismatch() -> None:
    """§11 — the expected-reason-match KPI needs the negative case recorded too."""
    result = classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED, exception_type="ValueError")],
    )

    assert result.per_item_outcomes[0].expected_reason_match is False


# -- multi-item rows ---------------------------------------------------------------------


def test_multi_item_red_baseline_classification() -> None:
    """§17 — one expected FAIL plus passes is valid; any SKIPPED invalid; all-pass stale."""
    valid = classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED), outcome(SIBLING, ItemOutcome.PASSED)],
    )
    with_skip = classify_red_baseline(
        EXPECTED,
        [
            outcome(ITEM, ItemOutcome.FAILED),
            outcome(SIBLING, ItemOutcome.PASSED),
            outcome(DESCENDANT, ItemOutcome.SKIPPED),
        ],
    )
    all_pass = classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.PASSED), outcome(SIBLING, ItemOutcome.PASSED)],
    )

    assert valid.status == BaselineStatus.VALID
    assert valid.representative_nodeid == ITEM
    assert with_skip.status == BaselineStatus.INVALID_RED_BASELINE
    assert all_pass.status == BaselineStatus.STALE_SKIP


def test_empty_outcome_vector_is_an_invalid_baseline() -> None:
    """A locator that collected nothing cannot be a valid red baseline."""
    assert classify_red_baseline(EXPECTED, []).status == BaselineStatus.INVALID_RED_BASELINE


def test_descendant_marker_excluded_from_aggregate() -> None:
    """§17 — SKIPPED items carrying their own marker are logged, not fatal."""
    result = classify_red_baseline(
        EXPECTED,
        [
            outcome(ITEM, ItemOutcome.FAILED),
            outcome(SIBLING, ItemOutcome.PASSED),
            outcome(DESCENDANT, ItemOutcome.SKIPPED),
        ],
        descendant_nodeids=[DESCENDANT],
    )

    assert result.status == BaselineStatus.VALID
    assert list(result.still_skipped_descendants) == [DESCENDANT]
    assert len(result.per_item_outcomes) == 3


def test_stale_skip_ignores_marked_descendants() -> None:
    result = classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.PASSED), outcome(DESCENDANT, ItemOutcome.SKIPPED)],
        descendant_nodeids=[DESCENDANT],
    )

    assert result.status == BaselineStatus.STALE_SKIP
    assert list(result.still_skipped_descendants) == [DESCENDANT]


# -- nested marker lifting ---------------------------------------------------------------


def test_nested_skip_requires_lifting_parent() -> None:
    """§17/§9.2 — a nested candidate whose patch lifts only its own marker is rejected."""
    candidate = lane2_candidate(nodeid=ITEM, enclosing_skip_nodeid=CLASS_NODEID)

    with pytest.raises(ValueError, match="enclosing marker"):
        validate_nested_marker_lifts(candidate, [ITEM])


def test_remaining_ancestor_marker_is_also_rejected() -> None:
    """A marker both lifted and still present is not lifted."""
    candidate = lane2_candidate(nodeid=ITEM, enclosing_skip_nodeid=CLASS_NODEID)

    with pytest.raises(ValueError, match="enclosing marker"):
        validate_nested_marker_lifts(candidate, [ITEM, CLASS_NODEID], [CLASS_NODEID])


def test_lifting_the_ancestor_allows_classification() -> None:
    candidate = lane2_candidate(nodeid=ITEM, enclosing_skip_nodeid=CLASS_NODEID)

    validate_nested_marker_lifts(candidate, [ITEM, CLASS_NODEID])

    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)])
    assert result.status == BaselineStatus.VALID


def test_unnested_candidate_needs_no_lifts() -> None:
    validate_nested_marker_lifts(lane2_candidate(nodeid=ITEM), [])


# -- applying the classification ---------------------------------------------------------


def routed(candidate: Candidate, result: RedBaselineResult) -> Candidate:
    """Route a candidate through the single routing owner, carrying the baseline result."""
    return apply_review_result(
        candidate,
        ReviewLoopResult(
            converged=False,
            iterations=1,
            state=CandidateState.DISPATCHING,
            red_result=result,
        ),
    )


def test_stale_skip_is_a_successful_reviewer_only_terminal_outcome() -> None:
    """§9 (line 492) — `stale_skip` is remediated by a reviewer-only diff, not dropped.

    `apply_red_baseline` records the facts and nothing else; `apply_review_result` is the one
    place a candidate is routed, so the terminal reviewer-only outcome is asserted there.
    """
    candidate = lane2_candidate(nodeid=ITEM, gate_passed=True, score=128.0, risk=1)
    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.PASSED)])

    applied = apply_red_baseline(candidate, result, lifted_markers=[ITEM])

    assert applied.red_baseline is not None
    assert applied.red_baseline.status is BaselineStatus.STALE_SKIP
    assert list(applied.lifted_markers) == [ITEM]
    assert applied.state is candidate.state
    assert applied.reason is candidate.reason
    assert applied.action == candidate.action

    reviewed = routed(applied, result)

    assert reviewed.action is Action.REVIEWER_ONLY_DIFF
    assert reviewed.state is CandidateState.TERMINAL
    assert reviewed.reason is ReasonCode.STALE_SKIP
    assert reviewed.auto_merge_eligible is False


def test_invalid_baseline_escalation_is_terminal_never_back_to_gated() -> None:
    """§9.1 (l.441-446) — one re-author, then `terminal`/`invalid_red_baseline`/human review.

    `gated` is a pre-dispatch state, so a backwards edge there would re-gate, re-score and re-open
    three role sessions on a candidate already known to fail, on this and every later run, while
    reporting it in the cheap pre-dispatch bucket. The baseline facts must survive the routing.
    """
    candidate = lane2_candidate(nodeid=ITEM, gate_passed=True, score=128.0, risk=1)
    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.SKIPPED)])

    applied = apply_red_baseline(candidate, result)

    assert applied.red_baseline is not None
    assert applied.red_baseline.status is BaselineStatus.INVALID_RED_BASELINE
    assert applied.state is candidate.state
    assert applied.reason is candidate.reason

    invalid = ReviewIteration(
        red_baseline=BaselineStatus.INVALID_RED_BASELINE,
        green=False,
        diff_reviewed=True,
        red_result=result,
    )
    loop = run_review_loop(PipelineConfig(iteration_cap=5), invalid, lambda _ordinal: invalid)

    assert loop.iterations == 2, "the re-author attempt is bounded at one"

    reviewed = apply_review_result(applied, loop)

    assert reviewed.state is CandidateState.TERMINAL
    assert reviewed.state.value != CandidateState.GATED.value
    assert reviewed.reason is ReasonCode.INVALID_RED_BASELINE
    assert reviewed.action is Action.HUMAN_REVIEW
    assert NEEDS_HUMAN_REVIEW_LABEL in reviewed.labels
    assert reviewed.auto_merge_eligible is False
    assert reviewed.red_baseline is not None
    assert reviewed.red_baseline.status is BaselineStatus.INVALID_RED_BASELINE
    assert reviewed.red_baseline.per_item_outcomes == result.per_item_outcomes
    assert reviewed.red_baseline.expected_failure == result.expected_failure


def test_valid_baseline_only_records_evidence() -> None:
    candidate = lane2_candidate(nodeid=ITEM, gate_passed=True, score=128.0, risk=1)
    result = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)])

    applied = apply_red_baseline(candidate, result)

    assert applied.reason is None
    assert applied.action == candidate.action
    assert applied.red_baseline is not None
    assert applied.red_baseline.status is BaselineStatus.VALID

    reviewed = routed(applied, result)

    assert reviewed.reason is None
    assert reviewed.state is CandidateState.DISPATCHING
    assert reviewed.action == candidate.action


def test_invalid_baseline_is_reauthored_exactly_once() -> None:
    """§9.1 — `invalid_red_baseline` → re-author once, then escalate."""
    invalid = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.SKIPPED)])
    valid = classify_red_baseline(EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)])

    assert should_reauthor_baseline(invalid, 1) is True
    assert should_reauthor_baseline(invalid, 2) is False
    assert should_reauthor_baseline(valid, 1) is False
