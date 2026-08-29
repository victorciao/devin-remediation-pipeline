"""Pure candidate scoring and rubric loading."""

from collections.abc import Mapping
from pathlib import Path
from typing import TypeAlias, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import PipelineConfig
from pipeline.schemas import Candidate, DefinitionKind, Lane

RubricDefaults: TypeAlias = dict[str, int]
RubricTables: TypeAlias = dict[Lane, RubricDefaults]


class ScoreError(ValueError):
    """Raised when a candidate cannot be scored."""


class ScoreResult(BaseModel):
    """Score and factor values used by tier dispatch."""

    model_config = ConfigDict(extra="forbid", strict=True)

    score: float = Field(ge=0)
    business_impact: int = Field(ge=1, le=5)
    verifiability: int = Field(ge=1, le=5)
    automatability: int = Field(ge=1, le=5)
    signal_quality: int = Field(ge=1, le=5)
    risk: int = Field(ge=1, le=5)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ScoreError(f"{label} must be a mapping")
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ScoreError(f"{label} keys must be strings")
        result[key] = item
    return result


def load_rubrics(path: str | Path = Path("config/rubrics.yaml")) -> RubricTables:
    """Load one default factor value for each factor in each remediation lane."""
    try:
        with Path(path).open(encoding="utf-8") as stream:
            raw = cast(object, yaml.safe_load(stream))
    except OSError as exc:
        raise ScoreError(f"cannot read rubric file: {path}") from exc
    except yaml.YAMLError as exc:
        raise ScoreError(f"invalid rubric YAML: {path}") from exc

    root = _mapping(raw, "rubrics")
    tables: RubricTables = {}
    for lane in Lane:
        lane_data = _mapping(root.get(lane.value), lane.value)
        defaults: RubricDefaults = {}
        for factor in (
            "business_impact",
            "verifiability",
            "automatability",
            "signal_quality",
            "risk",
        ):
            factor_data = _mapping(lane_data.get(factor), f"{lane.value}.{factor}")
            default = factor_data.get("default")
            if not isinstance(default, int) or not 1 <= default <= 5:
                raise ScoreError(f"{lane.value}.{factor}.default must be an integer in 1..5")
            defaults[factor] = default
        tables[lane] = defaults
    return tables


def _factor(candidate_value: int | None, defaults: Mapping[str, int], factor: str) -> int:
    value = candidate_value if candidate_value is not None else defaults[factor]
    if not 1 <= value <= 5:
        raise ScoreError(f"{factor} must be in 1..5")
    return value


def _effective_risk(candidate: Candidate, risk: int, config: PipelineConfig) -> int:
    breadth = (
        candidate.live_enclosed_tests
        if candidate.live_enclosed_tests is not None
        else candidate.enclosed_tests
    )
    if (
        candidate.lane is Lane.SKIPPED_TESTS
        and candidate.kind is DefinitionKind.CLASS
        and breadth is not None
        and 0 < breadth <= config.lane2_class_breadth_max
    ):
        return min(risk + 1, 5)
    return risk


def score_candidate(
    candidate: Candidate,
    config: PipelineConfig,
    *,
    rubrics: RubricTables | None = None,
) -> ScoreResult:
    """Score a gate-passed candidate using the configured five-factor formula."""
    if candidate.gate_passed is not True:
        raise ScoreError("only gate-passed candidates can be scored")
    tables = rubrics if rubrics is not None else load_rubrics(config.rubrics_path)
    try:
        defaults = tables[candidate.lane]
    except KeyError as exc:
        raise ScoreError(f"missing rubric for lane: {candidate.lane.value}") from exc

    business_impact = _factor(candidate.business_impact, defaults, "business_impact")
    verifiability = _factor(candidate.verifiability, defaults, "verifiability")
    automatability = _factor(candidate.automatability, defaults, "automatability")
    signal_quality = _factor(candidate.signal_quality, defaults, "signal_quality")
    risk = _effective_risk(candidate, _factor(candidate.risk, defaults, "risk"), config)
    raw_score = business_impact * verifiability * automatability * signal_quality / max(risk, 1)
    return ScoreResult(
        score=min(raw_score, config.score_cap),
        business_impact=business_impact,
        verifiability=verifiability,
        automatability=automatability,
        signal_quality=signal_quality,
        risk=risk,
    )


def apply_score(
    candidate: Candidate,
    config: PipelineConfig,
    *,
    rubrics: RubricTables | None = None,
) -> Candidate:
    """Return a candidate populated with its calculated score and factors."""
    result = score_candidate(candidate, config, rubrics=rubrics)
    return candidate.model_copy(
        update={
            "score": result.score,
            "business_impact": result.business_impact,
            "verifiability": result.verifiability,
            "automatability": result.automatability,
            "signal_quality": result.signal_quality,
            "risk": result.risk,
        }
    )


def calculate_score(
    candidate: Candidate,
    config: PipelineConfig,
    *,
    rubrics: RubricTables | None = None,
) -> float:
    """Return only the composite score for a gate-passed candidate."""
    return score_candidate(candidate, config, rubrics=rubrics).score


score = calculate_score
