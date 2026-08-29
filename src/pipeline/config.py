"""Validated configuration surface for the remediation pipeline."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)
BUDGET_HARD_MAX = 25
SECURITY_ISSUE_MODE = "generic_tracking"


class ConfigError(ValueError):
    """Raised when startup configuration cannot safely be constructed."""


def _parse_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _parse_float(name: str, value: str) -> float:
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc


class Mode(str, Enum):
    """Execution mode values."""

    SIMULATE = "simulate"
    LIVE = "live"


class KpiSink(str, Enum):
    """KPI sink values."""

    LOCAL = "local"
    GSHEET = "gsheet"


class AlertSource(str, Enum):
    """LANE 1 alert source values."""

    API = "api"
    SARIF_FILE = "sarif_file"


class CiEvidenceMode(str, Enum):
    """CI evidence mode values."""

    GITHUB = "github"
    LOCAL = "local"


class IssueSink(str, Enum):
    """Manager-facing artifact sink values."""

    ISSUES = "issues"
    PR_COMMENT = "pr_comment"


class PipelineConfig(BaseModel):
    """The 19 configurable §13 knobs with their shipped defaults."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    mode: Mode = Mode.SIMULATE
    iteration_cap: int = Field(default=5, ge=1, le=10, strict=True)
    coverage_bar: float = Field(default=0.80, ge=0.0, le=1.0, strict=True)
    budget_N: int = Field(default=10, ge=1, le=BUDGET_HARD_MAX, strict=True)
    score_cap: float = Field(default=200, gt=0, strict=True)
    tier_high_min: float = Field(default=60, gt=0, strict=True)
    tier_medium_min: float = Field(default=20, gt=0, strict=True)
    eol_major_lag: int = Field(default=2, ge=1, strict=True)
    merge_rate_floor: float = Field(default=0.50, ge=0.0, le=1.0, strict=True)
    session_failure_ceiling: float = Field(default=0.30, ge=0.0, le=1.0, strict=True)
    kpi_sink: KpiSink = KpiSink.LOCAL
    major_only_requires_human: bool = True
    alert_source: AlertSource = AlertSource.API
    ci_evidence_mode: CiEvidenceMode = CiEvidenceMode.LOCAL
    ci_wait_timeout_s: int = Field(default=5400, gt=0, strict=True)
    auto_merge_enabled: bool = False
    issue_sink: IssueSink = IssueSink.ISSUES
    version_source: str = ".github/ISSUE_TEMPLATE/bug-report.yml"
    lane2_class_breadth_max: int = Field(default=5, ge=1, strict=True)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> Mode:
        """Resolve empty and unknown modes to simulate and log the resolution."""
        if not isinstance(value, str):
            logger.warning("mode resolved to simulate: value is missing or invalid")
            return Mode.SIMULATE
        normalized = value.strip().lower()
        if normalized not in (Mode.SIMULATE, Mode.LIVE):
            logger.warning("mode resolved to simulate: unrecognized value")
            return Mode.SIMULATE
        return Mode(normalized)

    @field_validator("kpi_sink", "alert_source", "ci_evidence_mode", "issue_sink", mode="before")
    @classmethod
    def normalize_choice(cls, value: object, info: ValidationInfo) -> object:
        """Convert source strings to strict enum values."""
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if info.field_name == "kpi_sink":
            return KpiSink(normalized)
        if info.field_name == "alert_source":
            return AlertSource(normalized)
        if info.field_name == "ci_evidence_mode":
            return CiEvidenceMode(normalized)
        return IssueSink(normalized)

    @field_validator("auto_merge_enabled", mode="after")
    @classmethod
    def force_local_auto_merge_off(cls, value: bool, info: ValidationInfo) -> bool:
        """Local evidence always disables auto-merge."""
        data = cast(dict[str, object], info.data)
        if data.get("ci_evidence_mode") == CiEvidenceMode.LOCAL:
            return False
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> PipelineConfig:
        """Reject unsafe sink combinations and invalid threshold ordering."""
        if self.kpi_sink == KpiSink.GSHEET and self.mode == Mode.SIMULATE:
            raise ConfigError("kpi_sink=gsheet is invalid while mode=simulate")
        if self.tier_high_min <= self.tier_medium_min:
            raise ConfigError("tier_high_min must be greater than tier_medium_min")
        return self


def _yaml_mapping(value: object) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("configuration file must contain a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ConfigError("configuration keys must be strings")
        result[key] = item
    return result


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"invalid boolean value: {value}")


def _parse_cli(args: Sequence[str]) -> tuple[dict[str, object], bool]:
    values: dict[str, object] = {}
    explicit_budget_flag = False
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--i-know-what-im-doing":
            explicit_budget_flag = True
            index += 1
            continue
        if not token.startswith("--"):
            raise ConfigError(f"unrecognized CLI argument: {token}")
        item = token[2:]
        if "=" in item:
            key, raw_value = item.split("=", 1)
        else:
            key = item
            index += 1
            if index >= len(args):
                raise ConfigError(f"missing value for --{key}")
            raw_value = args[index]
        normalized_key = key.replace("-", "_")
        if normalized_key in {
            "iteration_cap",
            "budget_N",
            "score_cap",
            "tier_high_min",
            "tier_medium_min",
            "eol_major_lag",
            "ci_wait_timeout_s",
            "lane2_class_breadth_max",
        }:
            values[normalized_key] = _parse_int(normalized_key, raw_value)
        elif normalized_key in {
            "coverage_bar",
            "merge_rate_floor",
            "session_failure_ceiling",
        }:
            values[normalized_key] = _parse_float(normalized_key, raw_value)
        elif normalized_key in {"major_only_requires_human", "auto_merge_enabled"}:
            values[normalized_key] = _parse_bool(raw_value)
        else:
            values[normalized_key] = raw_value
        index += 1
    return values, explicit_budget_flag


def _env_values(env: Mapping[str, str]) -> dict[str, object]:
    values: dict[str, object] = {}
    field_names = {
        "ITERATION_CAP": "iteration_cap",
        "COVERAGE_BAR": "coverage_bar",
        "BUDGET_N": "budget_N",
        "SCORE_CAP": "score_cap",
        "TIER_HIGH_MIN": "tier_high_min",
        "TIER_MEDIUM_MIN": "tier_medium_min",
        "EOL_MAJOR_LAG": "eol_major_lag",
        "MERGE_RATE_FLOOR": "merge_rate_floor",
        "SESSION_FAILURE_CEILING": "session_failure_ceiling",
        "MAJOR_ONLY_REQUIRES_HUMAN": "major_only_requires_human",
        "AUTO_MERGE_ENABLED": "auto_merge_enabled",
        "CI_WAIT_TIMEOUT_S": "ci_wait_timeout_s",
        "LANE2_CLASS_BREADTH_MAX": "lane2_class_breadth_max",
        "KPI_SINK": "kpi_sink",
        "ALERT_SOURCE": "alert_source",
        "CI_EVIDENCE_MODE": "ci_evidence_mode",
        "ISSUE_SINK": "issue_sink",
        "VERSION_SOURCE": "version_source",
        "MODE": "mode",
    }
    for key, raw_value in env.items():
        if not key.startswith("PIPELINE_"):
            continue
        name = key.removeprefix("PIPELINE_")
        field_name = field_names.get(name)
        if field_name is None:
            raise ConfigError(f"unrecognized environment setting: {key}")
        if name in {
            "ITERATION_CAP",
            "BUDGET_N",
            "EOL_MAJOR_LAG",
            "CI_WAIT_TIMEOUT_S",
            "LANE2_CLASS_BREADTH_MAX",
        }:
            values[field_name] = _parse_int(field_name, raw_value)
        elif name in {
            "COVERAGE_BAR",
            "SCORE_CAP",
            "TIER_HIGH_MIN",
            "TIER_MEDIUM_MIN",
            "MERGE_RATE_FLOOR",
            "SESSION_FAILURE_CEILING",
        }:
            values[field_name] = _parse_float(field_name, raw_value)
        elif name in {"MAJOR_ONLY_REQUIRES_HUMAN", "AUTO_MERGE_ENABLED"}:
            values[field_name] = _parse_bool(raw_value)
        else:
            values[field_name] = raw_value
    return values


def _load_file(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as stream:
            parsed = cast(object, yaml.safe_load(stream))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML configuration: {path}") from exc
    return _yaml_mapping(parsed)


def load_config(
    config_file: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    cli_args: Sequence[str] = (),
) -> PipelineConfig:
    """Load file, environment, and CLI configuration in increasing precedence."""
    values: dict[str, object] = {}
    if config_file is not None:
        values.update(_load_file(Path(config_file)))
    values.update(_env_values(env if env is not None else os.environ))
    cli_values, explicit_budget_flag = _parse_cli(cli_args)
    values.update(cli_values)

    raw_budget = values.get("budget_N", 10)
    if isinstance(raw_budget, str):
        raw_budget = _parse_int("budget_N", raw_budget)
    if isinstance(raw_budget, int) and raw_budget > BUDGET_HARD_MAX:
        if not explicit_budget_flag:
            raise ConfigError("budget_N above BUDGET_HARD_MAX requires --i-know-what-im-doing")
        logger.warning(
            "guardrail_clamped",
            extra={"reason": "guardrail_clamped", "budget_N": raw_budget},
        )
        values["budget_N"] = BUDGET_HARD_MAX

    try:
        return PipelineConfig.model_validate(values)
    except (ValidationError, ConfigError) as exc:
        if isinstance(exc, ConfigError):
            raise
        raise ConfigError(str(exc)) from exc


Config = PipelineConfig
