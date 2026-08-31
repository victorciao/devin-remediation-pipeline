"""§10 CI evidence resolution, §10.1's one-way upgrade, and §14.1's artifact ordering.

The auto-merge conjunction is the most safety-relevant contract in the pipeline, so every
non-`success` context state is asserted to fail closed *and* to leave `enable_auto_merge`
uncalled — the absence of that write, not just a false flag, is the invariant.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline.config import (
    DEFAULT_REQUIRED_CONTEXTS_MIN,
    CiEvidenceMode,
    Mode,
    PipelineConfig,
)
from pipeline.github_client import (
    ArtifactUnavailableError,
    CiModeTransition,
    CiWaitResult,
    ClosedPullRequestError,
    GitHubClient,
    GitHubResponseError,
    LabelCapabilityError,
    MergedPullRequestError,
    PreflightError,
    SimulationWriteError,
    publish_artifacts,
    read_check_runs,
    run_live_preflight,
    wait_for_check_runs,
)
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import CheckRunConclusion, MergeMode, ReasonCode, Tier
from tests.factories import codeql_candidate
from tests.fakes import (
    BASE_SHA,
    HEAD_SHA,
    FakeGitHubTransport,
    all_contexts,
)

HELD_STATES = ("action_required", "awaiting_approval", "waiting")
IN_FLIGHT_STATES = ("queued", "in_progress", "pending")
FAILED_STATES = ("failure", "cancelled", "timed_out", "error")
NON_SUCCESS_STATES = (*HELD_STATES, *IN_FLIGHT_STATES, *FAILED_STATES, "skipped", "neutral")
AUTO_MERGE_PATH = "/repos/victorciao/superset/pulls/2/auto-merge"
ISSUE_PATH = "/repos/victorciao/superset/issues"
PULLS_PATH = "/repos/victorciao/superset/pulls"


def live_config(**fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A LIVE-mode config; SIMULATE forbids every write path under test here."""
    return PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-token"),
        devin_api_key=SecretStr("placeholder-key"),
        **fields,
    )


def github_evidence_config(**fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A LIVE config where auto-merge is *permitted*, so only the gates can refuse it."""
    return live_config(
        ci_evidence_mode=CiEvidenceMode.ACTIONS,
        auto_merge_enabled=True,
        **fields,
    )


def wait_config(**fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A config whose CI wait budget is short enough to assert the deadline path."""
    return PipelineConfig(ci_wait_timeout_s=fields.pop("ci_wait_timeout_s", 60), **fields)


class Clock:
    """A monotonic clock that only advances when the code under test sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def wait(
    config: PipelineConfig,
    *,
    statuses: Mapping[str, str],
    clock: Clock | None = None,
    elapsed_s: float = 0.0,
    poll: bool = True,
    is_fork: bool = False,
    ci_mode: CiEvidenceMode | None = None,
    on_mode_transition: Callable[[CiModeTransition], None] | None = None,
    client: FakeGitHubTransport | None = None,
) -> CiWaitResult:
    """Wait for required contexts against an in-memory status mapping."""
    timer = clock or Clock()
    return wait_for_check_runs(
        config,
        client=client or FakeGitHubTransport(),
        elapsed_s=elapsed_s,
        conclusions=[
            CheckRunConclusion(
                name=name,
                conclusion=None if state == "pending" else state,
                status="pending" if state == "pending" else "completed",
            )
            for name, state in statuses.items()
        ],
        poll=poll,
        sleep=timer.sleep,
        clock=timer,
        poll_interval_s=15.0,
        is_fork=is_fork,
        ci_mode=ci_mode,
        on_mode_transition=on_mode_transition,
    )


# -- §10 the required context set --------------------------------------------------------


# -- §10.1 the one-way local -> github upgrade -------------------------------------------


def test_ci_wait_keeps_the_configured_local_evidence_mode() -> None:
    """§10.1 — waiting for checks does not relabel local execution as Actions evidence."""
    transitions: list[CiModeTransition] = []

    result = wait(
        wait_config(ci_evidence_mode=CiEvidenceMode.LOCAL),
        statuses=all_contexts(),
        ci_mode=CiEvidenceMode.LOCAL,
        on_mode_transition=transitions.append,
    )

    assert transitions == []
    assert result.mode is CiEvidenceMode.LOCAL
    assert result.reason is None


def test_ci_wait_does_not_re_emit_a_transition_for_an_upgraded_run() -> None:
    """§10.1 — the mode is run-scoped, so a second candidate emits no second transition."""
    transitions: list[CiModeTransition] = []

    result = wait(
        wait_config(),
        statuses=all_contexts(),
        ci_mode=CiEvidenceMode.ACTIONS,
        on_mode_transition=transitions.append,
    )

    assert transitions == []
    assert result.mode is CiEvidenceMode.ACTIONS


def test_full_context_set_within_the_timeout_keeps_github_evidence() -> None:
    """§10.1 — every required context reporting `success` in the window is auto-merge eligible."""
    result = wait(
        wait_config(ci_evidence_mode=CiEvidenceMode.ACTIONS, auto_merge_enabled=True),
        statuses=all_contexts(),
    )

    assert result.mode is CiEvidenceMode.ACTIONS
    assert result.reason is None
    assert result.auto_merge_eligible is True


# -- §10.1 held vs in-flight CI ----------------------------------------------------------


@pytest.mark.parametrize("state", HELD_STATES)
def test_held_workflow_is_awaiting_approval_and_does_not_poll(state: str) -> None:
    """§10.1 (line 601) — a held workflow is `ci_evidence_unavailable`/`awaiting_approval`."""
    clock = Clock()

    result = wait(
        wait_config(),
        statuses={**all_contexts(), "unit-tests-required": state},
        clock=clock,
    )

    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.detail == "awaiting_workflow_approval"
    assert result.auto_merge_eligible is False
    assert clock.slept == []


@pytest.mark.parametrize("state", IN_FLIGHT_STATES)
def test_in_flight_ci_polls_until_the_deadline(state: str) -> None:
    """§10.1 — `queued`/`in_progress`/`pending` are in-flight, never awaiting approval.

    A previous implementation read them as the approval gate and made the poll loop dead code.
    """
    clock = Clock()

    result = wait(
        wait_config(ci_wait_timeout_s=60),
        statuses={**all_contexts(), "unit-tests-required": state},
        clock=clock,
    )

    assert clock.slept == [15.0, 15.0, 15.0, 15.0]
    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.detail is None
    assert result.auto_merge_eligible is False


@pytest.mark.parametrize("state", IN_FLIGHT_STATES)
def test_in_flight_ci_that_turns_green_is_eligible(state: str) -> None:
    """§10.1 — the poll loop is live: a context that reports late still resolves green."""
    clock = Clock()
    statuses = {**all_contexts(), "unit-tests-required": state}
    calls = {"count": 0}

    class Flipping(FakeGitHubTransport):
        def get(self, path: str) -> object:
            if "/check-runs" in path:
                calls["count"] += 1
                if calls["count"] > 2:
                    return {
                        "check_runs": [
                            {"name": name, "conclusion": conclusion}
                            for name, conclusion in all_contexts().items()
                        ]
                    }
                return {
                    "check_runs": [
                        {"name": name, "conclusion": conclusion}
                        for name, conclusion in statuses.items()
                    ]
                }
            return super().get(path)

    result = wait_for_check_runs(
        wait_config(ci_evidence_mode=CiEvidenceMode.ACTIONS, auto_merge_enabled=True),
        client=Flipping(),
        elapsed_s=0,
        sha=HEAD_SHA,
        sleep=clock.sleep,
        clock=clock,
    )

    assert clock.slept == [15.0, 15.0]
    assert result.reason is None
    assert result.auto_merge_eligible is True


def test_check_run_status_is_used_when_no_conclusion_exists() -> None:
    """§10 — a check run with no conclusion yet reports its `status`, not nothing."""
    transport = FakeGitHubTransport(
        context_states={},
        check_run_statuses={**all_contexts("queued"), "unit-tests-required": "action_required"},
    )

    result = wait_for_check_runs(
        wait_config(),
        client=transport,
        elapsed_s=0,
        sha=HEAD_SHA,
        poll=False,
    )

    assert result.detail == "awaiting_workflow_approval"


def test_check_and_status_reads_paginate_and_dedupe_first_seen_context() -> None:
    class Paginated(FakeGitHubTransport):
        def get(self, path: str) -> object:
            self.reads.append(path)
            if path.endswith("/check-runs?per_page=100&page=1"):
                return {
                    "check_runs": [
                        {"name": "first", "conclusion": "success"},
                        *(
                            {"name": f"check-{index}", "conclusion": "success"}
                            for index in range(99)
                        ),
                    ]
                }
            if path.endswith("/check-runs?per_page=100&page=2"):
                return {
                    "check_runs": [
                        {"name": "first", "conclusion": "failure"},
                        {"name": "second", "conclusion": "success"},
                    ]
                }
            if path.endswith("/status?per_page=100&page=1"):
                return {
                    "statuses": [
                        {"context": "second", "state": "failure"},
                        {"context": "legacy", "state": "success"},
                        *(
                            {"context": f"status-{index}", "state": "success"}
                            for index in range(98)
                        ),
                    ]
                }
            if path.endswith("/status?per_page=100&page=2"):
                return {
                    "statuses": [
                        {"context": "legacy-two", "state": "success"},
                    ]
                }
            return super().get(path)

    transport = Paginated()
    observed = read_check_runs(PipelineConfig(), transport, HEAD_SHA)

    assert "legacy" in [item.name for item in observed]
    assert "legacy-two" in [item.name for item in observed]
    assert next(item for item in observed if item.name == "first").conclusion == "success"
    assert any(read.endswith("check-runs?per_page=100&page=2") for read in transport.reads)
    assert any(read.endswith("status?per_page=100&page=1") for read in transport.reads)
    assert any(read.endswith("status?per_page=100&page=2") for read in transport.reads)


def test_check_read_transport_failure_is_evidence_unavailable() -> None:
    class Interrupted(FakeGitHubTransport):
        def get(self, path: str) -> object:
            self.reads.append(path)
            if path.endswith("/check-runs?per_page=100&page=1"):
                return {
                    "check_runs": [
                        {"name": name, "conclusion": "success"}
                        for name in DEFAULT_REQUIRED_CONTEXTS_MIN
                    ]
                    + [{"name": f"check-{index}", "conclusion": "success"} for index in range(99)]
                }
            if path.endswith("/check-runs?per_page=100&page=2"):
                raise HttpTransportError("connection interrupted")
            return super().get(path)

    result = wait_for_check_runs(
        PipelineConfig(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        client=Interrupted(),
        elapsed_s=0,
        sha=HEAD_SHA,
        poll=False,
    )

    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.auto_merge_eligible is False
    assert "connection interrupted" in (result.detail or "")


def test_check_read_page_cap_fails_closed() -> None:
    class TooManyPages(FakeGitHubTransport):
        def get(self, path: str) -> object:
            self.reads.append(path)
            if "/check-runs?per_page=100&page=" in path:
                return {
                    "check_runs": [
                        {"name": f"check-{index}", "conclusion": "success"} for index in range(100)
                    ]
                }
            return super().get(path)

    with pytest.raises(PreflightError, match="pagination exceeded"):
        read_check_runs(PipelineConfig(), TooManyPages(), HEAD_SHA)


def test_a_fork_with_a_workflow_run_but_no_context_is_awaiting_approval() -> None:
    """§10.1 (line 601) — the fork gate reports *no* context, and that is not silence-as-green."""
    clock = Clock()

    result = wait(
        wait_config(ci_wait_timeout_s=600),
        statuses={},
        clock=clock,
        is_fork=True,
        client=FakeGitHubTransport(completed_workflow_runs=True),
    )

    assert clock.slept == [15.0]
    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.detail == "awaiting_workflow_approval"


def test_a_fork_with_no_workflow_run_at_all_reports_absent_workflows() -> None:
    """§10.1 — a repository that never started a run is not a repository awaiting approval.

    Both leave CI evidence unavailable, but only one is unblocked by a human clicking
    approve; conflating them sent a LIVE run looking for an approval gate that a fork
    without workflows does not have.
    """
    clock = Clock()

    result = wait(
        wait_config(ci_wait_timeout_s=600),
        statuses={},
        clock=clock,
        is_fork=True,
        client=FakeGitHubTransport(completed_workflow_runs=False),
    )

    assert clock.slept == [15.0]
    assert result.reason is ReasonCode.CI_WORKFLOWS_ABSENT
    assert result.detail == "ci_workflows_absent"
    assert result.auto_merge_eligible is False


def test_non_fork_pr_reporting_no_context_polls_to_the_deadline() -> None:
    """§10.1 — silence on a same-repo PR is in-flight CI, not the approval gate."""
    clock = Clock()

    result = wait(wait_config(ci_wait_timeout_s=30), statuses={}, clock=clock)

    assert clock.slept == [15.0, 15.0]
    assert result.detail is None
    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE


# -- §10.1 the timeout -------------------------------------------------------------------


def test_ci_wait_timeout_records_unavailable_and_refuses_auto_merge() -> None:
    """§10.1 (line 594) — on expiry the candidate is `ci_evidence_unavailable`, never eligible."""
    result = wait(
        wait_config(ci_evidence_mode=CiEvidenceMode.ACTIONS, auto_merge_enabled=True),
        statuses=all_contexts("pending"),
        elapsed_s=5400,
    )

    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.auto_merge_eligible is False


def test_ci_wait_budget_is_consumed_by_elapsed_time() -> None:
    """§10.1 — `ci_wait_timeout_s` is a budget: spent time leaves no window to poll in."""
    clock = Clock()

    wait(
        wait_config(ci_wait_timeout_s=60),
        statuses=all_contexts("pending"),
        clock=clock,
        elapsed_s=60,
    )

    assert clock.slept == []


def test_ci_wait_timeout_downgrades_evidence_to_local() -> None:
    """§10.1 (line 594) — expiry downgrades evidence to `local`, per candidate."""
    result = wait(
        wait_config(ci_wait_timeout_s=30),
        statuses=all_contexts("pending"),
        ci_mode=CiEvidenceMode.LOCAL,
    )

    assert result.mode is CiEvidenceMode.LOCAL


# -- §10 failing closed on every non-success state ---------------------------------------


@pytest.mark.parametrize("state", FAILED_STATES)
def test_failed_required_context_is_ci_check_failed(state: str) -> None:
    """§10 — a failing required context is a recorded failure, not a wait."""
    result = wait(wait_config(), statuses={**all_contexts(), "test-sqlite": state})

    assert result.reason is ReasonCode.CI_CHECK_FAILED
    assert result.auto_merge_eligible is False


@pytest.mark.parametrize("state", NON_SUCCESS_STATES)
@pytest.mark.parametrize("context", DEFAULT_REQUIRED_CONTEXTS_MIN)
def test_every_non_success_state_on_every_context_fails_closed(context: str, state: str) -> None:
    """§10 — every required context must literally be `success`; anything else refuses merge."""
    result = wait(
        wait_config(
            ci_evidence_mode=CiEvidenceMode.ACTIONS,
            auto_merge_enabled=True,
            ci_wait_timeout_s=1,
        ),
        statuses={**all_contexts(), context: state},
    )

    assert result.auto_merge_eligible is False
    assert result.reason is not None


# -- §14.1 artifact ordering -------------------------------------------------------------


def publish(
    transport: FakeGitHubTransport,
    *,
    config: PipelineConfig | None = None,
    candidate_fields: Mapping[str, Any] | None = None,
    ci_result: CiWaitResult | None = None,
    **overrides: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Publish one high-tier candidate through the mandated ordering."""
    resolved = config or live_config()
    client = GitHubClient(resolved, transport=transport)
    fields = dict(candidate_fields or {})
    candidate = codeql_candidate(tier=Tier.HIGH, **fields)
    probe_result = ci_result or CiWaitResult(CiEvidenceMode.LOCAL, None, False)
    return publish_artifacts(
        client,
        candidate,
        marker=overrides.pop("marker", f"<!-- devin-remediation:{candidate.candidate_id} -->"),
        issue_title=overrides.pop("issue_title", "issue title"),
        issue_body=overrides.pop("issue_body", "issue body"),
        pr_title=overrides.pop("pr_title", "fix(security): bound the generated range"),
        pr_body=overrides.pop("pr_body", "pr body"),
        head=overrides.pop("head", "devin/codeql-0"),
        ci_probe=overrides.pop("ci_probe", lambda _number: probe_result),
        **overrides,
    )


def test_medium_tier_stops_at_the_issue() -> None:
    """§6 — a MEDIUM-tier candidate is issue-only, so no PR is opened."""
    transport = FakeGitHubTransport()
    client = GitHubClient(live_config(), transport=transport)

    links = publish_artifacts(
        client,
        codeql_candidate(tier=Tier.MEDIUM),
        marker="<!-- devin-remediation:codeql-0 -->",
        issue_title="issue title",
        issue_body="issue body",
    )

    assert links.pr_url is None
    assert transport.write_paths == [ISSUE_PATH]


def test_pr_publication_requires_an_observed_ci_probe() -> None:
    """§10 — publication may never assume CI evidence it has not observed."""
    transport = FakeGitHubTransport()
    client = GitHubClient(live_config(), transport=transport)

    with pytest.raises(ValueError, match="CI probe"):
        publish_artifacts(
            client,
            codeql_candidate(tier=Tier.HIGH),
            marker="<!-- devin-remediation:codeql-0 -->",
            issue_title="issue title",
            issue_body="issue body",
            pr_title="fix(security): x",
            pr_body="pr body",
            head="devin/codeql-0",
        )


def test_duplicate_head_adopts_the_open_pull_request() -> None:
    """§14.1 — a 422 on an existing head adopts that PR rather than failing the candidate."""
    transport = FakeGitHubTransport(
        create_pr_error=HttpTransportError("already exists", status_code=422),
        existing_pull_requests=[
            {
                "number": 55,
                "html_url": "https://github.test/pull/55",
                "state": "open",
                "merged_at": None,
            }
        ],
    )

    links = publish(transport)

    assert links.pr_number == 55
    assert transport.write_paths == [ISSUE_PATH]


def test_duplicate_head_with_a_merged_pull_request_is_reported() -> None:
    """§14.1 — an externally merged head is reported, never re-opened."""
    transport = FakeGitHubTransport(
        create_pr_error=HttpTransportError("already exists", status_code=422),
        existing_pull_requests=[
            {
                "number": 55,
                "html_url": "https://github.test/pull/55",
                "state": "closed",
                "merged_at": "2026-08-29T12:00:00Z",
            }
        ],
    )

    with pytest.raises(MergedPullRequestError):
        publish(transport)

    assert transport.write_paths == [ISSUE_PATH]


def test_duplicate_head_with_only_a_closed_pull_request_is_reported_closed() -> None:
    """§14.1 — a closed, unmerged head is a human-review outcome, not a re-open."""
    transport = FakeGitHubTransport(
        create_pr_error=HttpTransportError("already exists", status_code=422),
        existing_pull_requests=[
            {
                "number": 55,
                "html_url": "https://github.test/pull/55",
                "state": "closed",
                "merged_at": None,
            }
        ],
    )

    with pytest.raises(ClosedPullRequestError):
        publish(transport)


def test_structurally_invalid_response_is_a_contained_github_error() -> None:
    """A malformed GitHub response defers one candidate; it is not a bare `ValueError`."""

    class NumberlessTransport(FakeGitHubTransport):
        def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
            super().post(path, payload)
            return {"html_url": "https://github.test/issues/1"}

    transport = NumberlessTransport()

    with pytest.raises(GitHubResponseError) as raised:
        publish(transport)

    assert isinstance(raised.value, ArtifactUnavailableError)


def test_simulate_mode_refuses_every_write(simulate_config: PipelineConfig) -> None:
    """§8/§17 — SIMULATE performs zero remote writes, even with a transport present."""
    transport = FakeGitHubTransport()
    client = GitHubClient(simulate_config, transport=transport)

    with pytest.raises(SimulationWriteError):
        client.create_issue("title", "body", [])
    with pytest.raises(SimulationWriteError):
        client.create_pr("title", "body", head="devin/x", base="master")

    assert transport.writes == []


def test_dispatch_preflight_aborts_when_issues_disabled() -> None:
    """§17 — with issues unavailable and the default sink, publishing aborts before any write."""
    transport = FakeGitHubTransport()

    with pytest.raises(ArtifactUnavailableError):
        publish(transport, config=live_config(has_issues=False))
    assert transport.writes == []


def test_preflight_ignores_newer_non_master_analysis() -> None:
    """A fresh master Python analysis remains authoritative over pull-request scans."""
    transport = FakeGitHubTransport(
        completed_workflow_runs=True,
        code_scanning_analyses=[
            {
                "commit_sha": "pull-request-sha",
                "ref": "refs/pull/31/head",
                "category": "/language:python",
            },
            {
                "commit_sha": BASE_SHA,
                "ref": "refs/heads/master",
                "category": "/language:python",
            },
        ],
    )

    run_live_preflight(live_config(), transport)

    assert any(
        read.endswith("/code-scanning/analyses?ref=refs/heads/master") for read in transport.reads
    )


def test_preflight_rejects_stale_master_python_analysis() -> None:
    """A master Python analysis on another revision blocks LIVE."""
    transport = FakeGitHubTransport(
        completed_workflow_runs=True,
        code_scanning_analyses=[
            {
                "commit_sha": "stale-sha",
                "ref": "refs/heads/master",
                "category": "/language:python",
            }
        ],
    )

    with pytest.raises(PreflightError, match="latest master Python CodeQL analysis"):
        run_live_preflight(live_config(), transport)


@pytest.mark.parametrize(
    "analyses",
    [
        [
            {
                "commit_sha": BASE_SHA,
                "ref": "refs/heads/master",
                "category": "/language:javascript-typescript",
            }
        ],
        [],
    ],
    ids=["javascript-only", "empty"],
)
def test_preflight_rejects_without_master_python_analysis(analyses: object) -> None:
    """LIVE requires a master Python analysis, not merely another CodeQL analysis."""
    transport = FakeGitHubTransport(
        completed_workflow_runs=True,
        code_scanning_analyses=analyses,
    )

    with pytest.raises(PreflightError, match="no master Python CodeQL analysis"):
        run_live_preflight(live_config(), transport)


# -- §10.1/§13 the auto-merge conjunction ------------------------------------------------


def github_ci(**fields: Any) -> CiWaitResult:  # noqa: ANN401
    """A CI result that observed `github` evidence with every context green."""
    return CiWaitResult(
        fields.pop("mode", CiEvidenceMode.ACTIONS),
        fields.pop("reason", None),
        fields.pop("auto_merge_eligible", True),
        fields.pop("detail", None),
    )


def test_auto_merge_is_requested_when_every_condition_holds() -> None:
    """§13 — `github` evidence, all contexts green, test evidence, and both knobs on."""
    transport = FakeGitHubTransport()

    links = publish(
        transport,
        config=github_evidence_config(),
        candidate_fields={
            "auto_merge_eligible": True,
            "test_added": True,
            "merge_mode": MergeMode.AUTO,
        },
        ci_result=github_ci(),
    )

    assert AUTO_MERGE_PATH in transport.write_paths
    assert links.auto_merge_requested is True


def test_an_approved_test_exemption_substitutes_for_a_new_test() -> None:
    """§9 — a recorded exemption is the only substitute for test evidence."""
    transport = FakeGitHubTransport()

    links = publish(
        transport,
        config=github_evidence_config(),
        candidate_fields={
            "auto_merge_eligible": True,
            "test_added": False,
            "test_exempt_reason": ReasonCode.STALE_SKIP,
            "merge_mode": MergeMode.AUTO,
        },
        ci_result=github_ci(),
    )

    assert links.auto_merge_requested is True


def test_lane1_test_exemption_keeps_auto_merge_reachable() -> None:
    """LANE 1 does not need a new test when its criterion is observed."""
    transport = FakeGitHubTransport()

    links = publish(
        transport,
        config=github_evidence_config(),
        candidate_fields={
            "auto_merge_eligible": True,
            "test_added": None,
            "test_exempt_reason": ReasonCode.TEST_NOT_REQUIRED,
            "merge_mode": MergeMode.AUTO,
        },
        ci_result=github_ci(),
    )

    assert links.auto_merge_requested is True


@pytest.mark.parametrize(
    ("label", "candidate_fields", "ci_result"),
    [
        (
            "local evidence",
            {"auto_merge_eligible": True, "test_added": True},
            github_ci(mode=CiEvidenceMode.LOCAL),
        ),
        (
            "ci reason recorded",
            {"auto_merge_eligible": True, "test_added": True},
            github_ci(reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE, auto_merge_eligible=False),
        ),
        (
            "ci check failed",
            {"auto_merge_eligible": True, "test_added": True},
            github_ci(reason=ReasonCode.CI_CHECK_FAILED, auto_merge_eligible=False),
        ),
        (
            "wait refused eligibility",
            {"auto_merge_eligible": True, "test_added": True},
            github_ci(auto_merge_eligible=False),
        ),
        (
            "no test evidence",
            {"auto_merge_eligible": True, "test_added": None},
            github_ci(),
        ),
        (
            "candidate not eligible",
            {"auto_merge_eligible": False, "test_added": True},
            github_ci(),
        ),
        (
            "candidate eligibility unresolved",
            {"auto_merge_eligible": None, "test_added": True},
            github_ci(),
        ),
    ],
)
def test_auto_merge_fails_closed_and_never_reaches_enable_auto_merge(
    label: str,
    candidate_fields: Mapping[str, Any],
    ci_result: CiWaitResult,
) -> None:
    """§13 — the conjunction fails closed: no weaker evidence may reach `enable_auto_merge`."""
    transport = FakeGitHubTransport()

    links = publish(
        transport,
        config=github_evidence_config(),
        candidate_fields=candidate_fields,
        ci_result=ci_result,
    )

    assert AUTO_MERGE_PATH not in transport.write_paths, label
    assert links.auto_merge_requested is False


def test_auto_merge_knob_off_never_reaches_enable_auto_merge() -> None:
    """§13 — `auto_merge_enabled = false` is a hard refusal at the publication edge."""
    transport = FakeGitHubTransport()

    links = publish(
        transport,
        config=live_config(ci_evidence_mode=CiEvidenceMode.ACTIONS),
        candidate_fields={
            "auto_merge_eligible": True,
            "test_added": True,
            "merge_mode": MergeMode.AUTO,
        },
        ci_result=github_ci(),
    )

    assert AUTO_MERGE_PATH not in transport.write_paths
    assert links.auto_merge_requested is False


def test_local_ci_evidence_forces_the_auto_merge_knob_off() -> None:
    """§10.1 — `ci_evidence_mode = local` forces `auto_merge_enabled = false` at config level."""
    config = live_config(ci_evidence_mode=CiEvidenceMode.LOCAL, auto_merge_enabled=True)

    assert config.auto_merge_enabled is False


def test_enable_auto_merge_rechecks_the_mode_at_the_client_edge() -> None:
    """§13 — the client re-checks configuration and mode; weaker evidence raises before writing."""
    transport = FakeGitHubTransport()
    client = GitHubClient(github_evidence_config(), transport=transport)

    with pytest.raises(ValueError):
        client.enable_auto_merge(2, ci_mode=CiEvidenceMode.LOCAL)

    assert transport.writes == []


def test_enable_auto_merge_is_refused_when_the_knob_is_off() -> None:
    """§13 — the knob is re-checked at the edge, not only by the caller."""
    transport = FakeGitHubTransport()
    client = GitHubClient(live_config(ci_evidence_mode=CiEvidenceMode.ACTIONS), transport=transport)

    with pytest.raises(ValueError):
        client.enable_auto_merge(2, ci_mode=CiEvidenceMode.ACTIONS)

    assert transport.writes == []


def test_enable_auto_merge_does_not_observe_a_merge_in_the_same_run() -> None:
    """§11 — auto-merge is a *request*; the merge is reconciled by a later run's discovery."""
    transport = FakeGitHubTransport()
    client = GitHubClient(github_evidence_config(), transport=transport)

    client.enable_auto_merge(2, ci_mode=CiEvidenceMode.ACTIONS)
    assert transport.write_paths == [AUTO_MERGE_PATH]


# -- provenance reads used by the CI probe -----------------------------------------------


def test_pull_request_head_metadata_reports_fork_origin() -> None:
    """§10.1 — the fork flag comes from the PR head repository, not from configuration."""
    client = GitHubClient(
        live_config(), transport=FakeGitHubTransport(head_repo_full_name="contributor/superset")
    )

    sha, is_fork = client.pull_request_head_metadata(2)

    assert sha == HEAD_SHA
    assert is_fork is True


def test_pull_request_head_metadata_reports_same_repository_heads() -> None:
    """§10.1 — a same-repository head is not subject to the fork approval gate."""
    client = GitHubClient(live_config(), transport=FakeGitHubTransport())

    sha, is_fork = client.pull_request_head_metadata(2)

    assert sha == HEAD_SHA
    assert is_fork is False


def test_commit_messages_between_identical_shas_is_empty() -> None:
    """§9.3 — `base_sha == head_sha` means no commits exist to review."""
    client = GitHubClient(live_config(), transport=FakeGitHubTransport())

    assert client.commit_messages_between(BASE_SHA, BASE_SHA) == []
    assert client.commit_messages_between(BASE_SHA, HEAD_SHA) != []


def test_ensure_label_creates_a_missing_label_once() -> None:
    """§7 — a label that cannot be read is created once and memoized per run."""
    transport = FakeGitHubTransport(labels_present=False)
    client = GitHubClient(live_config(), transport=transport)

    assert client.ensure_label("needs-human-review") is True
    assert client.ensure_label("needs-human-review") is True
    assert transport.write_paths == ["/repos/victorciao/superset/labels"]


def test_a_label_that_cannot_be_read_fails_closed() -> None:
    """§17 — an unreadable required label is a capability failure, not an absent label.

    Treating a `403` like a `404` would make the client attempt a creation it cannot perform and
    then publish an unlabelled human-review artifact, which is the routing §14 requires.
    """
    transport = FakeGitHubTransport(
        label_read_error=HttpTransportError("forbidden", status_code=403)
    )
    client = GitHubClient(live_config(), transport=transport)

    with pytest.raises(LabelCapabilityError, match="needs-human-review"):
        client.ensure_label("needs-human-review")
    assert transport.writes == []


def test_a_label_creation_denial_fails_closed_and_stays_denied() -> None:
    """§17 — the fork may not carry `needs-human-review`, so creation is part of the path."""
    transport = FakeGitHubTransport(
        labels_present=False,
        label_create_error=HttpTransportError("forbidden", status_code=403),
    )
    client = GitHubClient(live_config(), transport=transport)

    with pytest.raises(LabelCapabilityError, match="needs-human-review"):
        client.ensure_label("needs-human-review")
    with pytest.raises(LabelCapabilityError, match="needs-human-review"):
        client.ensure_label("needs-human-review")
    assert transport.writes == []


def test_a_denied_label_carries_the_capability_reason_code() -> None:
    """§17 — the raised error names the reason a caller must persist on the candidate."""
    assert LabelCapabilityError("denied").reason is ReasonCode.LABEL_CAPABILITY_UNAVAILABLE
