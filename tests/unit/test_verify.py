"""Focused tests for head-only LANE 2 verification."""

from __future__ import annotations

from pipeline.schemas import ItemOutcome, PerItemOutcome, ReasonCode
from pipeline.verify import ItemRunResult, Observers, verify_lane2
from tests.factories import lane2_candidate


def test_lane2_green_head_satisfies_without_running_base() -> None:
    calls: list[tuple[str, str]] = []

    def run_item(sha: str, nodeid: str) -> ItemRunResult:
        calls.append((sha, nodeid))
        return ItemRunResult(
            outcomes=(PerItemOutcome(nodeid=nodeid, outcome=ItemOutcome.PASSED),),
            command=f"pytest {sha} {nodeid}",
        )

    candidate = lane2_candidate()
    evidence, baseline = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(run_item=run_item),
    )

    assert evidence.satisfied is True
    assert baseline is None
    assert calls == [("head", candidate.nodeid or "")]


def test_lane2_failing_head_is_green_not_reached() -> None:
    candidate = lane2_candidate()
    evidence, baseline = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(
            run_item=lambda _sha, nodeid: ItemRunResult(
                outcomes=(PerItemOutcome(nodeid=nodeid, outcome=ItemOutcome.FAILED),),
                command="pytest head",
            )
        ),
    )

    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.GREEN_NOT_REACHED
    assert baseline is None


def test_lane2_unavailable_head_capability_reason_is_propagated() -> None:
    candidate = lane2_candidate()
    evidence, baseline = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(
            run_item=lambda _sha, _nodeid: ItemRunResult(
                outcomes=(),
                command="pytest head",
                reason=ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        ),
    )

    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert baseline is None
