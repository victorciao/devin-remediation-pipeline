"""Focused tests for head-only LANE 2 verification."""

from __future__ import annotations

from pipeline.config import CiEvidenceMode, PipelineConfig
from pipeline.schemas import ItemOutcome, PerItemOutcome, ReasonCode
from pipeline.verify import (
    ItemRunResult,
    Observers,
    SkipMarkerObservation,
    SuiteResult,
    SymbolObservation,
    is_locally_observable,
    verify_candidate,
    verify_lane2,
    verify_lane3,
)
from tests.factories import lane2_candidate, lane3_candidate


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
        config=PipelineConfig(),
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
        config=PipelineConfig(),
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
        config=PipelineConfig(),
    )

    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert baseline is None


def test_actions_integration_nodeid_defers_without_local_run() -> None:
    candidate = lane2_candidate()
    calls: list[str] = []

    def run_item(_sha: str, nodeid: str) -> ItemRunResult:
        calls.append(nodeid)
        return ItemRunResult((), "pytest should not run")

    evidence, _ = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(run_item=run_item),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
    )

    assert calls == []
    assert evidence.satisfied is None
    assert evidence.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert "outside local_item_scope" in evidence.observations[0]


def test_actions_unit_nodeid_keeps_local_pre_pr_path() -> None:
    candidate = lane2_candidate(nodeid="tests/unit_tests/test_example.py::test_value")
    calls: list[tuple[str, str]] = []

    def run_item(sha: str, nodeid: str) -> ItemRunResult:
        calls.append((sha, nodeid))
        return ItemRunResult(
            (PerItemOutcome(nodeid=nodeid, outcome=ItemOutcome.PASSED),),
            "pytest unit",
        )

    evidence, _ = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(run_item=run_item),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
    )

    assert calls == [("head", candidate.nodeid or "")]
    assert evidence.satisfied is True


def test_actions_local_capability_failure_defers_and_preserves_attempt() -> None:
    candidate = lane2_candidate(nodeid="tests/unit_tests/test_example.py::test_value")
    evidence, _ = verify_lane2(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(
            run_item=lambda _sha, _nodeid: ItemRunResult(
                (),
                "pytest unit",
                ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
    )

    assert evidence.satisfied is None
    assert evidence.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert evidence.commands == ["pytest unit"]
    assert "test capability was unavailable at head" in evidence.observations
    assert "local run could not observe the nodeid" in evidence.observations[-1]


def test_local_mode_keeps_integration_capability_failure_terminal() -> None:
    evidence, _ = verify_lane2(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(
            run_item=lambda _sha, _nodeid: ItemRunResult(
                (),
                "pytest integration",
                ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.LOCAL),
    )

    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE


def test_is_locally_observable_uses_configured_prefixes() -> None:
    config = PipelineConfig(local_item_scope=("tests/unit_tests/",))
    assert is_locally_observable("tests/unit_tests/test_example.py::test_value", config)
    assert not is_locally_observable("tests/integration_tests/test_example.py::test_value", config)
    assert not is_locally_observable(None, config)


def test_lane2_marker_present_blocks_green_suite() -> None:
    suite_calls: list[tuple[str, str]] = []

    def read_suite(sha: str, context: str) -> SuiteResult:
        suite_calls.append((sha, context))
        return SuiteResult(True, "GET checks", conclusion="success")

    evidence, _ = verify_candidate(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(
            probe_skip_marker=lambda _candidate, _sha: SkipMarkerObservation(
                True, "ast probe", markers=("function decorator: pytest.mark.skip",)
            ),
            read_ci_suite=read_suite,
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )

    assert suite_calls == []
    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.GREEN_NOT_REACHED
    assert "pytest.mark.skip" in evidence.observations[0]


def test_lane2_marker_probe_unavailable_never_satisfies() -> None:
    evidence, _ = verify_candidate(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(
            probe_skip_marker=lambda _candidate, _sha: SkipMarkerObservation(
                False, "ast probe", available=False, detail="parse failed"
            )
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert evidence.observations == ["parse failed"]


def test_lane2_marker_seam_missing_never_satisfies() -> None:
    evidence, _ = verify_candidate(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE


def test_lane2_integration_marker_absent_uses_postgres_suite_context() -> None:
    requested: list[str] = []

    def read_suite(_sha: str, context: str) -> SuiteResult:
        requested.append(context)
        return SuiteResult(True, "GET checks", conclusion="success")

    evidence, _ = verify_candidate(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(
            probe_skip_marker=lambda _candidate, _sha: SkipMarkerObservation(False, "ast probe"),
            read_ci_suite=read_suite,
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert evidence.satisfied is True
    assert requested == ["test-postgres-required"]
    assert any("test-postgres-required check" in text for text in evidence.observations)


def test_lane2_unit_marker_absent_uses_unit_suite_context() -> None:
    requested: list[str] = []
    candidate = lane2_candidate(nodeid="tests/unit_tests/test_example.py::test_value")

    def read_suite(_sha: str, context: str) -> SuiteResult:
        requested.append(context)
        return SuiteResult(True, "GET checks", conclusion="success")

    evidence, _ = verify_candidate(
        candidate,
        base_sha="base",
        head_sha="head",
        observers=Observers(
            probe_skip_marker=lambda _candidate, _sha: SkipMarkerObservation(False, "ast probe"),
            read_ci_suite=read_suite,
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert evidence.satisfied is True
    assert requested == ["unit-tests-required"]


def test_lane2_marker_absent_failing_suite_is_suite_regressed() -> None:
    evidence, _ = verify_candidate(
        lane2_candidate(),
        base_sha="base",
        head_sha="head",
        observers=Observers(
            probe_skip_marker=lambda _candidate, _sha: SkipMarkerObservation(False, "ast probe"),
            read_ci_suite=lambda _sha, _context: SuiteResult(
                False, "GET checks", conclusion="failure"
            ),
        ),
        config=PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.SUITE_REGRESSED


def test_lane3_unavailable_symbol_observation_fails_closed() -> None:
    evidence = verify_lane3(
        lane3_candidate(),
        head_sha="head",
        observers=Observers(
            probe_symbol=lambda _candidate, _sha: SymbolObservation(
                resolves=False,
                caller_count=0,
                override_count=0,
                command="ast probe",
                available=False,
                detail="module source unavailable",
            )
        ),
        config=PipelineConfig(),
    )

    assert evidence.satisfied is False
    assert evidence.reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert evidence.observations == ["module source unavailable"]
