"""§10 CI evidence resolution, required contexts, and the §14 rate-limit backoff."""

from __future__ import annotations

from pipeline.config import CiEvidenceMode, PipelineConfig
from pipeline.schemas import ReasonCode
from tests import _api
from tests.fakes import FakeGitHubClient, FakeResponse, RateLimitedCall, SleepRecorder

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
    config = PipelineConfig(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True)
    client = FakeGitHubClient(reported_contexts=list(ASF_REQUIRED_CONTEXTS))

    result = _api.github_client().wait_for_required_contexts(
        config, client=client, elapsed_s=600, reported_contexts=list(ASF_REQUIRED_CONTEXTS)
    )

    assert result.mode == CiEvidenceMode.GITHUB
    assert result.reason is None
    assert result.auto_merge_eligible is True


def test_rate_limit_backoff() -> None:
    """§14 — a 429 with a reset header sleeps a bounded time and retries once."""
    call = RateLimitedCall(limited_calls=1, reset_header="2")
    sleeper = SleepRecorder()

    response = _api.github_client().request_with_backoff(
        call, sleep=sleeper, now=lambda: 0.0, max_retries=3
    )

    assert isinstance(response, FakeResponse)
    assert response.status_code == 200
    assert call.calls == 2
    assert len(sleeper.durations) == 1
    assert 0 < sleeper.durations[0] <= 60


def test_rate_limit_backoff_gives_up_after_max_retries() -> None:
    call = RateLimitedCall(limited_calls=5)
    sleeper = SleepRecorder()

    response = _api.github_client().request_with_backoff(
        call, sleep=sleeper, now=lambda: 0.0, max_retries=2
    )

    assert isinstance(response, FakeResponse)
    assert response.status_code == 429
    assert len(sleeper.durations) == 2
