"""Pure LANE 2 red-baseline classification used by orchestrator verification."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence

from pipeline.schemas import (
    BaselineStatus,
    Candidate,
    ExpectedFailure,
    ItemOutcome,
    PerItemOutcome,
    RedBaselineResult,
)

logger = logging.getLogger(__name__)


def _matches_expected(
    outcome: PerItemOutcome,
    expected: ExpectedFailure,
) -> bool:
    if outcome.outcome is not ItemOutcome.FAILED:
        return False
    if outcome.exception_type != expected.exception_type:
        return False
    if outcome.message is None or re.search(expected.message_pattern, outcome.message) is None:
        return False
    return expected.assert_location is None or (outcome.assert_location == expected.assert_location)


def classify_red_baseline(
    expected: ExpectedFailure,
    outcomes: Sequence[PerItemOutcome],
    *,
    descendant_nodeids: Iterable[str] = (),
) -> RedBaselineResult:
    """Classify a single- or multi-item red baseline.

    Descendants with their own unconditional marker are excluded from the
    aggregate and recorded separately. Every remaining item must be collected,
    with at least one failure matching the planner signature. An all-pass
    aggregate is the successful ``stale_skip`` path.
    """
    descendants = set(descendant_nodeids)
    logged_outcomes = [
        outcome.model_copy(update={"expected_reason_match": _matches_expected(outcome, expected)})
        for outcome in outcomes
    ]
    still_skipped = [
        outcome.nodeid
        for outcome in logged_outcomes
        if outcome.outcome is ItemOutcome.SKIPPED and outcome.nodeid in descendants
    ]
    applicable = [
        outcome
        for outcome in logged_outcomes
        if not (outcome.outcome is ItemOutcome.SKIPPED and outcome.nodeid in descendants)
    ]
    for outcome in logged_outcomes:
        logger.info(
            "red_baseline_item",
            extra={
                "nodeid": outcome.nodeid,
                "outcome": outcome.outcome.value,
                "exception_type": outcome.exception_type,
                "message": (
                    outcome.message[:160] + "…"
                    if outcome.message is not None and len(outcome.message) > 160
                    else outcome.message
                ),
            },
        )
    if not applicable or any(outcome.outcome is ItemOutcome.SKIPPED for outcome in applicable):
        status = BaselineStatus.INVALID_RED_BASELINE
    elif all(outcome.outcome is ItemOutcome.PASSED for outcome in applicable):
        status = BaselineStatus.STALE_SKIP
    elif any(_matches_expected(outcome, expected) for outcome in applicable):
        status = BaselineStatus.VALID
    else:
        status = BaselineStatus.INVALID_RED_BASELINE
    return RedBaselineResult(
        status=status,
        per_item_outcomes=logged_outcomes,
        still_skipped_descendants=still_skipped,
        representative_nodeid=next(
            (outcome.nodeid for outcome in applicable if _matches_expected(outcome, expected)),
            None,
        ),
        expected_failure=expected,
    )


def apply_red_baseline(
    candidate: Candidate,
    result: RedBaselineResult,
    *,
    lifted_markers: Iterable[str] = (),
    remaining_markers: Iterable[str] = (),
) -> Candidate:
    """Apply baseline facts without deciding candidate routing."""
    remaining = set(remaining_markers)
    marker_list = [marker for marker in lifted_markers if marker not in remaining]
    return candidate.model_copy(update={"red_baseline": result, "lifted_markers": marker_list})


def is_test_path(path: str, configured_test_paths: Iterable[str] = ()) -> bool:
    """Return whether a path belongs to test files rather than production code."""
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or path.startswith("test_")
        or path.startswith("fixtures/")
        or "/fixtures/" in path
        or path.endswith("/conftest.py")
        or path == "conftest.py"
        or path in tuple(configured_test_paths)
    )


__all__ = [
    "apply_red_baseline",
    "classify_red_baseline",
    "is_test_path",
]
