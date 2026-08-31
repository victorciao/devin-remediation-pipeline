"""§4.1 rubric ownership: observables resolve to rows once, before gating and scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import PipelineConfig
from pipeline.gate import evaluate_gates
from pipeline.rubric import RubricError, RubricTables, load_rubrics, resolve_factors
from pipeline.schemas import DefinitionKind, Lane, ReasonCode
from tests.conftest import RUBRICS_PATH
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate

FACTOR_NAMES = (
    "business_impact",
    "verifiability",
    "automatability",
    "signal_quality",
    "risk",
)


def test_rubrics_cover_every_lane_and_factor(rubrics: RubricTables) -> None:
    """§4.1 — `config/rubrics.yaml` defines all five factors for all three lanes."""
    assert set(rubrics) == set(Lane)
    for lane in Lane:
        assert set(rubrics[lane]) == set(FACTOR_NAMES)


def test_rubrics_normalize_to_the_one_to_five_scale(rubrics: RubricTables) -> None:
    """Every rubric row and default sits on the 1..5 scale."""
    for lane in Lane:
        for factor_name in FACTOR_NAMES:
            factor = rubrics[lane][factor_name]
            assert factor.observable
            assert 1 <= factor.default <= 5
            assert all(1 <= value <= 5 for value in factor.rows.values())


def test_resolve_factors_records_the_row_it_used(simulate_config: PipelineConfig) -> None:
    """Resolution is auditable: each factor reports the rubric row that produced it."""
    candidate = codeql_candidate(security_severity_level="critical")

    factors = resolve_factors(candidate, simulate_config)

    assert factors.business_impact == 5
    assert factors.factor_rows["business_impact"] == "critical"
    assert set(factors.factor_rows) == set(FACTOR_NAMES)


def test_resolve_factors_falls_back_to_the_rubric_default(simulate_config: PipelineConfig) -> None:
    """An absent observable resolves to the documented default, recorded as `default`."""
    candidate = codeql_candidate(security_severity_level=None)

    factors = resolve_factors(candidate, simulate_config)

    assert factors.business_impact == 3
    assert factors.factor_rows["business_impact"] == "default"


def test_unresolvable_observable_raises_rubric_error(simulate_config: PipelineConfig) -> None:
    """An observable with no rubric row is a `RubricError`, never a silent default."""
    candidate = codeql_candidate(security_severity_level="catastrophic")

    with pytest.raises(RubricError) as excinfo:
        resolve_factors(candidate, simulate_config)

    assert excinfo.value.reason is ReasonCode.RUBRIC_FACTOR_UNRESOLVED


def test_rubric_error_reason_code_is_rubric_factor_unresolved() -> None:
    """The reason code exists with the frozen string value."""
    assert ReasonCode.RUBRIC_FACTOR_UNRESOLVED.value == "rubric_factor_unresolved"


def test_resolve_factors_uses_the_lane_specific_table(simulate_config: PipelineConfig) -> None:
    """The same observable name resolves through the candidate's own lane table."""
    lane1 = resolve_factors(codeql_candidate(blast_radius="local_module"), simulate_config)
    lane3 = resolve_factors(lane3_candidate(caller_count=0), simulate_config)

    assert lane1.risk == 1
    assert lane3.risk == 1
    assert lane3.factor_rows["risk"] == "no_callers"


def test_lane2_breadth_resolves_business_impact(simulate_config: PipelineConfig) -> None:
    """LANE 2 business impact comes from the covered-surface breadth observable."""
    narrow = resolve_factors(lane2_candidate(enclosed_tests=1), simulate_config)
    shared = resolve_factors(
        lane2_candidate(
            kind=DefinitionKind.CLASS,
            enclosed_tests=12,
            transformation_scope="bounded_class",
        ),
        simulate_config,
    )

    assert narrow.factor_rows["business_impact"] == "narrow"
    assert shared.factor_rows["business_impact"] == "shared"
    assert shared.business_impact > narrow.business_impact


def test_live_breadth_wins_over_the_ast_bound(simulate_config: PipelineConfig) -> None:
    """§8.2 — a live collection count supersedes the AST lower bound during resolution."""
    candidate = lane2_candidate(
        kind=DefinitionKind.CLASS,
        enclosed_tests=1,
        live_enclosed_tests=30,
        transformation_scope="bounded_class",
    )

    factors = resolve_factors(candidate, simulate_config)

    assert factors.factor_rows["business_impact"] == "broad"


def test_lane3_signal_quality_counts_deprecation_age_in_majors(
    simulate_config: PipelineConfig,
) -> None:
    """LANE 3 signal quality is the age of the deprecation in major versions."""
    factors = resolve_factors(
        lane3_candidate(deprecated_in="3.0", current_major=6), simulate_config
    )

    assert factors.factor_rows["signal_quality"] == "three_or_more"
    assert factors.signal_quality == 5


def test_resolve_factors_accepts_injected_tables(simulate_config: PipelineConfig) -> None:
    """A caller may supply already-loaded tables so a run loads the YAML once."""
    tables = load_rubrics(RUBRICS_PATH)

    injected = resolve_factors(codeql_candidate(), simulate_config, tables)
    loaded = resolve_factors(codeql_candidate(), simulate_config)

    assert injected == loaded


def test_load_rubrics_rejects_a_missing_file() -> None:
    """A missing rubric file is a `RubricError`, not an `OSError`."""
    with pytest.raises(RubricError):
        load_rubrics(RUBRICS_PATH.parent / "no-such-rubrics.yaml")


def test_load_rubrics_rejects_out_of_scale_values(tmp_path: Path) -> None:
    """A rubric row outside 1..5 is rejected at load time."""
    source = RUBRICS_PATH.read_text(encoding="utf-8").replace("critical: 5", "critical: 9", 1)
    path = tmp_path / "rubrics.yaml"
    path.write_text(source, encoding="utf-8")

    with pytest.raises(RubricError):
        load_rubrics(path)


def test_gate_consumes_resolution_output_for_every_lane(simulate_config: PipelineConfig) -> None:
    """enumerate -> resolve -> gate: the gate result carries the factors it was given."""
    for candidate in (codeql_candidate(), lane2_candidate(), lane3_candidate()):
        factors = resolve_factors(candidate, simulate_config)
        evaluation = evaluate_gates(candidate, simulate_config, resolved_factors=factors)

        assert evaluation.resolved_factors == factors
