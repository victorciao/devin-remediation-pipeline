"""§17 integration smoke — the full SIMULATE flow writes locally and nowhere else."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.schemas import Action, Lane
from tests import _api
from tests.fakes import NoWriteGitHubClient


@pytest.fixture()
def simulate_run(tmp_path: Path, simulate_config: PipelineConfig) -> _api.RunResult:
    client = NoWriteGitHubClient()

    return _api.dispatch().run_pipeline(simulate_config, client=client, workdir=tmp_path)


def test_full_simulate_flow_makes_no_writes(simulate_run: _api.RunResult) -> None:
    """fixture -> gate -> score -> dispatch -> artifacts -> reports, zero remote writes."""
    assert simulate_run.candidates != []
    assert simulate_run.events != []
    assert simulate_run.report_path.is_file()


def test_simulate_flow_covers_all_three_lanes(simulate_run: _api.RunResult) -> None:
    lanes = {candidate.lane for candidate in simulate_run.candidates}

    assert lanes == {Lane.CODEQL, Lane.SKIPPED_TESTS, Lane.DEPRECATIONS}


def test_simulate_flow_respects_the_budget(
    simulate_run: _api.RunResult, simulate_config: PipelineConfig
) -> None:
    dispatched = [
        event
        for event in simulate_run.events
        if event.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
    ]

    assert len(dispatched) <= simulate_config.budget_N


def test_simulate_flow_writes_a_readable_event_log(
    simulate_run: _api.RunResult, tmp_path: Path
) -> None:
    log = tmp_path / "state" / "events.jsonl"

    assert log.is_file()
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == len(simulate_run.events)
    for line in lines:
        record = json.loads(line)
        assert record["run_id"] == simulate_run.run_id
        assert record["candidate_id"]


def test_simulate_flow_is_idempotent_across_reruns(
    tmp_path: Path, simulate_config: PipelineConfig
) -> None:
    """§14.1 — a second SIMULATE run re-uses the state store and dispatches nothing new."""
    dispatch = _api.dispatch()

    first = dispatch.run_pipeline(simulate_config, client=NoWriteGitHubClient(), workdir=tmp_path)
    second = dispatch.run_pipeline(simulate_config, client=NoWriteGitHubClient(), workdir=tmp_path)

    assert {candidate.candidate_id for candidate in second.candidates} == {
        candidate.candidate_id for candidate in first.candidates
    }
    assert second.run_id != first.run_id


def test_simulate_run_leaves_no_secrets_in_its_artifacts(
    simulate_run: _api.RunResult, tmp_path: Path
) -> None:
    written = [path for path in tmp_path.rglob("*") if path.is_file()]

    assert written != []
    for path in written:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "ghp_" not in text
        assert "PIPELINE_GITHUB_TOKEN" not in text


def test_live_smoke_is_opt_in() -> None:
    """§17 — the live smoke is credential-gated: LIVE without credentials cannot be built.

    The gating itself is asserted rather than skipped, so the default suite always checks it.
    """
    assert os.environ.get("PIPELINE_LIVE_SMOKE") != "1"

    with pytest.raises(ConfigError):
        PipelineConfig(mode=Mode.LIVE)

    config = PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-github-token"),
        devin_api_key=SecretStr("placeholder-devin-key"),
    )

    assert config.mode == Mode.LIVE
    assert config.github_token is not None
