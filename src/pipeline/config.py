"""Validated configuration surface for the remediation pipeline."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    ValidatorFunctionWrapHandler,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)
BUDGET_HARD_MAX = 25
SECURITY_ISSUE_MODE = "generic_tracking"
DEFAULT_BUDGET_N = 10
DEFAULT_ITERATION_CAP = 5
DEFAULT_MAX_SESSIONS = DEFAULT_BUDGET_N * (3 + 2 * DEFAULT_ITERATION_CAP)


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
    """The §13 configuration surface with its shipped defaults."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    mode: Mode = Mode.SIMULATE
    iteration_cap: int = Field(default=DEFAULT_ITERATION_CAP, ge=1, le=10, strict=True)
    coverage_bar: float = Field(default=0.80, ge=0.0, le=1.0, strict=True)
    budget_N: int = Field(default=DEFAULT_BUDGET_N, ge=1, le=BUDGET_HARD_MAX, strict=True)
    score_cap: float = Field(default=200, gt=0, strict=True)
    tier_high_min: float = Field(default=60, gt=0, strict=True)
    tier_medium_min: float = Field(default=20, gt=0, strict=True)
    eol_major_lag: int = Field(default=2, ge=1, strict=True)
    merge_rate_floor: float = Field(default=0.50, ge=0.0, le=1.0, strict=True)
    session_failure_ceiling: float = Field(default=0.30, ge=0.0, le=1.0, strict=True)
    max_sessions: int = Field(default=DEFAULT_MAX_SESSIONS, ge=1, strict=True)
    max_total_acu: float = Field(default=500.0, gt=0, strict=True)
    kpi_sink: KpiSink = KpiSink.LOCAL
    major_only_requires_human: bool = True
    alert_source: AlertSource = AlertSource.API
    alert_fixture_path: Path = Path("fixtures/codeql_alerts.json")
    ci_evidence_mode: CiEvidenceMode = CiEvidenceMode.LOCAL
    ci_wait_timeout_s: int = Field(default=5400, gt=0, strict=True)
    auto_merge_enabled: bool = False
    has_issues: bool = True
    issue_sink: IssueSink = IssueSink.ISSUES
    version_source: str = ".github/ISSUE_TEMPLATE/bug-report.yml"
    lane2_class_breadth_max: int = Field(default=5, ge=1, strict=True)
    target_owner: str = Field(default="victorciao", min_length=1)
    target_repo: str = Field(default="superset", min_length=1)
    github_token: SecretStr | None = None
    devin_api_key: SecretStr | None = None
    rubrics_path: Path = Path("config/rubrics.yaml")
    templates_dir: Path = Path("templates")
    role_session_snapshot_id: str | None = None

    def __init__(self, **data: object) -> None:
        """Construct configuration while exposing validation failures consistently."""
        try:
            super().__init__(**data)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc

    def __setattr__(self, name: str, value: object) -> None:
        """Apply assignment validation while preserving the public error type."""
        try:
            super().__setattr__(name, value)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc

    def model_copy(
        self,
        *,
        update: Mapping[str, Any] | None = None,
        deep: bool = False,
    ) -> PipelineConfig:
        """Preserve the local-evidence auto-merge invariant on copies."""
        copied = super().model_copy(update=update, deep=deep)
        if copied.ci_evidence_mode is CiEvidenceMode.LOCAL:
            object.__setattr__(copied, "auto_merge_enabled", False)
        return copied

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

    @field_validator("github_token", "devin_api_key", mode="before")
    @classmethod
    def normalize_secret(cls, value: object) -> SecretStr | None:
        """Wrap environment-provided credentials without exposing their values."""
        if value is None or isinstance(value, SecretStr):
            return value
        if isinstance(value, str):
            return SecretStr(value)
        raise TypeError("credential must be a string")

    @field_validator("rubrics_path", "templates_dir", "alert_fixture_path", mode="before")
    @classmethod
    def normalize_path(cls, value: object) -> object:
        """Accept path strings from file and environment sources."""
        if isinstance(value, str):
            return Path(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def default_max_sessions(cls, value: object) -> object:
        """Derive the session ceiling when callers omit it."""
        if not isinstance(value, dict) or "max_sessions" in value:
            return value
        raw_budget = value.get("budget_N", DEFAULT_BUDGET_N)
        raw_iteration_cap = value.get("iteration_cap", DEFAULT_ITERATION_CAP)
        try:
            budget = int(raw_budget)
            iteration_cap = int(raw_iteration_cap)
        except (TypeError, ValueError):
            return value
        resolved = dict(value)
        resolved["max_sessions"] = budget * (3 + 2 * iteration_cap)
        return resolved

    @model_validator(mode="before")
    @classmethod
    def disable_local_auto_merge(cls, value: object) -> object:
        """Make local CI evidence permanently ineligible for auto-merge."""
        if not isinstance(value, dict):
            return value
        if value.get("ci_evidence_mode") in {CiEvidenceMode.LOCAL, CiEvidenceMode.LOCAL.value}:
            resolved = dict(value)
            resolved["auto_merge_enabled"] = False
            return resolved
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> PipelineConfig:
        """Re-assert cross-field safety rules on construction and assignment."""
        if self.kpi_sink == KpiSink.GSHEET and self.mode == Mode.SIMULATE:
            raise ConfigError("kpi_sink=gsheet is invalid while mode=simulate")
        if self.tier_high_min <= self.tier_medium_min:
            raise ConfigError("tier_high_min must be greater than tier_medium_min")
        if self.mode == Mode.LIVE and (self.github_token is None or self.devin_api_key is None):
            raise ConfigError("mode=live requires github_token and devin_api_key")
        required_floor = self.budget_N * (3 + 2 * self.iteration_cap)
        if self.max_sessions < required_floor:
            raise ConfigError(
                f"max_sessions={self.max_sessions} is below required floor {required_floor}"
            )
        return self

    @model_validator(mode="wrap")
    @classmethod
    def normalize_validation_errors(
        cls,
        value: object,
        handler: ValidatorFunctionWrapHandler,
    ) -> PipelineConfig:
        """Expose every invalid configuration path as ConfigError."""
        try:
            result: object = handler(value)
        except ValidationError as exc:
            raise ConfigError(str(exc)) from exc
        if not isinstance(result, PipelineConfig):
            raise ConfigError("configuration validator returned an invalid model")
        return result


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
            "max_sessions",
        }:
            values[normalized_key] = _parse_int(normalized_key, raw_value)
        elif normalized_key in {
            "coverage_bar",
            "merge_rate_floor",
            "session_failure_ceiling",
            "max_total_acu",
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
    if "DEVIN_API_KEY" in env:
        values["devin_api_key"] = SecretStr(env["DEVIN_API_KEY"])
    if "GITHUB_PAT_REMEDIATION" in env:
        values["github_token"] = SecretStr(env["GITHUB_PAT_REMEDIATION"])
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
        "HAS_ISSUES": "has_issues",
        "CI_WAIT_TIMEOUT_S": "ci_wait_timeout_s",
        "LANE2_CLASS_BREADTH_MAX": "lane2_class_breadth_max",
        "MAX_SESSIONS": "max_sessions",
        "MAX_TOTAL_ACU": "max_total_acu",
        "KPI_SINK": "kpi_sink",
        "ALERT_SOURCE": "alert_source",
        "ALERT_FIXTURE_PATH": "alert_fixture_path",
        "CI_EVIDENCE_MODE": "ci_evidence_mode",
        "ISSUE_SINK": "issue_sink",
        "VERSION_SOURCE": "version_source",
        "MODE": "mode",
        "TARGET_OWNER": "target_owner",
        "TARGET_REPO": "target_repo",
        "GITHUB_TOKEN": "github_token",
        "DEVIN_API_KEY": "devin_api_key",
        "RUBRICS_PATH": "rubrics_path",
        "TEMPLATES_DIR": "templates_dir",
        "ROLE_SESSION_SNAPSHOT_ID": "role_session_snapshot_id",
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
            "MAX_SESSIONS",
        }:
            values[field_name] = _parse_int(field_name, raw_value)
        elif name in {
            "COVERAGE_BAR",
            "SCORE_CAP",
            "TIER_HIGH_MIN",
            "TIER_MEDIUM_MIN",
            "MERGE_RATE_FLOOR",
            "SESSION_FAILURE_CEILING",
            "MAX_TOTAL_ACU",
        }:
            values[field_name] = _parse_float(field_name, raw_value)
        elif name in {"MAJOR_ONLY_REQUIRES_HUMAN", "AUTO_MERGE_ENABLED", "HAS_ISSUES"}:
            values[field_name] = _parse_bool(raw_value)
        else:
            values[field_name] = (
                SecretStr(raw_value)
                if field_name in {"github_token", "devin_api_key"}
                else raw_value
            )
    return values


def _load_file(path: Path) -> dict[str, object]:
    try:
        with path.open(encoding="utf-8") as stream:
            parsed = cast(object, yaml.safe_load(stream))
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML configuration: {path}") from exc
    values = _yaml_mapping(parsed)
    for key in values:
        if key.lower() in {"github_token", "devin_api_key"}:
            raise ConfigError(f"credentials are environment-only: {key}")
    return values


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
