"""§10 CI evidence resolution, §14 rate-limit backoff, and the artifact ordering contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest
from pydantic import SecretStr

from pipeline.config import CiEvidenceMode, IssueSink, Mode, PipelineConfig
from pipeline.github_client import (
    ArtifactUnavailableError,
    GitHubClient,
    GitHubRateLimitError,
    SimulationWriteError,
    publish_artifacts,
    publish_degraded,
)
from pipeline.schemas import ReasonCode, Tier
from tests import _api
from tests.factories import codeql_candidate
from tests.fakes import FakeGitHubClient, SleepRecorder

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


class RecordingTransport:
    """A transport that records every mutation and replays scripted responses."""

    def __init__(
        self,
        *,
        responses: Sequence[Mapping[str, object]] = (),
        rate_limited: int = 0,
        reset_at: float | None = None,
        retry_after: float | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, Mapping[str, object]]] = []
        self._responses = list(responses)
        self._rate_limited = rate_limited
        self._reset_at = reset_at
        self._retry_after = retry_after
        self.attempts = 0

    def _respond(
        self, method: str, path: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        self.attempts += 1
        if self._rate_limited > 0:
            self._rate_limited -= 1
            raise GitHubRateLimitError(
                "rate limited",
                retry_after=self._retry_after,
                reset_at=self._reset_at,
            )
        self.calls.append((method, path, payload))
        if self._responses:
            return self._responses.pop(0)
        return {"number": len(self.calls), "html_url": f"https://example.invalid{path}"}

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._respond("post", path, payload)

    def patch(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        return self._respond("patch", path, payload)


def live_config(**fields: object) -> PipelineConfig:
    """A LIVE-mode config; SIMULATE forbids every write path under test here."""
    return PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-token"),
        devin_api_key=SecretStr("placeholder-key"),
        **fields,
    )


def client_for(
    config: PipelineConfig,
    transport: RecordingTransport,
    *,
    sleeper: SleepRecorder | None = None,
) -> GitHubClient:
    """Build a client with injected clock and sleeper so no test ever really waits."""
    return GitHubClient(
        config,
        transport=transport,
        clock=lambda: 0.0,
        sleeper=(sleeper or SleepRecorder()),
    )


def test_required_contexts_are_the_thirteen_asf_contexts() -> None:
    """§10 — the required set is matched against rendered context strings."""
    assert tuple(_api.github_client().REQUIRED_CONTEXTS) == ASF_REQUIRED_CONTEXTS


def test_capability_probe_resolves_local_evidence_and_disables_auto_merge(
    simulate_config: PipelineConfig,
) -> None:
    """§0d/§10.1 — no reported contexts means `local` evidence and no auto-merge."""
    client = FakeGitHubClient(reported_contexts=[])

    report = _api.github_client().probe_capabilities(simulate_config, client=client)

    assert report.ci_evidence_mode == CiEvidenceMode.LOCAL
    assert report.auto_merge_enabled is False
    assert client.writes == []


def test_ci_evidence_mode_upgrades_once() -> None:
    """§10.1 — a reporting context flips `local -> github` exactly once."""
    upgrade = _api.github_client().maybe_upgrade_ci_mode

    first = upgrade(CiEvidenceMode.LOCAL, reported_contexts=["unit-tests-required"])
    second = upgrade(
        CiEvidenceMode.GITHUB, reported_contexts=["unit-tests-required"], already_upgraded=True
    )

    assert first.mode == CiEvidenceMode.GITHUB
    assert first.transitioned is True
    assert second.mode == CiEvidenceMode.GITHUB
    assert second.transitioned is False


def test_pending_workflow_approval_keeps_local_evidence() -> None:
    """§10.1 — the fork approval gate is recorded, not treated as a failure."""
    transition = _api.github_client().maybe_upgrade_ci_mode(
        CiEvidenceMode.LOCAL, reported_contexts=[], awaiting_workflow_approval=True
    )

    assert transition.mode == CiEvidenceMode.LOCAL
    assert transition.transitioned is False
    assert transition.reason == ReasonCode.AWAITING_WORKFLOW_APPROVAL


def test_ci_wait_timeout_downgrades_evidence() -> None:
    """§10.1/§17 — expiry gives `ci_evidence_unavailable`, `local` evidence, no auto-merge."""
    config = PipelineConfig(ci_evidence_mode=CiEvidenceMode.GITHUB, ci_wait_timeout_s=5400)
    client = FakeGitHubClient(reported_contexts=[])

    result = _api.github_client().wait_for_required_contexts(
        config, client=client, elapsed_s=5401, reported_contexts=[]
    )

    assert result.mode == CiEvidenceMode.LOCAL
    assert result.reason == ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert result.auto_merge_eligible is False


def test_full_context_set_within_the_timeout_keeps_github_evidence() -> None:
    """§10.1 — every required context reporting inside the window keeps `github` evidence."""
    config = PipelineConfig(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True)
    client = FakeGitHubClient(reported_contexts=list(ASF_REQUIRED_CONTEXTS))

    result = _api.github_client().wait_for_required_contexts(
        config, client=client, elapsed_s=600, reported_contexts=list(ASF_REQUIRED_CONTEXTS)
    )

    assert result.mode == CiEvidenceMode.GITHUB
    assert result.reason is None
    assert result.auto_merge_eligible is True


def test_rate_limit_backoff() -> None:
    """§14 — a 429 with a reset header sleeps a bounded, server-timed interval and retries."""
    transport = RecordingTransport(rate_limited=1, reset_at=2.0)
    sleeper = SleepRecorder()
    client = client_for(live_config(), transport, sleeper=sleeper)

    number, url = client.create_issue("title", "body", ["needs-human-review"])

    assert transport.attempts == 2
    assert sleeper.durations == [2.0]
    assert number == 1
    assert url.startswith("https://example.invalid/repos/")


def test_rate_limit_backoff_gives_up_after_max_retries() -> None:
    """§14 — the retry budget is bounded; a permanently limited API surfaces the error."""
    transport = RecordingTransport(rate_limited=10, retry_after=1.0)
    sleeper = SleepRecorder()
    client = GitHubClient(
        live_config(),
        transport=transport,
        clock=lambda: 0.0,
        sleeper=sleeper,
        max_attempts=3,
    )

    with pytest.raises(GitHubRateLimitError):
        client.create_issue("title", "body", [])

    assert transport.attempts == 3
    assert len(sleeper.durations) == 2


def test_rate_limit_wait_is_capped() -> None:
    """§14 — a far-future reset never sleeps past the bounded maximum."""
    transport = RecordingTransport(rate_limited=1, reset_at=10_000.0)
    sleeper = SleepRecorder()
    client = GitHubClient(
        live_config(),
        transport=transport,
        clock=lambda: 0.0,
        sleeper=sleeper,
        max_wait_s=30.0,
    )

    client.create_issue("title", "body", [])

    assert sleeper.durations == [30.0]


def test_simulate_mode_refuses_every_write(simulate_config: PipelineConfig) -> None:
    """§8/§17 — SIMULATE performs zero remote writes, even with a transport present."""
    transport = RecordingTransport()
    client = client_for(simulate_config, transport)

    with pytest.raises(SimulationWriteError):
        client.create_issue("title", "body", [])
    with pytest.raises(SimulationWriteError):
        client.create_pr("title", "body", head="devin/x", base="master")

    assert transport.calls == []


def test_artifacts_are_created_issue_then_pr_then_issue_patch() -> None:
    """§14.1 — the mandated order is issue → PR carrying `Closes #n` → issue-body patch."""
    transport = RecordingTransport()
    client = client_for(live_config(), transport)
    candidate = codeql_candidate(tier=Tier.HIGH)

    links = publish_artifacts(
        client,
        candidate,
        issue_title="issue title",
        issue_body="issue body",
        pr_title="fix(security): x",
        pr_body="pr body",
        head="devin/x",
    )

    methods = [(method, path.rsplit("/", 2)[-2:]) for method, path, _ in transport.calls]
    assert [method for method, _ in methods] == ["post", "post", "patch"]
    assert transport.calls[0][1].endswith("/issues")
    assert transport.calls[1][1].endswith("/pulls")
    assert "Closes #1" in str(transport.calls[1][2]["body"])
    assert links.issue_url is not None and links.pr_url is not None
    assert links.pr_url in str(transport.calls[2][2]["body"])


def test_medium_tier_stops_at_the_issue() -> None:
    """§6 — a MEDIUM-tier candidate is issue-only, so no PR is opened."""
    transport = RecordingTransport()
    client = client_for(live_config(), transport)

    links = publish_artifacts(
        client,
        codeql_candidate(tier=Tier.MEDIUM),
        issue_title="issue title",
        issue_body="issue body",
        pr_title="fix(security): x",
        pr_body="pr body",
        head="devin/x",
    )

    assert links.pr_url is None
    assert [path for _, path, _ in transport.calls] == [
        "/repos/victorciao/superset/issues",
    ]


def test_dispatch_preflight_aborts_when_issues_disabled() -> None:
    """§17 — with issues unavailable and the default sink, publishing aborts before any write."""
    transport = RecordingTransport()
    client = client_for(live_config(has_issues=False), transport)

    with pytest.raises(ArtifactUnavailableError):
        publish_artifacts(
            client,
            codeql_candidate(tier=Tier.HIGH),
            issue_title="issue title",
            issue_body="issue body",
            pr_title="fix(security): x",
            pr_body="pr body",
            head="devin/x",
        )

    assert transport.calls == []


def test_degraded_sink_opens_a_pr_and_one_manager_comment() -> None:
    """§17 — the `pr_comment` sink publishes a PR plus a manager-facing comment."""
    transport = RecordingTransport()
    client = client_for(
        live_config(has_issues=False, issue_sink=IssueSink.PR_COMMENT),
        transport,
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
    assert [path for _, path, _ in transport.calls] == [
        "/repos/victorciao/superset/pulls",
        "/repos/victorciao/superset/issues/1/comments",
    ]


def test_degraded_sink_is_rejected_when_issues_are_available() -> None:
    """§14.1 — the degraded path is only legal while the issues capability is off."""
    transport = RecordingTransport()
    client = client_for(live_config(), transport)

    with pytest.raises(ArtifactUnavailableError):
        publish_degraded(
            client,
            codeql_candidate(tier=Tier.HIGH),
            pr_title="fix(security): x",
            pr_body="pr body",
            comment_body="manager summary",
            head="devin/x",
        )

    assert transport.calls == []


def test_auto_merge_is_refused_under_local_ci_evidence() -> None:
    """§10.1/§17 — `ci_evidence_mode = local` hard-disables auto-merge at the client edge."""
    transport = RecordingTransport()
    client = client_for(live_config(ci_evidence_mode=CiEvidenceMode.LOCAL), transport)

    with pytest.raises(ValueError):
        client.enable_auto_merge(1)

    assert transport.calls == []
