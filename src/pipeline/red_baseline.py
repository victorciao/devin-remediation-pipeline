"""Pure red-baseline and role-diff decisions for runtime orchestration."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from pipeline.schemas import (
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
    """Apply baseline classification, including reviewer-only stale skips."""
    remaining = set(remaining_markers)
    marker_list = [marker for marker in lifted_markers if marker not in remaining]
    if result.status is BaselineStatus.STALE_SKIP:
        return candidate.model_copy(
            update={
                "action": Action.REVIEWER_ONLY_DIFF,
                "state": CandidateState.TERMINAL,
                "reason": ReasonCode.STALE_SKIP,
                "auto_merge_eligible": False,
                "red_baseline": result,
                "lifted_markers": marker_list,
            }
        )
    if result.status is BaselineStatus.INVALID_RED_BASELINE:
        return candidate.model_copy(
            update={
                "state": CandidateState.GATED,
                "reason": ReasonCode.INVALID_RED_BASELINE,
                "auto_merge_eligible": False,
                "red_baseline": result,
                "lifted_markers": marker_list,
            }
        )
    return candidate.model_copy(update={"red_baseline": result, "lifted_markers": marker_list})


def should_reauthor_baseline(result: RedBaselineResult, attempt: int) -> bool:
    """Permit exactly one reviewer re-author attempt for an invalid baseline."""
    return result.status is BaselineStatus.INVALID_RED_BASELINE and attempt == 1


@dataclass(frozen=True)
class DiffInspection:
    """Classification of a role's changed hunks."""

    accepted: bool
    reason: ReasonCode | None
    changed_paths: tuple[str, ...]


_SKIP_MARKER = re.compile(
    r"pytest\.mark\.(?:skip|skipif|skipUnless|xfail)|"
    r"unittest(?:\.case)?\.(?:skip|skipIf|skipUnless)"
)
_TEST_LOGIC = re.compile(
    r"\bassert\b|pytest\.raises|unittest\.TestCase|"
    r"\b(?:setUp|tearDown)\b"
)


def _changed_hunks(diff_text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    hunks: list[tuple[str, tuple[str, ...]]] = []
    current_path: str | None = None
    current_lines: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            if current_path is not None:
                hunks.append((current_path, tuple(current_lines)))
            current_path = line.removeprefix("+++ b/")
            current_lines = []
        elif current_path is not None:
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                current_lines.append(line[1:])
    if current_path is not None:
        hunks.append((current_path, tuple(current_lines)))
    return tuple(hunks)


def classify_implementer_diff(
    diff_text: str,
    *,
    test_paths: Iterable[str] = (),
) -> DiffInspection:
    """Reject implementer hunks that alter tests, assertions, or skip markers."""
    hunks = _changed_hunks(diff_text)
    paths = tuple(path for path, _ in hunks)
    configured_test_paths = tuple(test_paths)
    for path, lines in hunks:
        test_path = (
            path.startswith("tests/")
            or "/tests/" in path
            or path.startswith("test_")
            or path.startswith("fixtures/")
            or "/fixtures/" in path
            or path.endswith("/conftest.py")
            or path == "conftest.py"
            or path in configured_test_paths
        )
        if test_path or any(
            _SKIP_MARKER.search(line) or _TEST_LOGIC.search(line) for line in lines
        ):
            return DiffInspection(False, ReasonCode.IMPLEMENTER_TEST_EDIT, paths)
    return DiffInspection(True, None, paths)


def validate_nested_marker_lifts(
    candidate: Candidate,
    lifted_markers: Iterable[str],
    remaining_markers: Iterable[str] = (),
) -> None:
    """Require every enumerated parent marker to be lifted before classification."""
    if candidate.enclosing_skip_nodeid is None:
        return
    lifted = set(lifted_markers)
    remaining = set(remaining_markers)
    parent = candidate.enclosing_skip_nodeid
    if parent not in lifted or parent in remaining:
        raise ValueError(
            "nested candidate must lift every enclosing marker before baseline classification"
        )


def inspect_reviewer_diff(
    diff_text: str,
    candidate: Candidate,
    *,
    lifted_markers: Iterable[str] = (),
    remaining_markers: Iterable[str] = (),
) -> DiffInspection:
    """Allow reviewer test changes while keeping ancestor marker lifts scratch-only."""
    validate_nested_marker_lifts(candidate, lifted_markers, remaining_markers)
    hunks = _changed_hunks(diff_text)
    paths = tuple(path for path, _ in hunks)
    own_name = (candidate.nodeid or "").rsplit("::", 1)[-1]
    parent_name = (
        candidate.enclosing_skip_nodeid.rsplit("::", 1)[-1]
        if candidate.enclosing_skip_nodeid is not None
        else None
    )
    for path, lines in hunks:
        if not (path.startswith("tests/") or "/tests/" in path or path.startswith("test_")):
            return DiffInspection(False, ReasonCode.IMPLEMENTER_TEST_EDIT, paths)
        context = "\n".join(lines)
        for line in lines:
            if _SKIP_MARKER.search(line) and own_name not in context:
                return DiffInspection(False, ReasonCode.IMPLEMENTER_TEST_EDIT, paths)
            if (
                _SKIP_MARKER.search(line)
                and parent_name is not None
                and parent_name in context
                and own_name not in context
            ):
                return DiffInspection(False, ReasonCode.IMPLEMENTER_TEST_EDIT, paths)
    return DiffInspection(True, None, paths)


__all__ = [
    "DiffInspection",
    "apply_red_baseline",
    "classify_implementer_diff",
    "classify_red_baseline",
    "inspect_reviewer_diff",
    "should_reauthor_baseline",
    "validate_nested_marker_lifts",
]
