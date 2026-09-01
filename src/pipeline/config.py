"""Validated configuration surface for the remediation pipeline."""

from __future__ import annotations

import logging
import os
import shlex
import sys
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import cast

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

from pipeline.schemas import Lane

logger = logging.getLogger(__name__)
BUDGET_HARD_MAX = 25
SECURITY_ISSUE_MODE = "generic_tracking"
DEFAULT_BUDGET_N = 5
DEFAULT_SESSION_TIMEOUT_S = 5400.0
DEFAULT_REQUIRED_CONTEXTS_MIN = ("pre-commit (current)",)
DEFAULT_ALERT_ANALYSIS_WAIT_S = 2700.0


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


class AlertSource(str, Enum):
    """LANE 1 alert source values."""

    CODE_SCANNING_API = "code_scanning_api"
    SARIF_FILE = "sarif_file"


class CiEvidenceMode(str, Enum):
    """CI evidence mode values."""

    ACTIONS = "actions"
    LOCAL = "local"


class PipelineConfig(BaseModel):
    """The pipeline configuration model with its shipped defaults."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)

    mode: Mode = Mode.SIMULATE
    budget_N: int = Field(default=DEFAULT_BUDGET_N, ge=1, le=BUDGET_HARD_MAX, strict=True)
    score_cap: float = Field(default=200, gt=0, strict=True)
    tier_high_min: float = Field(default=60, gt=0, strict=True)
    tier_medium_min: float = Field(default=20, gt=0, strict=True)
    eol_major_lag: int = Field(default=2, ge=1, strict=True)
    merge_rate_floor: float = Field(default=0.50, ge=0.0, le=1.0, strict=True)
    verification_pass_rate_floor: float = Field(default=0.80, ge=0.0, le=1.0, strict=True)
    session_failure_ceiling: float = Field(default=0.30, ge=0.0, le=1.0, strict=True)
    max_sessions: int = Field(default=DEFAULT_BUDGET_N + 3, ge=1, strict=True)
    session_timeout_s: float = Field(default=DEFAULT_SESSION_TIMEOUT_S, gt=0, strict=True)
    max_total_acu: float = Field(default=500.0, gt=0, strict=True)
    alert_source: AlertSource = AlertSource.CODE_SCANNING_API
    alert_fixture_path: Path = Path("fixtures/codeql_alerts.json")
    alert_analysis_wait_s: float = Field(default=DEFAULT_ALERT_ANALYSIS_WAIT_S, gt=0, strict=True)
    ci_evidence_mode: CiEvidenceMode = CiEvidenceMode.LOCAL
    suite_check_context: str = Field(default="unit-tests-required", min_length=1, strict=True)
    local_item_scope: tuple[str, ...] = ("tests/unit_tests/",)
    integration_suite_check_context: str = Field(
        default="test-postgres-required",
        min_length=1,
        strict=True,
    )
    ci_wait_timeout_s: int = Field(default=5400, gt=0, strict=True)
    required_contexts_min: tuple[str, ...] = DEFAULT_REQUIRED_CONTEXTS_MIN
    only_lanes: tuple[Lane, ...] = ()
    has_issues: bool = True
    marker_search_enabled: bool = True
    version_source: str = ".github/ISSUE_TEMPLATE/bug-report.yml"
    lane2_class_breadth_max: int = Field(default=5, ge=1, strict=True)
    target_owner: str = Field(default="victorciao", min_length=1)
    target_repo: str = Field(default="superset", min_length=1)
    github_token: SecretStr | None = None
    devin_api_key: SecretStr | None = None
    rubrics_path: Path = Path("config/rubrics.yaml")
    templates_dir: Path = Path("templates")
    session_snapshot_id: str | None = None
    pytest_command: tuple[str, ...] = (
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "-rA",
    )

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

    @field_validator(
        "alert_source",
        "ci_evidence_mode",
        mode="before",
    )
    @classmethod
    def normalize_choice(cls, value: object, info: ValidationInfo) -> object:
        """Convert source strings to strict enum values."""
        if not isinstance(value, str):
            return value
        normalized = value.strip().lower()
        if info.field_name == "alert_source":
            return AlertSource(normalized)
        return CiEvidenceMode(normalized)

    @field_validator("required_contexts_min", "local_item_scope", mode="before")
    @classmethod
    def normalize_required_contexts(cls, value: object) -> object:
        """Accept a comma-separated string or any sequence of context names."""
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)):
            return tuple(str(item) for item in value)
        return value

    @field_validator("only_lanes", mode="before")
    @classmethod
    def normalize_only_lanes(cls, value: object) -> object:
        """Accept a comma-separated lane scope or a sequence of lane values."""
        if isinstance(value, str):
            values: Sequence[object] = value.split(",")
        elif isinstance(value, (list, tuple)):
            values = value
        else:
            return value
        return tuple(
            item if isinstance(item, Lane) else Lane(str(item).strip().lower())
            for item in values
            if str(item).strip()
        )

    @field_validator("pytest_command", mode="before")
    @classmethod
    def normalize_pytest_command(cls, value: object) -> object:
        """Accept a configured target interpreter command."""
        if isinstance(value, str):
            return tuple(shlex.split(value))
        return value

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
        """Derive the session ceiling from the PR budget when callers omit it."""
        if not isinstance(value, dict) or "max_sessions" in value:
            return value
        try:
            budget = int(value.get("budget_N", DEFAULT_BUDGET_N))
        except (TypeError, ValueError):
            return value
        resolved = dict(value)
        resolved["max_sessions"] = budget + 3
        return resolved

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> PipelineConfig:
        """Re-assert cross-field safety rules on construction and assignment."""
        if self.tier_high_min <= self.tier_medium_min:
            raise ConfigError("tier_high_min must be greater than tier_medium_min")
        if self.mode == Mode.LIVE and (self.github_token is None or self.devin_api_key is None):
            raise ConfigError("mode=live requires github_token and devin_api_key")
        if self.mode == Mode.LIVE and not self.required_contexts_min:
            raise ConfigError("mode=live requires non-empty required_contexts_min")
        if self.max_sessions < self.budget_N:
            raise ConfigError(f"max_sessions={self.max_sessions} is below budget_N={self.budget_N}")
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
            "merge_rate_floor",
            "verification_pass_rate_floor",
            "session_failure_ceiling",
            "max_total_acu",
            "session_timeout_s",
            "alert_analysis_wait_s",
        }:
            values[normalized_key] = _parse_float(normalized_key, raw_value)
        elif normalized_key in {"has_issues", "marker_search_enabled"}:
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
        "BUDGET_N": "budget_N",
        "SCORE_CAP": "score_cap",
        "TIER_HIGH_MIN": "tier_high_min",
        "TIER_MEDIUM_MIN": "tier_medium_min",
        "EOL_MAJOR_LAG": "eol_major_lag",
        "MERGE_RATE_FLOOR": "merge_rate_floor",
        "VERIFICATION_PASS_RATE_FLOOR": "verification_pass_rate_floor",
        "SESSION_FAILURE_CEILING": "session_failure_ceiling",
        "HAS_ISSUES": "has_issues",
        "MARKER_SEARCH_ENABLED": "marker_search_enabled",
        "REQUIRED_CONTEXTS_MIN": "required_contexts_min",
        "ALERT_ANALYSIS_WAIT_S": "alert_analysis_wait_s",
        "SESSION_TIMEOUT_S": "session_timeout_s",
        "CI_WAIT_TIMEOUT_S": "ci_wait_timeout_s",
        "LANE2_CLASS_BREADTH_MAX": "lane2_class_breadth_max",
        "MAX_SESSIONS": "max_sessions",
        "MAX_TOTAL_ACU": "max_total_acu",
        "ALERT_SOURCE": "alert_source",
        "ALERT_FIXTURE_PATH": "alert_fixture_path",
        "CI_EVIDENCE_MODE": "ci_evidence_mode",
        "SUITE_CHECK_CONTEXT": "suite_check_context",
        "LOCAL_ITEM_SCOPE": "local_item_scope",
        "INTEGRATION_SUITE_CHECK_CONTEXT": "integration_suite_check_context",
        "ONLY_LANES": "only_lanes",
        "VERSION_SOURCE": "version_source",
        "MODE": "mode",
        "TARGET_OWNER": "target_owner",
        "TARGET_REPO": "target_repo",
        "GITHUB_TOKEN": "github_token",
        "DEVIN_API_KEY": "devin_api_key",
        "RUBRICS_PATH": "rubrics_path",
        "TEMPLATES_DIR": "templates_dir",
        "SESSION_SNAPSHOT_ID": "session_snapshot_id",
        "PYTEST_COMMAND": "pytest_command",
    }
    for key, raw_value in env.items():
        if not key.startswith("PIPELINE_"):
            continue
        name = key.removeprefix("PIPELINE_")
        field_name = field_names.get(name)
        if field_name is None:
            raise ConfigError(f"unrecognized environment setting: {key}")
        if name in {
            "BUDGET_N",
            "EOL_MAJOR_LAG",
            "CI_WAIT_TIMEOUT_S",
            "LANE2_CLASS_BREADTH_MAX",
            "MAX_SESSIONS",
        }:
            values[field_name] = _parse_int(field_name, raw_value)
        elif name in {
            "SCORE_CAP",
            "TIER_HIGH_MIN",
            "TIER_MEDIUM_MIN",
            "MERGE_RATE_FLOOR",
            "VERIFICATION_PASS_RATE_FLOOR",
            "SESSION_FAILURE_CEILING",
            "MAX_TOTAL_ACU",
            "SESSION_TIMEOUT_S",
            "ALERT_ANALYSIS_WAIT_S",
        }:
            values[field_name] = _parse_float(field_name, raw_value)
        elif name in {"HAS_ISSUES", "MARKER_SEARCH_ENABLED"}:
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
    cli_values, _ = _parse_cli(cli_args)
    values.update(cli_values)

    raw_budget = values.get("budget_N", DEFAULT_BUDGET_N)
    if isinstance(raw_budget, str):
        raw_budget = _parse_int("budget_N", raw_budget)
    if isinstance(raw_budget, int) and raw_budget > BUDGET_HARD_MAX:
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
