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

from pipeline.config import CiEvidenceMode, IssueSink, Mode, PipelineConfig
from pipeline.github_client import (
    REQUIRED_CONTEXTS,
    ArtifactUnavailableError,
    CiModeTransition,
    CiWaitResult,
    ClosedPullRequestError,
    GitHubClient,
    GitHubResponseError,
    MergedPullRequestError,
    SimulationWriteError,
    maybe_upgrade_ci_mode,
    publish_artifacts,
    publish_degraded,
    wait_for_required_contexts,
)
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import ReasonCode, Tier
from tests.factories import codeql_candidate
from tests.fakes import (
    BASE_SHA,
    HEAD_SHA,
    FakeGitHubTransport,
    all_contexts,
)

ASF_REQUIRED_CONTEXTS = (
    "lint-check",
    "pre-commit (current)",
    "unit-tests-required",
    "test-postgres-required",
    "test-sqlite",
    "test-mysql",
    "test-postgres-hive",
    "test-postgres-presto",
    "frontend-build",
    "cypress-matrix-required",
    "playwright-tests-required",
    "dependency-review",
    "enforce-single-migration-head",
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
        ci_evidence_mode=CiEvidenceMode.GITHUB,
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
    return wait_for_required_contexts(
        config,
        client=client or FakeGitHubTransport(),
        elapsed_s=elapsed_s,
        reported_contexts=statuses,
        poll=poll,
        sleep=timer.sleep,
        clock=timer,
        poll_interval_s=15.0,
        is_fork=is_fork,
        ci_mode=ci_mode,
        on_mode_transition=on_mode_transition,
    )


# -- §10 the required context set --------------------------------------------------------


def test_required_contexts_are_the_thirteen_asf_contexts() -> None:
    """§10 — the required set is matched against rendered context strings."""
    assert tuple(REQUIRED_CONTEXTS) == ASF_REQUIRED_CONTEXTS


# -- §10.1 the one-way local -> github upgrade -------------------------------------------


def test_ci_evidence_mode_upgrades_once() -> None:
    """§10.1 — a reporting required context flips `local -> github` exactly once."""
    first = maybe_upgrade_ci_mode(CiEvidenceMode.LOCAL, reported_contexts=["unit-tests-required"])
    second = maybe_upgrade_ci_mode(
        CiEvidenceMode.GITHUB,
        reported_contexts=["unit-tests-required"],
        already_upgraded=True,
    )

    assert first.mode is CiEvidenceMode.GITHUB
    assert first.transitioned is True
    assert second.mode is CiEvidenceMode.GITHUB
    assert second.transitioned is False


def test_upgrade_requires_a_required_context_not_any_context() -> None:
    """§10.1 — the upgrade is driven by an *observed required* context, nothing weaker."""
    transition = maybe_upgrade_ci_mode(CiEvidenceMode.LOCAL, reported_contexts=["some-other-job"])

    assert transition.mode is CiEvidenceMode.LOCAL
    assert transition.transitioned is False


def test_pending_workflow_approval_keeps_local_evidence() -> None:
    """§10.1 — the fork approval gate is recorded, not treated as a failure."""
    transition = maybe_upgrade_ci_mode(
        CiEvidenceMode.LOCAL, reported_contexts=[], awaiting_workflow_approval=True
    )

    assert transition.mode is CiEvidenceMode.LOCAL
    assert transition.transitioned is False
    assert transition.reason is ReasonCode.AWAITING_WORKFLOW_APPROVAL


def test_ci_wait_emits_one_transition_and_keeps_github_evidence() -> None:
    """§10.1 — the upgrade happens inside the wait and is emitted once as a Layer 1 event."""
    transitions: list[CiModeTransition] = []

    result = wait(
        wait_config(),
        statuses=all_contexts(),
        ci_mode=CiEvidenceMode.LOCAL,
        on_mode_transition=transitions.append,
    )

    assert [transition.mode for transition in transitions] == [CiEvidenceMode.GITHUB]
    assert result.mode is CiEvidenceMode.GITHUB
    assert result.reason is None


def test_ci_wait_does_not_re_emit_a_transition_for_an_upgraded_run() -> None:
    """§10.1 — the mode is run-scoped, so a second candidate emits no second transition."""
    transitions: list[CiModeTransition] = []

    result = wait(
        wait_config(),
        statuses=all_contexts(),
        ci_mode=CiEvidenceMode.GITHUB,
        on_mode_transition=transitions.append,
    )

    assert transitions == []
    assert result.mode is CiEvidenceMode.GITHUB


def test_full_context_set_within_the_timeout_keeps_github_evidence() -> None:
    """§10.1 — every required context reporting `success` in the window is auto-merge eligible."""
    result = wait(
        wait_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
        statuses=all_contexts(),
    )

    assert result.mode is CiEvidenceMode.GITHUB
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

    result = wait_for_required_contexts(
        wait_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
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

    result = wait_for_required_contexts(
        wait_config(),
        client=transport,
        elapsed_s=0,
        sha=HEAD_SHA,
        poll=False,
    )

    assert result.detail == "awaiting_workflow_approval"


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
        wait_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan §10.1 line 594: on expiry the candidate's evidence is downgraded to `local`. "
        "The implementation returns the upgraded `github` mode after the deadline, so a "
        "timed-out candidate is reported under evidence its CI never produced."
    ),
)
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
@pytest.mark.parametrize("context", REQUIRED_CONTEXTS)
def test_every_non_success_state_on_every_context_fails_closed(context: str, state: str) -> None:
    """§10 — all 13 contexts must literally be `success`; anything else refuses auto-merge."""
    result = wait(
        wait_config(
            ci_evidence_mode=CiEvidenceMode.GITHUB,
            auto_merge_enabled=True,
            ci_wait_timeout_s=1,
        ),
        statuses={**all_contexts(), context: state},
    )

    assert result.auto_merge_eligible is False
    assert result.reason is not None


def test_a_missing_required_context_is_never_read_as_green() -> None:
    """§10 — an absent context is missing evidence, not a pass."""
    statuses = all_contexts()
    del statuses["dependency-review"]

    result = wait(wait_config(ci_wait_timeout_s=1), statuses=statuses)

    assert result.reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.auto_merge_eligible is False


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
        issue_title=overrides.pop("issue_title", "issue title"),
        issue_body=overrides.pop("issue_body", "issue body"),
        pr_title=overrides.pop("pr_title", "fix(security): bound the generated range"),
        pr_body=overrides.pop("pr_body", "pr body"),
        head=overrides.pop("head", "devin/codeql-0"),
        ci_probe=overrides.pop("ci_probe", lambda _number: probe_result),
        **overrides,
    )


def test_artifacts_are_created_issue_then_pr_then_issue_patch() -> None:
    """§14.1 — the mandated order is issue → PR carrying `Closes #n` → issue-body patch."""
    transport = FakeGitHubTransport()

    links = publish(transport)

    assert transport.write_paths == [ISSUE_PATH, PULLS_PATH, f"{ISSUE_PATH}/1"]
    assert "Closes #1" in str(transport.payload_for(PULLS_PATH)["body"])
    assert links.issue_url is not None and links.pr_url is not None
    assert links.pr_url in str(transport.payload_for(f"{ISSUE_PATH}/1")["body"])


def test_publication_callbacks_fire_in_lifecycle_order() -> None:
    """§14.1 — each durable state row is written from its own lifecycle callback, in order."""
    transport = FakeGitHubTransport()
    events: list[str] = []

    publish(
        transport,
        after_issue=lambda number, url: events.append(f"issue:{number}"),
        after_pr_created=lambda number, url: events.append(f"pr:{number}"),
        after_ci=lambda number: events.append(f"ci:{number}"),
        after_issue_patched=lambda url: events.append("issue_patched"),
    )

    assert events == ["issue:1", "pr:2", "ci:2", "issue_patched"]


def test_medium_tier_stops_at_the_issue() -> None:
    """§6 — a MEDIUM-tier candidate is issue-only, so no PR is opened."""
    transport = FakeGitHubTransport()
    client = GitHubClient(live_config(), transport=transport)

    links = publish_artifacts(
        client,
        codeql_candidate(tier=Tier.MEDIUM),
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
            issue_title="issue title",
            issue_body="issue body",
            pr_title="fix(security): x",
            pr_body="pr body",
            head="devin/codeql-0",
        )


def test_existing_issue_and_pr_are_adopted_without_recreating_them() -> None:
    """§14.1 — a resumed candidate patches its existing artifacts, it does not duplicate them."""
    transport = FakeGitHubTransport()
    created: list[int] = []

    links = publish(
        transport,
        existing_issue_number=7,
        existing_issue_url="https://github.test/issues/7",
        existing_pr_number=2,
        existing_pr_url="https://github.test/pull/2",
        after_issue=lambda number, url: created.append(number),
        after_pr_created=lambda number, url: created.append(number),
    )

    assert transport.write_paths == [f"{ISSUE_PATH}/7"]
    assert created == []
    assert links.issue_number == 7
    assert links.pr_number == 2


def test_merged_existing_pr_is_cross_linked_before_it_is_reported() -> None:
    """§14.1 — a merge found in the crash window still completes the `PR:` cross-link."""
    transport = FakeGitHubTransport(pr_merged_at="2026-08-29T12:00:00Z", pr_state="closed")
    patched: list[str] = []

    with pytest.raises(MergedPullRequestError) as raised:
        publish(
            transport,
            existing_issue_number=7,
            existing_issue_url="https://github.test/issues/7",
            existing_pr_number=2,
            existing_pr_url="https://github.test/pull/2",
            after_issue_patched=patched.append,
        )

    assert transport.write_paths == [f"{ISSUE_PATH}/7"]
    assert "PR: https://github.test/pull/2" in str(transport.payload_for(f"{ISSUE_PATH}/7")["body"])
    assert patched == ["https://github.test/pull/2"]
    assert raised.value.match.merged_at == "2026-08-29T12:00:00Z"


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
    assert transport.write_paths == [ISSUE_PATH, f"{ISSUE_PATH}/1"]


def test_duplicate_head_with_a_merged_pull_request_cross_links_then_reports() -> None:
    """§14.1 — an externally merged head is cross-linked and reported, never re-opened."""
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

    assert transport.write_paths == [ISSUE_PATH, f"{ISSUE_PATH}/1"]
    assert "PR: https://github.test/pull/55" in str(
        transport.payload_for(f"{ISSUE_PATH}/1")["body"]
    )


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


# -- §7 preflight and the degraded sink --------------------------------------------------


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


def test_degraded_sink_opens_a_pr_and_one_manager_comment() -> None:
    """§17 — the `pr_comment` sink publishes a PR plus a manager-facing comment."""
    transport = FakeGitHubTransport()
    client = GitHubClient(
        live_config(has_issues=False, issue_sink=IssueSink.PR_COMMENT),
        transport=transport,
    )

    links = publish_degraded(
        client,
        codeql_candidate(tier=Tier.HIGH),
        pr_title="fix(security): x",
        pr_body="pr body",
        comment_body="manager summary",
        head="devin/x",
    )

    assert links.issue_url is None
    assert links.pr_url is not None
    assert links.comment_url is not None
    assert transport.write_paths == [PULLS_PATH, f"{ISSUE_PATH}/1/comments"]


def test_degraded_sink_is_rejected_when_issues_are_available() -> None:
    """§14.1 — the degraded path is only legal while the issues capability is off."""
    transport = FakeGitHubTransport()
    client = GitHubClient(live_config(), transport=transport)

    with pytest.raises(ArtifactUnavailableError):
        publish_degraded(
            client,
            codeql_candidate(tier=Tier.HIGH),
            pr_title="fix(security): x",
            pr_body="pr body",
            comment_body="manager summary",
            head="devin/x",
        )

    assert transport.writes == []


# -- §10.1/§13 the auto-merge conjunction ------------------------------------------------


def github_ci(**fields: Any) -> CiWaitResult:  # noqa: ANN401
    """A CI result that observed `github` evidence with every context green."""
    return CiWaitResult(
        fields.pop("mode", CiEvidenceMode.GITHUB),
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
        candidate_fields={"auto_merge_eligible": True, "test_added": True},
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
        config=live_config(ci_evidence_mode=CiEvidenceMode.GITHUB),
        candidate_fields={"auto_merge_eligible": True, "test_added": True},
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
    client = GitHubClient(live_config(ci_evidence_mode=CiEvidenceMode.GITHUB), transport=transport)

    with pytest.raises(ValueError):
        client.enable_auto_merge(2, ci_mode=CiEvidenceMode.GITHUB)

    assert transport.writes == []


def test_enable_auto_merge_does_not_observe_a_merge_in_the_same_run() -> None:
    """§11 — auto-merge is a *request*; the merge is reconciled by a later run's discovery."""
    transport = FakeGitHubTransport()
    client = GitHubClient(github_evidence_config(), transport=transport)

    client.enable_auto_merge(2, ci_mode=CiEvidenceMode.GITHUB)
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
