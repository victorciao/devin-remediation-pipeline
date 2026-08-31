"""§13 configuration: defaults, resolution rules, guardrails and secret handling."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import SecretStr

from pipeline.config import (
    BUDGET_HARD_MAX,
    CiEvidenceMode,
    ConfigError,
    KpiSink,
    Mode,
    PipelineConfig,
    load_config,
)
from pipeline.schemas import Lane, ReasonCode


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


def test_budget_above_hard_max_without_the_flag_is_clamped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        config = load_config(cli_args=["--budget_N=99"])

    assert config.budget_N == BUDGET_HARD_MAX
    assert any(
        ReasonCode.GUARDRAIL_CLAMPED.value in record.getMessage() for record in caplog.records
    )


def test_unrecognized_environment_setting_is_an_error() -> None:
    with pytest.raises(ConfigError):
        load_config(env={"PIPELINE_NOT_A_KNOB": "1"})


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


def test_suite_check_context_is_environment_configurable() -> None:
    """The named Actions suite context can be configured for the target fork."""
    config = load_config(env={"PIPELINE_SUITE_CHECK_CONTEXT": "pre-commit checks"})

    assert config.suite_check_context == "pre-commit checks"
    assert (
        load_config(
            env={"PIPELINE_CI_EVIDENCE_MODE": "local", "PIPELINE_AUTO_MERGE_ENABLED": "true"}
        ).auto_merge_enabled
        is False
    )


def test_dispatch_scope_defaults_empty_and_accepts_cli_and_environment() -> None:
    config = PipelineConfig()

    assert config.suite_check_context == "unit-tests-required"
    assert config.required_contexts_min == ("pre-commit (current)",)
    assert config.only_lanes == ()
    assert load_config(cli_args=["--only-lanes=skipped_tests"]).only_lanes == (Lane.SKIPPED_TESTS,)
    assert load_config(env={"PIPELINE_ONLY_LANES": "codeql, deprecations"}).only_lanes == (
        Lane.CODEQL,
        Lane.DEPRECATIONS,
    )


def test_dispatch_scope_rejects_unknown_lanes() -> None:
    with pytest.raises(ConfigError):
        load_config(cli_args=["--only-lanes=not-a-lane"])


def test_the_removed_human_review_knob_has_no_environment_name_either() -> None:
    """§17 (l.1016) — the invariant would be undone by a surviving env override."""
    with pytest.raises(ConfigError, match="MAJOR_ONLY_REQUIRES_HUMAN"):
        load_config(env={"PIPELINE_MAJOR_ONLY_REQUIRES_HUMAN": "false"})
