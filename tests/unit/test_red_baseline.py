"""§9.1 red-baseline contract: four fields, per-item aggregate, stale skips, descendants."""

from __future__ import annotations

import pytest

from pipeline.schemas import (
    BaselineStatus,
    ExpectedFailure,
    ItemOutcome,
    PerItemOutcome,
    ReasonCode,
)
from tests import _api

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
    result = _api.session_client().classify_red_baseline(
        EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)]
    )

    assert result.status == BaselineStatus.VALID
    assert result.representative_nodeid == ITEM
    assert result.per_item_outcomes[0].expected_reason_match is True


def test_reviewer_owns_lane2_baseline_classification() -> None:
    """§17 — failed / passed / skipped map to valid / `stale_skip` / `invalid_red_baseline`."""
    classify = _api.session_client().classify_red_baseline
    mapping = {
        ItemOutcome.FAILED: BaselineStatus.VALID,
        ItemOutcome.PASSED: BaselineStatus.STALE_SKIP,
        ItemOutcome.SKIPPED: BaselineStatus.INVALID_RED_BASELINE,
        ItemOutcome.ERROR: BaselineStatus.INVALID_RED_BASELINE,
    }

    observed = {result: classify(EXPECTED, [outcome(ITEM, result)]).status for result in mapping}

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
    result = _api.session_client().classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED, exception_type=exception_type, message=message)],
    )

    assert result.status == BaselineStatus.INVALID_RED_BASELINE
    assert result.per_item_outcomes[0].expected_reason_match is False


def test_all_four_expected_fields_are_logged() -> None:
    """§9.1 — the four expected fields and their observed counterparts are recorded."""
    result = _api.session_client().classify_red_baseline(
        EXPECTED, [outcome(ITEM, ItemOutcome.FAILED)]
    )

    assert result.expected_failure == EXPECTED
    logged = result.per_item_outcomes[0]
    assert logged.nodeid == EXPECTED.nodeid
    assert logged.exception_type == EXPECTED.exception_type
    assert logged.message is not None
    assert logged.assert_location == EXPECTED.assert_location


# -- multi-item rows ---------------------------------------------------------------------


def test_multi_item_red_baseline_classification() -> None:
    """§17 — one expected FAIL plus passes is valid; any SKIPPED invalid; all-pass stale."""
    classify = _api.session_client().classify_red_baseline

    valid = classify(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED), outcome(SIBLING, ItemOutcome.PASSED)],
    )
    with_skip = classify(
        EXPECTED,
        [
            outcome(ITEM, ItemOutcome.FAILED),
            outcome(SIBLING, ItemOutcome.PASSED),
            outcome(DESCENDANT, ItemOutcome.SKIPPED),
        ],
    )
    all_pass = classify(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.PASSED), outcome(SIBLING, ItemOutcome.PASSED)],
    )

    assert valid.status == BaselineStatus.VALID
    assert valid.representative_nodeid == ITEM
    assert with_skip.status == BaselineStatus.INVALID_RED_BASELINE
    assert all_pass.status == BaselineStatus.STALE_SKIP


def test_descendant_marker_excluded_from_aggregate() -> None:
    """§17 — SKIPPED items carrying their own marker are logged, not fatal."""
    result = _api.session_client().classify_red_baseline(
        EXPECTED,
        [
            outcome(ITEM, ItemOutcome.FAILED),
            outcome(SIBLING, ItemOutcome.PASSED),
            outcome(DESCENDANT, ItemOutcome.SKIPPED),
        ],
        descendant_marker_nodeids=[DESCENDANT],
    )

    assert result.status == BaselineStatus.VALID
    assert list(result.still_skipped_descendants) == [DESCENDANT]


def test_stale_skip_ignores_marked_descendants() -> None:
    result = _api.session_client().classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.PASSED), outcome(DESCENDANT, ItemOutcome.SKIPPED)],
        descendant_marker_nodeids=[DESCENDANT],
    )

    assert result.status == BaselineStatus.STALE_SKIP
    assert list(result.still_skipped_descendants) == [DESCENDANT]


def test_nested_skip_requires_lifting_parent() -> None:
    """§17 — a nested candidate whose patch lifts only its own marker is rejected first."""
    with pytest.raises(ValueError) as excinfo:
        _api.session_client().classify_red_baseline(
            EXPECTED,
            [outcome(ITEM, ItemOutcome.SKIPPED)],
            lifted_markers=[ITEM],
            enclosing_skip_nodeid=CLASS_NODEID,
        )

    assert ReasonCode.BLOCKED_BY_ENCLOSING_SKIP.value in str(excinfo.value)


def test_lifting_the_ancestor_allows_classification() -> None:
    result = _api.session_client().classify_red_baseline(
        EXPECTED,
        [outcome(ITEM, ItemOutcome.FAILED)],
        lifted_markers=[ITEM, CLASS_NODEID],
        enclosing_skip_nodeid=CLASS_NODEID,
    )

    assert result.status == BaselineStatus.VALID
