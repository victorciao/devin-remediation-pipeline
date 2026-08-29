"""§4 stage-2 scoring: the composite formula, the risk floor, the cap and tier mapping."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.config import PipelineConfig
from pipeline.dispatch import tier_for_score
from pipeline.rubric import RubricTables, resolve_factors
from pipeline.schemas import DefinitionKind, Tier
from pipeline.score import ScoreError, apply_score, score_candidate
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR
from tests.factories import codeql_candidate, lane2_candidate

FACTORS = ("business_impact", "verifiability", "automatability", "signal_quality", "risk")


def config_with(**overrides: object) -> PipelineConfig:
    """A configuration pointed at the shipped rubrics with the given knobs overridden."""
    return PipelineConfig(
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        **overrides,
    )


def test_worked_example_scores_128(simulate_config: PipelineConfig) -> None:
    """§4 — `4 x 4 x 4 x 4 / max(2, 1) = 128`, which maps to the high tier."""
    candidate = codeql_candidate(gate_passed=True)
    factors = resolve_factors(candidate, simulate_config)

    result = score_candidate(candidate, simulate_config, resolved_factors=factors)

    assert (
        factors.business_impact,
        factors.verifiability,
        factors.automatability,
        factors.signal_quality,
        factors.risk,
    ) == (4, 4, 4, 4, 2)
    assert result.score == pytest.approx(128.0)
    assert tier_for_score(result.score, simulate_config) is Tier.HIGH


def test_scoring_consumes_the_resolved_factors_without_re_resolving(
    simulate_config: PipelineConfig,
    rubrics: RubricTables,
) -> None:
    """§4.1 — the score is computed from the factors resolved before gating."""
    candidate = codeql_candidate(gate_passed=True)
    factors = resolve_factors(candidate, simulate_config, rubrics).model_copy(
        update={"business_impact": 2}
    )

    result = score_candidate(candidate, simulate_config, resolved_factors=factors)

    assert result.business_impact == 2
    assert result.score == pytest.approx(64.0)


def test_scoring_ignores_preset_candidate_factor_fields(simulate_config: PipelineConfig) -> None:
    """A pre-set `business_impact` on the candidate never substitutes for resolution."""
    candidate = codeql_candidate(gate_passed=True, business_impact=1)

    result = score_candidate(candidate, simulate_config)

    assert result.business_impact == 4
    assert result.score == pytest.approx(128.0)


def test_only_gate_passed_candidates_are_scored(simulate_config: PipelineConfig) -> None:
    """§4 — GATE precedes SCORE; scoring a non-gated candidate is an error."""
    with pytest.raises(ScoreError):
        score_candidate(codeql_candidate(), simulate_config)


def test_risk_floor_of_one_leaves_the_product_unchanged(simulate_config: PipelineConfig) -> None:
    """`max(risk, 1)` means risk 1 divides by 1, not by 0."""
    candidate = codeql_candidate(
        gate_passed=True,
        security_severity_level="low",
        targeted_test_signal="weak",
        transformation_scope="constrained_transform",
        rule_precision=None,
        updated_at_fresh=None,
        blast_radius="local_module",
    )
    factors = resolve_factors(candidate, simulate_config)

    result = score_candidate(candidate, simulate_config, resolved_factors=factors)

    assert factors.risk == 1
    assert result.score == pytest.approx(
        factors.business_impact
        * factors.verifiability
        * factors.automatability
        * factors.signal_quality
    )


def test_risk_below_one_is_rejected_by_the_schema() -> None:
    """The floor's other half: the schema refuses a risk value below 1."""
    with pytest.raises(ValidationError):
        codeql_candidate(risk=0)


def test_score_cap_clamps_the_composite(simulate_config: PipelineConfig) -> None:
    """§17 — the composite is capped at `score_cap`, not merely large."""
    candidate = codeql_candidate(
        gate_passed=True,
        security_severity_level="critical",
        targeted_test_signal="targeted",
        transformation_scope="local_python_transform",
        rule_precision="precise",
        updated_at_fresh=True,
        blast_radius="local_module",
    )

    result = score_candidate(candidate, simulate_config)

    assert simulate_config.score_cap == 200
    assert result.score == pytest.approx(200.0)


def test_score_cap_knob_changes_the_clamp() -> None:
    """The cap is a knob: lowering it lowers the clamp without code edits."""
    config = config_with(score_cap=50.0)
    candidate = codeql_candidate(
        gate_passed=True,
        security_severity_level="critical",
        targeted_test_signal="targeted",
        transformation_scope="local_python_transform",
        rule_precision="precise",
        updated_at_fresh=True,
        blast_radius="local_module",
    )

    assert score_candidate(candidate, config).score == pytest.approx(50.0)


@pytest.mark.parametrize(
    ("score", "tier"),
    [
        (200.0, Tier.HIGH),
        (60.0, Tier.HIGH),
        (59.0, Tier.MEDIUM),
        (20.0, Tier.MEDIUM),
        (19.0, Tier.LOW),
        (0.0, Tier.LOW),
    ],
)
def test_tier_mapping_at_threshold_boundaries(
    simulate_config: PipelineConfig, score: float, tier: Tier
) -> None:
    """§17 — `59/60` and `19/20` are the inclusive-minimum boundaries."""
    assert tier_for_score(score, simulate_config) is tier


def test_tier_thresholds_are_knobs() -> None:
    """Retuned thresholds move the boundaries with no code change."""
    config = config_with(tier_high_min=100.0, tier_medium_min=50.0)

    assert tier_for_score(99.0, config) is Tier.MEDIUM
    assert tier_for_score(100.0, config) is Tier.HIGH
    assert tier_for_score(49.0, config) is Tier.LOW


def test_codeql_business_impact_follows_security_severity(
    simulate_config: PipelineConfig,
) -> None:
    """§4.1 anchor — CodeQL `business_impact` runs critical 5 ... note 1."""
    critical = resolve_factors(
        codeql_candidate(security_severity_level="critical"), simulate_config
    )
    note = resolve_factors(codeql_candidate(security_severity_level="note"), simulate_config)

    assert critical.business_impact == 5
    assert note.business_impact == 1


def test_lane2_class_rows_take_the_extra_risk_point(simulate_config: PipelineConfig) -> None:
    """§4.1 — a `kind = class` LANE 2 row scores +1 risk, lowering its score."""
    function_row = lane2_candidate(gate_passed=True, kind=DefinitionKind.FUNCTION)
    class_row = lane2_candidate(
        gate_passed=True,
        kind=DefinitionKind.CLASS,
        enclosed_tests=1,
        transformation_scope="bounded_class",
    )

    function_result = score_candidate(function_row, simulate_config)
    class_result = score_candidate(class_row, simulate_config)

    assert class_result.risk == function_result.risk + 1
    assert class_result.score < function_result.score


def test_lane2_class_risk_adjustment_lives_only_in_scoring(
    simulate_config: PipelineConfig,
) -> None:
    """The +1 class risk point is a scoring adjustment; resolution reports the rubric row."""
    class_row = lane2_candidate(
        gate_passed=True,
        kind=DefinitionKind.CLASS,
        enclosed_tests=1,
        transformation_scope="bounded_class",
    )

    factors = resolve_factors(class_row, simulate_config)
    result = score_candidate(class_row, simulate_config, resolved_factors=factors)

    assert result.risk == factors.risk + 1


def test_apply_score_records_the_score_and_its_evidence(simulate_config: PipelineConfig) -> None:
    """The scored candidate carries its factors and rubric rows for the §12 event record."""
    candidate = codeql_candidate(gate_passed=True)

    scored = apply_score(candidate, simulate_config)

    assert scored.score == pytest.approx(128.0)
    assert all(getattr(scored, factor) is not None for factor in FACTORS)
    assert set(scored.factor_rows) == set(FACTORS)
