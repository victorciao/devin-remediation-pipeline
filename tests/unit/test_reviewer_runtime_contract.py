"""Reviewer-authored tests for §5 triggers, §7 discovery, §12/§15 preconditions and §14 KPIs.

Each test pins a plan sentence that no existing test covers: discovery is fresh and never
fixture-fed under LIVE, every run enumerates all three lanes, the LIVE preconditions abort
rather than degrade, and every §15 knob value in the plan is settable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pipeline.config import (
    BUDGET_HARD_MAX,
    CiEvidenceMode,
    ConfigError,
    Mode,
    PipelineConfig,
    load_config,
)
from pipeline.github_client import PreflightError, run_live_preflight
from pipeline.observability.kpis import compute_kpis
from pipeline.schemas import (
    Candidate,
    CandidateState,
    CriterionEvidence,
    EventRecord,
    Lane,
    Tier,
)
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR
from tests.factories import codeql_candidate
from tests.fakes import FakeGitHubTransport


def config(**overrides: Any) -> PipelineConfig:  # noqa: ANN401
    """A configuration pointed at the shipped rubrics and templates."""
    return PipelineConfig(rubrics_path=RUBRICS_PATH, templates_dir=TEMPLATES_DIR, **overrides)


def live_config(**overrides: Any) -> PipelineConfig:  # noqa: ANN401
    """A LIVE configuration carrying placeholder credentials (never real secrets)."""
    return config(
        mode=Mode.LIVE,
        **overrides,
    )


def _target_repo(root: Path) -> Path:
    """A minimal target checkout carrying one unconditionally skipped test."""
    repo = root / "superset"
    (repo / "tests").mkdir(parents=True)
    (repo / "superset").mkdir()
    (repo / "tests" / "test_thing.py").write_text(
        'import pytest\n\n\n@pytest.mark.skip(reason="broken since the api/v1 migration")\n'
        "def test_thing() -> None:\n    assert False\n",
        encoding="utf-8",
    )
    return repo


def _baseline(valid_lanes: list[str]) -> dict[str, object]:
    return {
        "baseline_valid_lanes": valid_lanes,
        "current_major": 6,
        "current_release": "6.0.0",
        "skipped_tests": [
            {"nodeid": "tests/invented.py::test_invented", "kind": "function", "enclosed_tests": 1}
        ],
        "deprecations": [{"locator": "superset.invented:Symbol.method", "deprecated_in": "3.0"}],
        "codeql_alerts": [],
    }


# -- §7: LIVE discovery is fresh; fixtures are the SIMULATE input only -------------------


def test_live_discovery_never_synthesizes_candidates_from_the_baseline_fixture(
    tmp_path: Path,
) -> None:
    """§7: `fixtures/baseline.json` is the SIMULATE input only."""
    from pipeline.__main__ import _enumerate

    notes: list[str] = []
    candidates = _enumerate(
        config=live_config(),
        baseline=_baseline(["skipped_tests", "deprecations"]),
        repo_path=tmp_path / "missing-checkout",
        repo_name="victorciao/superset",
        target_exists=False,
        base_sha="a" * 40,
        preflight=None,
        notes=notes,
    )
    assert candidates == [], (
        "LIVE discovery fabricated candidates from the baseline fixture with no target checkout"
    )


def test_every_run_enumerates_all_three_lanes_regardless_of_the_baseline_fixture(
    tmp_path: Path,
) -> None:
    """§5: every run enumerates all three lanes; nothing about lane selection is derived."""
    from pipeline.__main__ import _enumerate

    repo = _target_repo(tmp_path)
    notes: list[str] = []
    candidates = _enumerate(
        config=config(),
        baseline=_baseline(["codeql"]),
        repo_path=repo,
        repo_name="victorciao/superset",
        target_exists=True,
        base_sha="a" * 40,
        preflight=None,
        notes=notes,
    )
    assert any(candidate.lane is Lane.SKIPPED_TESTS for candidate in candidates), (
        "LANE 2 was skipped because the baseline fixture omitted it from baseline_valid_lanes"
    )


# -- §7/§15: LIVE preconditions abort before the first write -----------------------------


def test_live_preflight_checks_that_the_latest_analysis_sits_on_base_sha() -> None:
    """§7: the latest `master` analysis SHA must equal `base_sha`, else a LIVE startup error."""
    transport = FakeGitHubTransport(completed_workflow_runs=True)
    run_live_preflight(live_config(), transport)
    assert any("code-scanning/analyses" in read for read in transport.reads), (
        "LIVE preflight never read /code-scanning/analyses, so a stale alert set reads as no debt"
    )


def test_live_preflight_aborts_without_a_completed_actions_run() -> None:
    """§15 LIVE preconditions: Actions enabled with >=1 completed `pull_request` run."""
    transport = FakeGitHubTransport(completed_workflow_runs=False, workflow_count=1)
    with pytest.raises(PreflightError):
        run_live_preflight(live_config(), transport)


class _NoPushTransport(FakeGitHubTransport):
    """A target the token can read but not push to."""

    def get(self, path: str) -> object:
        value = super().get(path)
        if path == f"/repos/{self.owner}/{self.repo}" and isinstance(value, dict):
            return {**value, "permissions": {"push": False, "pull": True}}
        return value


def test_live_preflight_checks_push_access_on_the_target_repository() -> None:
    """§15 LIVE preconditions: `GET /repos/{o}/{r}` reachable **with push access**."""
    with pytest.raises(PreflightError):
        run_live_preflight(live_config(), _NoPushTransport(completed_workflow_runs=True))


# -- §15: every documented knob value is settable without code edits ---------------------


def test_documented_ci_evidence_mode_value_actions_is_settable() -> None:
    """§10/§15 name the two modes `local` and `actions`; both must load."""
    loaded = load_config(env={"PIPELINE_CI_EVIDENCE_MODE": "actions"}, cli_args=())
    assert loaded.ci_evidence_mode is not CiEvidenceMode.LOCAL


def test_budget_above_the_hard_max_is_clamped_rather_than_a_startup_error() -> None:
    """§15: `budget_N` is clamped at `BUDGET_HARD_MAX`, logging `guardrail_clamped`."""
    loaded = load_config(env={"PIPELINE_BUDGET_N": str(BUDGET_HARD_MAX + 5)}, cli_args=())
    assert loaded.budget_N == BUDGET_HARD_MAX


def test_live_requires_a_non_empty_required_contexts_min() -> None:
    """§15 LIVE preconditions: `required_contexts_min` non-empty, checked before any write."""
    with pytest.raises(ConfigError):
        live_config(required_contexts_min=())


# -- §14: KPI definitions ----------------------------------------------------------------


def _event(**fields: Any) -> EventRecord:  # noqa: ANN401
    base: dict[str, Any] = {
        "run_id": "run",
        "candidate_id": "cand",
        "lane": Lane.SKIPPED_TESTS,
        "tier": Tier.HIGH,
    }
    base.update(fields)
    return EventRecord(**base)


def test_verification_pass_rate_is_per_dispatched_candidate() -> None:
    """§14 Layer 3: pass rate = candidates whose criterion was satisfied / candidates dispatched."""
    satisfied = CriterionEvidence(criterion="c", satisfied=True, commands=["pytest"])
    events = [
        _event(candidate_id="cand", session_id="s1", criterion_evidence=satisfied),
        _event(candidate_id="cand", session_id="s1"),
        _event(candidate_id="cand", session_id="s1", pr_url="https://github.test/pull/1"),
    ]
    candidates = [codeql_candidate(candidate_id="cand", run_id="run", session_id="s1")]
    metrics = compute_kpis(candidates, events, config())
    assert metrics["verification_pass_rate"] == 1.0, (
        "the pass rate counted state rows instead of dispatched candidates"
    )


def test_kpis_report_issues_created_separately_from_issues_adopted() -> None:
    """§14 Layer 3: issues created and issues adopted, split by tier."""
    metrics = compute_kpis([], [], config())
    assert {"issues_created", "issues_adopted"} <= set(metrics), (
        "the KPI rollup does not distinguish created from adopted issues"
    )


def test_candidate_state_rows_account_for_every_candidate() -> None:
    """§14 Layer 2: no candidate may be unaccounted for in the run report."""
    candidate = Candidate(
        candidate_id="cand",
        lane=Lane.SKIPPED_TESTS,
        repo="victorciao/superset",
        stable_locator="tests/x.py::test_y",
        state=CandidateState.ENUMERATED,
    )
    metrics = compute_kpis([candidate], [], config())
    assert metrics["candidates_seen"] == 1
    accounted = [metrics["active"], metrics["completed"]]
    assert sum(value for value in accounted if isinstance(value, int)) == 1
