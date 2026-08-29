"""§13 configuration: defaults, resolution rules, guardrails and secret handling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from pipeline.config import (
    BUDGET_HARD_MAX,
    SECURITY_ISSUE_MODE,
    CiEvidenceMode,
    ConfigError,
    KpiSink,
    Mode,
    PipelineConfig,
    load_config,
)
from pipeline.schemas import ReasonCode


def test_shipped_defaults_match_the_locked_values(simulate_config: PipelineConfig) -> None:
    """§13 — the locked values are defaults, and these are the ones §17 pins."""
    assert simulate_config.mode == Mode.SIMULATE
    assert simulate_config.iteration_cap == 5
    assert simulate_config.coverage_bar == 0.80
    assert simulate_config.budget_N == 10
    assert simulate_config.tier_high_min == 60
    assert simulate_config.tier_medium_min == 20
    assert simulate_config.eol_major_lag == 2
    assert simulate_config.merge_rate_floor == 0.50
    assert simulate_config.session_failure_ceiling == 0.30
    assert simulate_config.ci_evidence_mode == CiEvidenceMode.LOCAL
    assert simulate_config.auto_merge_enabled is False
    assert simulate_config.lane2_class_breadth_max == 5
    assert BUDGET_HARD_MAX == 25
    assert SECURITY_ISSUE_MODE == "generic_tracking"


def test_coverage_bar_default_meets_the_eighty_percent_floor(
    simulate_config: PipelineConfig,
) -> None:
    assert simulate_config.coverage_bar >= 0.80


@pytest.mark.parametrize("raw", ["", "   ", "LIVE-ish", "dry-run", None])
def test_mode_unset_empty_or_unrecognized_resolves_to_simulate(raw: str | None) -> None:
    """§17 — every unusable mode value resolves to `simulate`."""
    env = {} if raw is None else {"PIPELINE_MODE": raw}

    assert load_config(env=env).mode == Mode.SIMULATE


def test_mode_live_is_honoured_when_explicit() -> None:
    """§3 — `live` is the one recognized non-default mode, and it requires credentials."""
    live_env = {
        "PIPELINE_MODE": "live",
        "PIPELINE_GITHUB_TOKEN": "placeholder-github-token",
        "PIPELINE_DEVIN_API_KEY": "placeholder-devin-key",
    }

    assert load_config(env=live_env).mode == Mode.LIVE

    with pytest.raises(ConfigError):
        load_config(env={"PIPELINE_MODE": "live"})


def test_gsheet_sink_rejected_in_simulate() -> None:
    """§13/§17 — a remote KPI sink in SIMULATE is a configuration error."""
    with pytest.raises(ConfigError):
        load_config(env={"PIPELINE_KPI_SINK": "gsheet", "PIPELINE_MODE": "simulate"})

    with pytest.raises(ConfigError):
        PipelineConfig(kpi_sink=KpiSink.GSHEET, mode=Mode.SIMULATE)


def test_budget_above_hard_max_is_clamped_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """§17 — `budget_N > BUDGET_HARD_MAX` clamps and logs `guardrail_clamped`."""
    with caplog.at_level(logging.WARNING):
        config = load_config(cli_args=["--budget_N=99", "--i-know-what-im-doing"])

    assert config.budget_N == BUDGET_HARD_MAX
    assert any(
        ReasonCode.GUARDRAIL_CLAMPED.value in record.getMessage() for record in caplog.records
    )


def test_budget_above_hard_max_without_the_flag_is_an_error() -> None:
    with pytest.raises(ConfigError):
        load_config(cli_args=["--budget_N=99"])


def test_invalid_direct_construction_raises_config_error() -> None:
    """The interface note: `ConfigError` on every path, including direct construction."""
    with pytest.raises(ConfigError):
        PipelineConfig(iteration_cap=0)

    with pytest.raises(ConfigError):
        PipelineConfig(coverage_bar=1.5)

    with pytest.raises(ConfigError):
        PipelineConfig(tier_high_min=10, tier_medium_min=20)


def test_unrecognized_environment_setting_is_an_error() -> None:
    with pytest.raises(ConfigError):
        load_config(env={"PIPELINE_NOT_A_KNOB": "1"})


def test_cli_overrides_environment_which_overrides_file(tmp_path: Path) -> None:
    config_file = tmp_path / "pipeline.yaml"
    config_file.write_text("budget_N: 4\niteration_cap: 2\n", encoding="utf-8")

    config = load_config(
        config_file,
        env={"PIPELINE_BUDGET_N": "6"},
        cli_args=["--budget_N=8"],
    )

    assert config.budget_N == 8
    assert config.iteration_cap == 2


def test_target_repository_and_paths_are_configurable() -> None:
    """Frozen post-delta additions: target owner/repo, rubrics path, templates dir."""
    config = PipelineConfig(
        target_owner="victorciao",
        target_repo="superset",
        rubrics_path=Path("config/rubrics.yaml"),
        templates_dir=Path("templates"),
    )

    assert config.target_owner == "victorciao"
    assert config.target_repo == "superset"
    assert config.rubrics_path == Path("config/rubrics.yaml")
    assert config.templates_dir == Path("templates")


def test_secrets_never_appear_in_repr_or_str() -> None:
    """Frozen post-delta additions: both credentials are non-printing `SecretStr`s."""
    token = "ghp_notarealtoken0000000000000000000000"
    api_key = "devin_notarealkey000000000000000000000"
    config = PipelineConfig(github_token=SecretStr(token), devin_api_key=SecretStr(api_key))

    assert config.github_token is not None
    assert config.devin_api_key is not None
    assert config.github_token.get_secret_value() == token
    assert config.devin_api_key.get_secret_value() == api_key
    for rendering in (repr(config), str(config), config.model_dump_json()):
        assert token not in rendering
        assert api_key not in rendering


def test_local_ci_evidence_hard_disables_auto_merge_on_every_path() -> None:
    """§10.1 — forced `false`, not merely defaulted."""
    assert (
        PipelineConfig(
            ci_evidence_mode=CiEvidenceMode.LOCAL, auto_merge_enabled=True
        ).auto_merge_enabled
        is False
    )
    assert (
        load_config(
            env={"PIPELINE_CI_EVIDENCE_MODE": "local", "PIPELINE_AUTO_MERGE_ENABLED": "true"}
        ).auto_merge_enabled
        is False
    )
