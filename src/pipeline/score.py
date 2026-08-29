"""Pure candidate scoring using shared observable rubric resolution."""

from pydantic import Field

from pipeline.config import PipelineConfig
from pipeline.rubric import (
    ResolvedFactors,
    RubricError,
    RubricFactor,
    RubricTables,
    load_rubrics,
    resolve_factors,
)
from pipeline.schemas import Candidate, DefinitionKind, Lane

ScoreError = RubricError


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


class ScoreResult(ResolvedFactors):
    """Score, factors, and rubric rows used by tier dispatch."""

    score: float = Field(ge=0)


def score_candidate(
    candidate: Candidate,
    config: PipelineConfig,
    rubrics: RubricTables | None = None,
    resolved_factors: ResolvedFactors | None = None,
) -> ScoreResult:
    """Score a gate-passed candidate using the factors resolved before gating."""
    if candidate.gate_passed is not True:
        raise ScoreError("only gate-passed candidates can be scored")
    factors = (
        resolved_factors
        if resolved_factors is not None
        else resolve_factors(candidate, config, rubrics)
    )
    risk = _effective_risk(candidate, factors.risk, config)
    raw_score = (
        factors.business_impact
        * factors.verifiability
        * factors.automatability
        * factors.signal_quality
        / max(risk, 1)
    )
    return ScoreResult(
        score=min(raw_score, config.score_cap),
        business_impact=factors.business_impact,
        verifiability=factors.verifiability,
        automatability=factors.automatability,
        signal_quality=factors.signal_quality,
        risk=risk,
        factor_rows=factors.factor_rows,
    )


def apply_score(
    candidate: Candidate,
    config: PipelineConfig,
    rubrics: RubricTables | None = None,
    resolved_factors: ResolvedFactors | None = None,
) -> Candidate:
    """Return a candidate populated with its calculated score and evidence."""
    result = score_candidate(candidate, config, rubrics, resolved_factors)
    return candidate.model_copy(
        update={
            "score": result.score,
            "business_impact": result.business_impact,
            "verifiability": result.verifiability,
            "automatability": result.automatability,
            "signal_quality": result.signal_quality,
            "risk": result.risk,
            "factor_rows": result.factor_rows,
        }
    )


__all__ = [
    "RubricFactor",
    "RubricTables",
    "ScoreError",
    "ScoreResult",
    "apply_score",
    "load_rubrics",
    "resolve_factors",
    "score_candidate",
]
