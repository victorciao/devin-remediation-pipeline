"""Reviewer-authored §10 criterion tests: no criterion passes without an observation.

These pin the plan's two load-bearing verification properties: a criterion the orchestrator
has not observed is never `satisfied`, and nothing a session reports about its own results
becomes evidence (§4, §10).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from pipeline.config import CiEvidenceMode, Lane1AlertCheck, Mode, PipelineConfig
from pipeline.observers import LocalCheckout
from pipeline.schemas import ExpectedFailure, Lane, RetryDecision
from pipeline.session_client import FixOutput, SessionAttempt, SessionRun, SessionSnapshot
from pipeline.state import CandidateStateStore
from pipeline.verify import (
    LANE1_CRITERION,
    LANE2_CRITERION,
    LANE3_CRITERION,
    Observers,
    SuiteResult,
    SymbolObservation,
    verify_candidate,
    verify_lane2,
)
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate


def config(**overrides: Any) -> PipelineConfig:  # noqa: ANN401
    """A configuration pointed at the shipped rubrics and templates."""
    return PipelineConfig(rubrics_path=RUBRICS_PATH, templates_dir=TEMPLATES_DIR, **overrides)


def observed(evidence: object) -> bool:
    """Whether this evidence names at least one command the orchestrator ran."""
    commands = getattr(evidence, "commands", [])
    return bool(commands)


# -- §10: a post-PR criterion is observed on the PR head, never asserted -----------------


def test_lane2_post_pr_criterion_is_not_satisfied_without_an_observation() -> None:
    """§10: suite-green under `ci_evidence_mode = actions` is observed post-PR and gates merge."""
    candidate = lane2_candidate(success_criterion=LANE2_CRITERION)
    evidence, _ = verify_candidate(
        candidate,
        base_sha="a" * 40,
        head_sha="b" * 40,
        observers=Observers(),
        config=config(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert not (evidence.satisfied is True and not observed(evidence)), (
        "post-PR LANE 2 evidence was recorded as satisfied with no command observed"
    )


def test_lane3_post_pr_criterion_is_not_satisfied_without_an_observation() -> None:
    """§10: LANE 3 suite-green under `actions` is completed on the PR head, not asserted."""
    candidate = lane3_candidate(success_criterion=LANE3_CRITERION)
    evidence, _ = verify_candidate(
        candidate,
        base_sha="a" * 40,
        head_sha="b" * 40,
        observers=Observers(),
        config=config(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="post_pr",
    )
    assert not (evidence.satisfied is True and not observed(evidence)), (
        "post-PR LANE 3 evidence was recorded as satisfied with no command observed"
    )


def test_lane1_suite_green_under_actions_mode_is_observed_before_it_is_satisfied() -> None:
    """§10/§12.5: under `actions` the suite is read from the fork's `Python-Unit` context."""
    candidate = codeql_candidate(success_criterion=LANE1_CRITERION)
    ran: list[str] = []

    def probe_alerts(_candidate: object, _sha: str) -> Any:  # noqa: ANN401
        from pipeline.verify import AlertObservation

        return AlertObservation(locators=(), command="GET /code-scanning/alerts?ref=refs/pull/1")

    def run_suite(scope: object, sha: str) -> SuiteResult:
        ran.append(str(sha))
        return SuiteResult(passed=True, command="pytest scope")

    evidence, _ = verify_candidate(
        candidate,
        base_sha="a" * 40,
        head_sha="b" * 40,
        observers=Observers(probe_alerts=probe_alerts, run_suite=run_suite),
        config=config(
            ci_evidence_mode=CiEvidenceMode.ACTIONS,
            lane1_alert_check=Lane1AlertCheck.PR_REF_ALERTS,
        ),
        stage="post_pr",
    )
    if evidence.satisfied is True:
        suite_evidence = [text for text in evidence.observations if "suite" in text.lower()]
        assert ran or any("passed" in text or "success" in text for text in suite_evidence), (
            "LANE 1 was satisfied post-PR with suite-green neither run nor read from the fork"
        )


def test_lane3_satisfied_evidence_under_actions_mode_names_the_suite_it_observed() -> None:
    """§10: suite-green is evidence; a satisfied criterion must name how it was obtained."""
    candidate = lane3_candidate(success_criterion=LANE3_CRITERION)

    def probe_symbol(_candidate: object, sha: str) -> SymbolObservation:
        return SymbolObservation(
            resolves=False,
            caller_count=0,
            override_count=0,
            command=f"ast re-check at {sha}",
        )

    evidence, _ = verify_candidate(
        candidate,
        base_sha="a" * 40,
        head_sha="b" * 40,
        observers=Observers(probe_symbol=probe_symbol),
        config=config(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        stage="pre_pr",
    )
    assert evidence.satisfied is not True, (
        "LANE 3 was satisfied pre-PR while its suite-green evidence was deferred to CI "
        "that is never read"
    )


# -- §10 LANE 2: the red baseline is measured with the test-path diff applied ------------


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed argument vectors, no shell
        ("git", "-C", str(repo), *args),
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _fixture_repo(root: Path) -> tuple[Path, str, str]:
    """A repo whose head both fixes a bug and re-enables the test that proves it."""
    repo = root / "target"
    (repo / "tests").mkdir(parents=True)
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "config", "user.email", "reviewer@example.invalid")
    _git(repo, "config", "user.name", "reviewer")
    (repo / "mod.py").write_text("def value() -> int:\n    return 1\n", encoding="utf-8")
    (repo / "tests" / "test_mod.py").write_text(
        "import pytest\n\nimport mod\n\n\n"
        '@pytest.mark.skip(reason="broken")\n'
        "def test_value() -> None:\n    assert mod.value() == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "mod.py").write_text("def value() -> int:\n    return 2\n", encoding="utf-8")
    (repo / "tests" / "test_mod.py").write_text(
        "import mod\n\n\ndef test_value() -> None:\n    assert mod.value() == 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    head_sha = _git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def test_lane2_red_baseline_applies_the_session_test_path_diff_at_base(tmp_path: Path) -> None:
    """§10 LANE 2.1: base runs the nodeid with only the session's test-path diff applied."""
    repo, base_sha, head_sha = _fixture_repo(tmp_path)
    checkout = LocalCheckout(
        repo_path=repo,
        worktree_root=tmp_path / "worktrees",
        pytest_command=(sys.executable, "-m", "pytest", "-q", "--tb=no", "-rA"),
    )
    candidate = lane2_candidate(
        nodeid="tests/test_mod.py::test_value",
        stable_locator="tests/test_mod.py::test_value",
        success_criterion=LANE2_CRITERION,
        test_paths=["tests/test_mod.py"],
        expected_failure=ExpectedFailure(
            nodeid="tests/test_mod.py::test_value",
            exception_type="AssertionError",
            message_pattern=".",
        ),
    )
    evidence, baseline = verify_lane2(
        candidate,
        base_sha=base_sha,
        head_sha=head_sha,
        observers=Observers(
            run_item=checkout.run_item,
            run_item_with_test_diff=checkout.run_item_with_test_diff,
            run_suite=checkout.run_suite,
        ),
        config=config(ci_evidence_mode=CiEvidenceMode.LOCAL),
    )
    assert evidence.satisfied is True, (
        f"red-at-base was measured without the test-path diff: {baseline} {evidence.observations}"
    )


# -- §4/§10: a session's own account of its results is not evidence ---------------------


def test_session_reported_test_is_not_recorded_as_an_orchestrator_observed_test(
    tmp_path: Path,
) -> None:
    """§4: `test_added` gates the §12 merge, so it must not come from the session's output."""
    from pipeline.__main__ import CandidateRunner
    from pipeline.session_client import RuntimeOrchestrator, SessionClient

    candidate = lane2_candidate(success_criterion=LANE2_CRITERION, head_branch="devin/x")
    runner = CandidateRunner(
        config=config(mode=Mode.SIMULATE),
        run_id="run",
        state_store=CandidateStateStore(tmp_path / "candidates.jsonl"),
        orchestrator=RuntimeOrchestrator(SessionClient(config(mode=Mode.SIMULATE))),
        observers=Observers(),
        output_dir=tmp_path,
        base_sha="a" * 40,
    )
    output = FixOutput(
        files_changed=("superset/x.py",),
        test_nodeid="tests/test_invented.py::test_never_written",
        test_paths=("tests/test_invented.py",),
        verify_command="pytest tests/test_invented.py",
        head_sha="b" * 40,
        suite_scope=("tests/test_invented.py",),
        fix_summary="fixed",
        testing_notes="I added a test",
        criterion_notes="I met the criterion",
        feasible=True,
        infeasible_reason=None,
    )
    run = SessionRun(
        attempt=SessionAttempt(candidate.candidate_id, 1, "session-1", True, RetryDecision.PROCEED),
        snapshot=SessionSnapshot("session-1", "finished", {}),
        output=output,
    )
    persisted = runner._apply_fix_output(candidate, run)  # noqa: SLF001 - reviewer-owned probe
    assert persisted.test_added is not True, (
        "the session's own `test_nodeid` was recorded as an added test with no orchestrator run"
    )


def test_session_reported_suite_scope_does_not_replace_the_lane_scope(tmp_path: Path) -> None:
    """§4/§10: the suite the orchestrator runs is the lane's scope, not the session's."""
    from pipeline.__main__ import CandidateRunner
    from pipeline.session_client import RuntimeOrchestrator, SessionClient

    candidate = lane3_candidate(
        success_criterion=LANE3_CRITERION,
        suite_scope=["superset/db_engine_specs/base.py"],
        head_branch="devin/x",
    )
    runner = CandidateRunner(
        config=config(mode=Mode.SIMULATE),
        run_id="run",
        state_store=CandidateStateStore(tmp_path / "candidates.jsonl"),
        orchestrator=RuntimeOrchestrator(SessionClient(config(mode=Mode.SIMULATE))),
        observers=Observers(),
        output_dir=tmp_path,
        base_sha="a" * 40,
    )
    run = SessionRun(
        attempt=SessionAttempt(candidate.candidate_id, 1, "session-1", True, RetryDecision.PROCEED),
        snapshot=SessionSnapshot("session-1", "finished", {}),
        output=FixOutput(
            files_changed=("superset/db_engine_specs/base.py",),
            test_nodeid=None,
            test_paths=(),
            verify_command="pytest tests/trivially_green.py",
            head_sha="b" * 40,
            suite_scope=("tests/trivially_green.py",),
            fix_summary="removed the symbol",
            testing_notes="",
            criterion_notes="no test required",
            feasible=True,
            infeasible_reason=None,
        ),
    )
    persisted = runner._apply_fix_output(candidate, run)  # noqa: SLF001 - reviewer-owned probe
    assert persisted.suite_scope == ["superset/db_engine_specs/base.py"], (
        "the session narrowed the suite scope the orchestrator verifies against"
    )


def test_lane_criteria_are_declared_for_every_lane() -> None:
    """§10: every candidate carries the criterion its lane declares at enumeration."""
    from pipeline.verify import declare_success_criterion

    assert {declare_success_criterion(lane) for lane in Lane} == {
        LANE1_CRITERION,
        LANE2_CRITERION,
        LANE3_CRITERION,
    }
