"""Focused tests for terminal session evidence preservation."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from pipeline.__main__ import CandidateRunner, LiveTarget
from pipeline.config import Mode, PipelineConfig
from pipeline.github_client import GitHubClient
from pipeline.schemas import CandidateState, ReasonCode
from pipeline.session_client import (
    FixOutput,
    SessionBlockedError,
    SessionDedupeError,
    SessionInfeasibleError,
)
from pipeline.state import CandidateStateStore
from pipeline.verify import Observers
from tests.factories import codeql_candidate
from tests.fakes import BASE_SHA, HEAD_SHA, FakeGitHubTransport


def _runner(
    tmp_path: Path,
    transport: FakeGitHubTransport,
    orchestrator: object,
) -> tuple[CandidateRunner, CandidateStateStore]:
    config = PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-token"),
        devin_api_key=SecretStr("placeholder-key"),
    )
    store = CandidateStateStore(tmp_path / "candidates.jsonl")
    runner = CandidateRunner(
        config=config,
        run_id="run",
        state_store=store,
        orchestrator=orchestrator,  # type: ignore[arg-type]
        observers=Observers(),
        output_dir=tmp_path,
        base_sha=BASE_SHA,
        live=LiveTarget(
            client=GitHubClient(config, transport=transport),
            transport=transport,  # type: ignore[arg-type]
            base_branch="master",
            branch_prefix="devin/remediation",
        ),
    )
    return runner, store


def _output(*, feasible: bool, reason: str | None = None) -> FixOutput:
    return FixOutput(
        files_changed=(),
        test_nodeid=None,
        test_paths=(),
        verify_command="pytest",
        head_sha=BASE_SHA,
        suite_scope=(),
        fix_summary="",
        testing_notes="",
        criterion_notes="",
        feasible=feasible,
        infeasible_reason=reason,
    )


class InfeasibleOrchestrator:
    def run_candidate(self, *_args: object, **_kwargs: object) -> object:
        raise SessionInfeasibleError(
            "session claim",
            output=_output(feasible=False, reason="not feasible"),
        )


class DedupeOrchestrator:
    def run_candidate(self, *_args: object, **_kwargs: object) -> object:
        raise SessionDedupeError("existing session")


class BlockedOrchestrator:
    def run_candidate(self, *_args: object, **_kwargs: object) -> object:
        raise SessionBlockedError("session blocked")


def test_infeasible_terminal_persists_claim_and_observed_head(tmp_path: Path) -> None:
    transport = FakeGitHubTransport(branch_shas={"candidate": HEAD_SHA})
    runner, store = _runner(tmp_path, transport, InfeasibleOrchestrator())
    candidate = codeql_candidate(
        head_branch="candidate",
        base_sha=BASE_SHA,
    )

    result = runner._run_session(candidate)  # noqa: SLF001 - focused lifecycle probe

    assert result.state is CandidateState.TERMINAL
    assert result.reason is ReasonCode.SESSION_FAILED
    assert result.reason_detail == "session claim: not feasible"
    assert result.head_sha == HEAD_SHA
    assert store.latest()[candidate.candidate_id].head_sha == HEAD_SHA


def test_dedupe_terminal_persists_observed_head(tmp_path: Path) -> None:
    transport = FakeGitHubTransport(branch_shas={"candidate": HEAD_SHA})
    runner, _store = _runner(tmp_path, transport, DedupeOrchestrator())
    candidate = codeql_candidate(head_branch="candidate", base_sha=BASE_SHA)

    result = runner._run_session(candidate)  # noqa: SLF001 - focused lifecycle probe

    assert result.state is CandidateState.TERMINAL
    assert result.head_sha == HEAD_SHA


def test_blocked_terminal_persists_observed_head(tmp_path: Path) -> None:
    transport = FakeGitHubTransport(branch_shas={"candidate": HEAD_SHA})
    runner, _store = _runner(tmp_path, transport, BlockedOrchestrator())
    candidate = codeql_candidate(head_branch="candidate", base_sha=BASE_SHA)

    result = runner._run_session(candidate)  # noqa: SLF001 - focused lifecycle probe

    assert result.state is CandidateState.TERMINAL
    assert result.reason is ReasonCode.SESSION_BLOCKED
    assert result.head_sha == HEAD_SHA


def test_terminal_head_read_failure_is_swallowed(tmp_path: Path) -> None:
    transport = FakeGitHubTransport()
    runner, store = _runner(tmp_path, transport, InfeasibleOrchestrator())
    candidate = codeql_candidate(head_branch="missing", base_sha=BASE_SHA)

    result = runner._run_session(candidate)  # noqa: SLF001 - focused lifecycle probe

    assert result.state is CandidateState.TERMINAL
    assert result.reason is ReasonCode.SESSION_FAILED
    assert result.head_sha is None
    assert store.latest()[candidate.candidate_id].state is CandidateState.TERMINAL
