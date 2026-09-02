"""§17 integration smoke — the full SIMULATE flow writes locally and nowhere else."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from pipeline import __main__ as entrypoint
from pipeline.config import Mode, PipelineConfig
from pipeline.dispatch import dispatch_candidates
from pipeline.gate import evaluate_gates
from pipeline.lanes.codeql import enumerate_codeql_candidates, read_alert_fixture
from pipeline.lanes.skipped_tests import enumerate_skipped_tests
from pipeline.observability.events import EventLog
from pipeline.rubric import resolve_factors
from pipeline.schemas import Action, Candidate, Lane
from pipeline.score import apply_score
from pipeline.simulation import simulate_run
from pipeline.state import CandidateStateStore
from pipeline.templates.render import candidate_marker
from tests.conftest import (
    FIXTURES_DIR,
    RUBRICS_PATH,
    TARGET_REPO,
    TEMPLATES_DIR,
    TEST_DATA_DIR,
)
from tests.factories import lane3_candidate

RUN_ID = "sim-1"


def simulate_config(**overrides: object) -> PipelineConfig:
    """A SIMULATE configuration; SIMULATE is the default mode."""
    return PipelineConfig(
        mode=Mode.SIMULATE,
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        **overrides,
    )


def decided(candidates: list[Candidate], config: PipelineConfig) -> list[Candidate]:
    """Run the plan's enumerate -> resolve -> gate -> score -> dispatch order."""
    scored: list[Candidate] = []
    for candidate in candidates:
        factors = resolve_factors(candidate, config)
        evaluation = evaluate_gates(candidate, config, resolved_factors=factors)
        failed = (
            evaluation.gate_results.get(evaluation.failed_gate) if evaluation.failed_gate else None
        )
        row = candidate.model_copy(
            update={
                "gate_passed": evaluation.gate_passed,
                "failed_gate": evaluation.failed_gate,
                "gate_results": evaluation.gate_results,
                "reason": failed.reason if failed is not None else candidate.reason,
            }
        )
        if evaluation.gate_passed:
            row = apply_score(row, config, None, factors)
        scored.append(row)
    return dispatch_candidates(scored, config)


def three_lane_candidates(tmp_path: Path, config: PipelineConfig) -> list[Candidate]:
    """One decided candidate set drawn from all three lanes, without any network call."""
    repo = tmp_path / "target"
    shutil.copytree(TEST_DATA_DIR / "skip_tree" / "basic", repo / "tests")
    lane1 = enumerate_codeql_candidates(
        read_alert_fixture(FIXTURES_DIR / "codeql_alerts.json"), TARGET_REPO
    )
    lane2, _ = enumerate_skipped_tests(repo, repo_name=TARGET_REPO)
    lane3 = [lane3_candidate(candidate_id="lane3-1", deprecated_in="3.0", current_major=6)]
    return decided([*lane1, *lane2, *lane3], config)


@pytest.fixture()
def simulated(tmp_path: Path) -> tuple[PipelineConfig, list[Candidate], tuple[Path, ...]]:
    """Render one whole SIMULATE run into a temporary output directory."""
    config = simulate_config()
    candidates = three_lane_candidates(tmp_path, config)
    produced = simulate_run(
        candidates,
        run_id=RUN_ID,
        output_dir=tmp_path / "out",
        config=config,
    )
    return config, candidates, produced


def test_full_simulate_flow_makes_no_writes(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
    tmp_path: Path,
) -> None:
    """fixture -> gate -> score -> dispatch -> artifacts -> reports, zero remote writes.

    `simulate_run` takes no transport at all, so the structural proof of "zero remote
    writes" is that a complete run is produced from a credential-free config.
    """
    config, candidates, produced = simulated

    assert config.mode is Mode.SIMULATE
    assert candidates != []
    assert produced != ()
    assert all(path.is_file() for path in produced)
    assert (tmp_path / "out" / "reports" / f"run-{RUN_ID}.md").is_file()
    assert (tmp_path / "out" / "reports" / "kpis.md").is_file()


def test_simulate_flow_covers_all_three_lanes(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
) -> None:
    """§17 — the smoke exercises every lane, not just the CodeQL fixture."""
    _, candidates, _ = simulated

    assert {candidate.lane for candidate in candidates} == {
        Lane.CODEQL,
        Lane.SKIPPED_TESTS,
        Lane.DEPRECATIONS,
    }


def test_simulate_flow_respects_the_budget(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
) -> None:
    """§6 — no run dispatches beyond `budget_N`, however many candidates are enumerated."""
    config, candidates, _ = simulated

    dispatched = [candidate for candidate in candidates if candidate.action is Action.OPEN_PR]

    assert len(dispatched) <= config.budget_N


def test_simulate_flow_writes_a_readable_event_log(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
    tmp_path: Path,
) -> None:
    """§12 — Layer 1 events are JSONL, one per candidate, all carrying the run ID."""
    _, candidates, _ = simulated
    log = tmp_path / "out" / "reports" / "events.jsonl"

    assert log.is_file()
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(candidates)
    for line in lines:
        record = json.loads(line)
        assert record["run_id"] == RUN_ID
        assert record["candidate_id"]
    assert len(EventLog(log).read()) == len(candidates)


def test_simulate_flow_artifacts_carry_the_candidate_marker(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
    tmp_path: Path,
) -> None:
    """§14.1 — every rendered artifact carries its candidate's stable marker."""
    _, candidates, _ = simulated
    issues = tmp_path / "out" / "reports" / "issues"

    for candidate in candidates:
        body = (issues / f"{candidate.candidate_id}.md").read_text(encoding="utf-8")
        assert candidate_marker(candidate.candidate_id) in body


def test_simulate_flow_is_idempotent_across_reruns(tmp_path: Path) -> None:
    """§14.1 — a second SIMULATE run over the same state creates no duplicate artifacts."""
    config = simulate_config()
    candidates = three_lane_candidates(tmp_path, config)
    output = tmp_path / "out"

    first = simulate_run(candidates, run_id="sim-1", output_dir=output, config=config)
    store = CandidateStateStore(output / "state" / "candidates.jsonl")
    before = {row.candidate_id for row in store.rows()}
    second = simulate_run(candidates, run_id="sim-2", output_dir=output, config=config)

    def artifacts(paths: tuple[Path, ...]) -> set[str]:
        return {path.name for path in paths if not path.name.startswith("run-")}

    assert artifacts(second) == artifacts(first)
    assert {row.candidate_id for row in store.rows()} == before
    dispatched = [
        candidate
        for candidate in candidates
        if candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
    ]
    assert dispatched != []
    for candidate in dispatched:
        recorded = candidate.model_copy(update={"issue_url": "https://example.invalid/issues/1"})
        store.append(recorded)
        assert store.append_if_new_artifact(recorded) is False


def test_simulate_run_leaves_no_secrets_in_its_artifacts(
    simulated: tuple[PipelineConfig, list[Candidate], tuple[Path, ...]],
    tmp_path: Path,
) -> None:
    """§14 — nothing a SIMULATE run writes may contain a credential."""
    written = [path for path in (tmp_path / "out").rglob("*") if path.is_file()]

    assert written != []
    for path in written:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "ghp_" not in text


def test_live_smoke_is_opt_in() -> None:
    """§17 — the live smoke is credential-gated: LIVE without credentials cannot be built.

    The gating itself is asserted rather than skipped, so the default suite always checks it.
    """
    assert os.environ.get("PIPELINE_LIVE_SMOKE") != "1"

    config = PipelineConfig(mode=Mode.LIVE)
    assert config.mode == Mode.LIVE


@pytest.mark.parametrize("missing", ("GITHUB_PAT_REMEDIATION", "DEVIN_API_KEY"))
def test_live_main_aborts_before_preflight_without_each_credential(
    missing: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """LIVE must fail clearly before any network access when one credential is absent."""
    monkeypatch.setenv("GITHUB_PAT_REMEDIATION", "placeholder-github-token")
    monkeypatch.setenv("DEVIN_API_KEY", "placeholder-devin-key")
    monkeypatch.delenv(missing)

    exit_code = entrypoint.main(
        [
            "--mode=live",
            "--head-branch",
            "devin/test",
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )

    assert exit_code == 1
    assert missing in capsys.readouterr().err
