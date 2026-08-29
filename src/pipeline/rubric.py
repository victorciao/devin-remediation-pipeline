"""Shared rubric loading and observable factor resolution."""

import logging
from pathlib import Path
from typing import TypeAlias, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import PipelineConfig
from pipeline.schemas import Candidate, DefinitionKind, Lane, ReasonCode

logger = logging.getLogger(__name__)


class RubricError(ValueError):
    """Raised when rubric data or an observable cannot be resolved."""

    reason = ReasonCode.RUBRIC_FACTOR_UNRESOLVED

    def __init__(self, message: str) -> None:
        logger.error(
            "rubric_factor_unresolved",
            extra={"reason": self.reason.value, "detail": message},
        )
        super().__init__(message)


class RubricFactor(BaseModel):
    """One observable-to-value rubric table."""

    model_config = ConfigDict(extra="forbid", strict=True)

    observable: str = Field(min_length=1)
    definition: str | None = None
    rows: dict[str, int]
    default: int = Field(ge=1, le=5)


RubricTables: TypeAlias = dict[Lane, dict[str, RubricFactor]]


class ResolvedFactors(BaseModel):
    """Factor values and rubric rows resolved before gate evaluation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    business_impact: int = Field(ge=1, le=5)
    verifiability: int = Field(ge=1, le=5)
    automatability: int = Field(ge=1, le=5)
    signal_quality: int = Field(ge=1, le=5)
    risk: int = Field(ge=1, le=5)
    factor_rows: dict[str, str] = Field(default_factory=dict)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RubricError(f"{label} must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise RubricError(f"{label} keys must be strings")
        result[key] = item
    return result


def _int_rows(value: object, label: str) -> dict[str, int]:
    raw_rows = _mapping(value, label)
    rows: dict[str, int] = {}
    for key, item in raw_rows.items():
        if not isinstance(item, int) or not 1 <= item <= 5:
            raise RubricError(f"{label}.{key} must be an integer in 1..5")
        rows[key] = item
    return rows


def load_rubrics(path: str | Path = Path("config/rubrics.yaml")) -> RubricTables:
    """Load observable-to-value tables for every lane and scoring factor."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            raw = cast(object, yaml.safe_load(stream))
    except OSError as exc:
        raise RubricError(f"cannot read rubric file: {path}") from exc
    except yaml.YAMLError as exc:
        raise RubricError(f"invalid rubric YAML: {path}") from exc

    root = _mapping(raw, "rubrics")
    tables: RubricTables = {}
    for lane in Lane:
        lane_data = _mapping(root.get(lane.value), lane.value)
        factors: dict[str, RubricFactor] = {}
        for factor_name in (
            "business_impact",
            "verifiability",
            "automatability",
            "signal_quality",
            "risk",
        ):
            factor_data = _mapping(lane_data.get(factor_name), f"{lane.value}.{factor_name}")
            observable = factor_data.get("observable")
            default = factor_data.get("default")
            if not isinstance(observable, str) or not observable:
                raise RubricError(f"{lane.value}.{factor_name}.observable is required")
            if not isinstance(default, int) or not 1 <= default <= 5:
                raise RubricError(f"{lane.value}.{factor_name}.default must be an integer in 1..5")
            definition = factor_data.get("definition")
            factors[factor_name] = RubricFactor(
                observable=observable,
                definition=definition if isinstance(definition, str) else None,
                rows=_int_rows(factor_data.get("rows"), f"{lane.value}.{factor_name}.rows"),
                default=default,
            )
        tables[lane] = factors
    return tables


def _row_value(rubric: RubricFactor, row: str | None, factor_name: str) -> tuple[int, str]:
    if row is not None:
        value = rubric.rows.get(row)
        if value is None:
            raise RubricError(f"unknown {factor_name} rubric row: {row}")
        return value, row
    return rubric.default, "default"


def _severity_row(candidate: Candidate) -> str | None:
    if candidate.security_severity_level is None:
        return None
    return candidate.security_severity_level.strip().lower()


def _codeql_signal_row(candidate: Candidate) -> str | None:
    if candidate.rule_precision is None and candidate.updated_at_fresh is None:
        return None
    precision = (
        candidate.rule_precision.strip().lower() if candidate.rule_precision is not None else None
    )
    if precision in {"precise", "high"} and candidate.updated_at_fresh is True:
        return "precise_fresh"
    if precision in {"precise", "high"}:
        return "precise"
    if candidate.updated_at_fresh is True:
        return "current"
    return "stale" if candidate.updated_at is not None else "weak"


def _lane2_breadth_row(candidate: Candidate) -> str | None:
    breadth = (
        candidate.live_enclosed_tests
        if candidate.live_enclosed_tests is not None
        else candidate.enclosed_tests
    )
    if breadth is None:
        return None
    if breadth <= 1:
        return "narrow"
    if breadth <= 5:
        return "local"
    if breadth <= 20:
        return "shared"
    return "broad"


def _lane2_signal_row(candidate: Candidate) -> str | None:
    if candidate.skip_reason is None:
        return None
    reason = candidate.skip_reason.strip()
    if not reason:
        return "unknown"
    if reason.lower().startswith("todo:") and len(reason[5:].strip()) > 0:
        return "todo_with_cause"
    if reason.lower() in {"skip", "skipped", "no reason", "unknown"}:
        return "bare_skip"
    return "explanatory"


def _lane2_automatability_row(candidate: Candidate) -> str | None:
    if candidate.transformation_scope is not None:
        return candidate.transformation_scope.strip().lower()
    if candidate.kind is DefinitionKind.FUNCTION:
        return "single_test"
    if candidate.kind is DefinitionKind.CLASS and candidate.enclosed_tests is not None:
        return "bounded_class" if candidate.enclosed_tests <= 5 else "broad_class"
    return None


def _lane2_risk_row(candidate: Candidate) -> str | None:
    if candidate.scope_is_test_only is not True:
        return None
    return "test_only_local" if candidate.enclosed_tests in {None, 1} else "test_only_shared"


def _lane3_business_row(candidate: Candidate) -> str | None:
    if candidate.public_api_surface is None:
        return None
    return "public_api" if candidate.public_api_surface else "internal_api"


def _lane3_signal_row(candidate: Candidate) -> str | None:
    if candidate.current_major is None or candidate.deprecated_in is None:
        return None
    try:
        deprecated_major = int(candidate.deprecated_in.split(".", 1)[0])
    except ValueError as exc:
        raise RubricError("deprecated_in must begin with a major version") from exc
    age = candidate.current_major - deprecated_major
    if age >= 3:
        return "three_or_more"
    if age == 2:
        return "two"
    if age == 1:
        return "one"
    if age == 0:
        return "zero"
    return "unknown"


def _lane3_risk_row(candidate: Candidate) -> str | None:
    if candidate.override_surface is True:
        return "override_surface"
    if candidate.caller_count is None and candidate.override_count is None:
        if candidate.internal_caller is None:
            return None
        return "shared_callers" if candidate.internal_caller else "no_callers"
    count = max(candidate.caller_count or 0, candidate.override_count or 0)
    if count == 0:
        return "no_callers"
    if count <= 2:
        return "bounded_callers"
    if count <= 10:
        return "shared_callers"
    return "broad_callers"


def _row_for(candidate: Candidate, factor_name: str) -> str | None:
    if candidate.lane is Lane.CODEQL:
        return {
            "business_impact": _severity_row(candidate),
            "verifiability": candidate.targeted_test_signal,
            "automatability": candidate.transformation_scope,
            "signal_quality": _codeql_signal_row(candidate),
            "risk": candidate.blast_radius,
        }.get(factor_name)
    if candidate.lane is Lane.SKIPPED_TESTS:
        return {
            "business_impact": _lane2_breadth_row(candidate),
            "verifiability": candidate.targeted_test_signal,
            "automatability": _lane2_automatability_row(candidate),
            "signal_quality": _lane2_signal_row(candidate),
            "risk": _lane2_risk_row(candidate),
        }.get(factor_name)
    return {
        "business_impact": _lane3_business_row(candidate),
        "verifiability": candidate.targeted_test_signal,
        "automatability": candidate.transformation_scope,
        "signal_quality": _lane3_signal_row(candidate),
        "risk": _lane3_risk_row(candidate),
    }.get(factor_name)


def resolve_factors(
    candidate: Candidate,
    config: PipelineConfig,
    rubrics: RubricTables | None = None,
) -> ResolvedFactors:
    """Resolve every score factor once from candidate observables and rubric tables."""
    tables = rubrics if rubrics is not None else load_rubrics(config.rubrics_path)
    try:
        factors = tables[candidate.lane]
    except KeyError as exc:
        raise RubricError(f"missing rubric for lane: {candidate.lane.value}") from exc

    values: dict[str, int] = {}
    rows: dict[str, str] = {}
    for factor_name in (
        "business_impact",
        "verifiability",
        "automatability",
        "signal_quality",
        "risk",
    ):
        value, row = _row_value(
            factors[factor_name],
            _row_for(candidate, factor_name),
            factor_name,
        )
        values[factor_name] = value
        rows[factor_name] = row
    return ResolvedFactors(
        business_impact=values["business_impact"],
        verifiability=values["verifiability"],
        automatability=values["automatability"],
        signal_quality=values["signal_quality"],
        risk=values["risk"],
        factor_rows=rows,
    )
