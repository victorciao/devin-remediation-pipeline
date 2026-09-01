"""Focused tests for terminal session evidence preservation."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from pipeline.__main__ import CandidateRunner, LiveTarget
from pipeline.config import Mode, PipelineConfig
from pipeline.github_client import GitHubClient
from pipeline.schemas import Action, Candidate, CandidateState, ReasonCode, RetryDecision
from pipeline.session_client import (
    FixOutput,
    SessionAttempt,
    SessionBlockedError,
    SessionCeilingError,
    SessionDedupeError,
    SessionInfeasibleError,
    SessionOutputError,
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


class CallbackFailureOrchestrator:
    """Create a durable session identity, then fail in the requested lifecycle path."""

    def __init__(self, failure: BaseException) -> None:
        self.failure = failure

    def run_candidate(self, _candidate: object, _prompt: str, **kwargs: object) -> object:
        callback = kwargs["session_created"]
        assert callable(callback)
        callback(
            SessionAttempt(
                "codeql-0",
                1,
                "created-session",
                True,
                RetryDecision.PROCEED,
            )
        )
        raise self.failure


def test_session_failures_preserve_the_created_session_id(tmp_path: Path) -> None:
    """Every contained failure row retains the session created before it failed."""
    failures = (
        (SessionCeilingError("ceiling"), ReasonCode.SESSION_CEILING),
        (
            SessionInfeasibleError(
                "infeasible",
                output=_output(feasible=False, reason="not feasible"),
            ),
            ReasonCode.SESSION_FAILED,
        ),
        (SessionDedupeError("dedupe"), ReasonCode.SESSION_FAILED),
        (SessionBlockedError("blocked"), ReasonCode.SESSION_BLOCKED),
        (SessionOutputError("output"), ReasonCode.SESSION_BLOCKED),
    )
    for index, (failure, reason) in enumerate(failures):
        runner, store = _runner(
            tmp_path / str(index),
            FakeGitHubTransport(branch_shas={"candidate": HEAD_SHA}),
            CallbackFailureOrchestrator(failure),
        )
        candidate = codeql_candidate(
            candidate_id="codeql-0",
            head_branch="candidate",
            base_sha=BASE_SHA,
        )

        result = runner._run_session(candidate)  # noqa: SLF001 - focused lifecycle probe

        assert result.reason is reason
        assert result.session_id == "created-session"
        assert store.latest()[candidate.candidate_id].session_id == "created-session"


class NoSessionOrchestrator:
    """Fail loudly if a previously settled candidate reaches dispatch."""

    def run_candidate(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("settled candidate was redispatched")


def _persisted_awaiting(
    runner: CandidateRunner,
    candidate: Candidate,
) -> None:
    """Persist one PR awaiting a human disposition."""
    runner.state_store.append(
        candidate.model_copy(
            update={
                "state": CandidateState.AWAITING_HUMAN_MERGE,
                "issue_number": 1,
                "issue_url": "https://github.test/victorciao/superset/issues/1",
                "pr_number": 2,
                "pr_url": "https://github.test/victorciao/superset/pull/2",
                "reason": ReasonCode.MANUAL_MERGE_REQUIRED,
                "action": Action.OPEN_PR,
            }
        )
    )


def test_persisted_awaiting_human_merge_is_not_redispatched(tmp_path: Path) -> None:
    """A persisted pending PR skips issue, branch, session and PR writes."""
    transport = FakeGitHubTransport()
    runner, _store = _runner(tmp_path, transport, NoSessionOrchestrator())
    candidate = codeql_candidate(
        action=Action.OPEN_PR,
        state=CandidateState.SCORED,
    )
    _persisted_awaiting(runner, candidate)

    result = runner.process(candidate)

    assert result.state is CandidateState.AWAITING_HUMAN_MERGE
    assert transport.writes == []


def test_reconcile_observes_human_merged_pr(tmp_path: Path) -> None:
    """A later run records an externally observed human merge."""
    transport = FakeGitHubTransport(pr_merged_at="2026-09-01T00:00:00Z")
    runner, _store = _runner(tmp_path, transport, NoSessionOrchestrator())
    candidate = codeql_candidate(action=Action.OPEN_PR)
    _persisted_awaiting(runner, candidate)

    result = runner.process(candidate)

    assert result.state is CandidateState.MERGED
    assert result.merged_at == "2026-09-01T00:00:00Z"
    assert result.merge_verified is True


def test_reconcile_observes_human_closed_pr(tmp_path: Path) -> None:
    """A later run records a human-closed PR as terminal."""
    transport = FakeGitHubTransport(pr_state="closed")
    runner, _store = _runner(tmp_path, transport, NoSessionOrchestrator())
    candidate = codeql_candidate(action=Action.OPEN_PR)
    _persisted_awaiting(runner, candidate)

    result = runner.process(candidate)

    assert result.state is CandidateState.TERMINAL
    assert result.reason is ReasonCode.CLOSED_PULL_REQUEST


def test_reconcile_leaves_open_pr_awaiting_human_merge(tmp_path: Path) -> None:
    """An open PR remains pending and is not sent through the lifecycle again."""
    transport = FakeGitHubTransport(pr_state="open")
    runner, _store = _runner(tmp_path, transport, NoSessionOrchestrator())
    candidate = codeql_candidate(action=Action.OPEN_PR)
    _persisted_awaiting(runner, candidate)

    result = runner.process(candidate)

    assert result.state is CandidateState.AWAITING_HUMAN_MERGE
