"""§4 stage-1 gate: resolved rubric predicates plus every lane-specific hard condition."""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.config import PipelineConfig
from pipeline.gate import HARD_CONDITION_REASONS, LANE_HARD_CONDITIONS, evaluate_gates
from pipeline.rubric import RubricTables, resolve_factors
from pipeline.schemas import Candidate, DefinitionKind, GateName, ReasonCode
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate

PARENT_NODEID = "tests/integration_tests/charts/data/api_tests.py::TestPostChartDataApi"
CHILD_NODEID = f"{PARENT_NODEID}::test_chart_data_get"

# §4 assigns each hard condition to the gate it fails; the reason string is never collapsed.
EXPECTED_HARD_CONDITION_GATES = {
    ReasonCode.OUT_OF_SCOPE_FRONTEND: GateName.VERIFIABILITY_EXISTS,
    ReasonCode.CLASS_SCOPE_TOO_BROAD: GateName.AUTOMATABILITY,
    ReasonCode.CLASS_BREADTH_UNKNOWN: GateName.AUTOMATABILITY,
    ReasonCode.BLOCKED_BY_ENCLOSING_SKIP: GateName.AUTOMATABILITY,
    ReasonCode.PUBLIC_API_SURFACE: GateName.AUTOMATABILITY,
    ReasonCode.INTERNAL_CALLER: GateName.AUTOMATABILITY,
}


def reason_of(
    evaluation_gate: GateName, candidate: Candidate, config: PipelineConfig
) -> ReasonCode:
    """The reason recorded against one gate for a candidate."""
    result = evaluate_gates(candidate, config).gate_results[evaluation_gate]
    assert result.reason is not None
    return result.reason


def test_gate_passes_for_clean_candidate(simulate_config: PipelineConfig) -> None:
    """A candidate whose resolved rubric values are >= 2 with no hard condition passes."""
    evaluation = evaluate_gates(codeql_candidate(), simulate_config)

    assert evaluation.gate_passed is True
    assert evaluation.failed_gate is None
    assert evaluation.failed_gates == []
    assert evaluation.hard_condition_failures == []
    assert evaluation.gate_results[GateName.TRIGGER_EXISTS].passed is True
    assert evaluation.gate_results[GateName.AUTOMATABILITY].passed is True
    assert evaluation.gate_results[GateName.VERIFIABILITY_EXISTS].passed is True


def test_gate_name_holds_only_the_three_section_four_gates() -> None:
    """§4 defines exactly three gates; hard conditions are reasons, not extra gates."""
    assert set(GateName) == {
        GateName.TRIGGER_EXISTS,
        GateName.AUTOMATABILITY,
        GateName.VERIFIABILITY_EXISTS,
    }


def test_gate_trigger_exists_requires_machine_readable_source(
    simulate_config: PipelineConfig,
) -> None:
    """A LANE 1 candidate with no rule id has no machine-readable trigger."""
    candidate = codeql_candidate(rule_id=None)

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.TRIGGER_EXISTS
    assert evaluation.gate_results[GateName.TRIGGER_EXISTS].reason is ReasonCode.TRIGGER_MISSING


def test_gate_reads_the_resolved_rubric_value_not_a_preset_field(
    simulate_config: PipelineConfig,
) -> None:
    """§4.1 — the gate threshold applies to the *resolved* value.

    The candidate carries a flattering pre-set `automatability` while its observable resolves
    to 1, so a gate that read the raw field would wrongly pass.
    """
    candidate = codeql_candidate(transformation_scope="product_judgment", automatability=5)

    factors = resolve_factors(candidate, simulate_config)
    evaluation = evaluate_gates(candidate, simulate_config)

    assert candidate.automatability == 5
    assert factors.automatability == 1
    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.AUTOMATABILITY_LOW


def test_gate_uses_the_supplied_resolved_factors_without_re_resolving(
    simulate_config: PipelineConfig,
    rubrics: RubricTables,
) -> None:
    """§4.1 — factors resolved once before gating are the ones the gate evaluates."""
    candidate = codeql_candidate()
    factors = resolve_factors(candidate, simulate_config, rubrics)
    lowered = factors.model_copy(update={"automatability": 1})

    evaluation = evaluate_gates(candidate, simulate_config, resolved_factors=lowered)

    assert evaluation.resolved_factors == lowered
    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY


def test_gate_records_the_factors_it_evaluated(simulate_config: PipelineConfig) -> None:
    """The evaluation carries the resolved factors forward so scoring never re-resolves."""
    candidate = codeql_candidate()

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.resolved_factors == resolve_factors(candidate, simulate_config)


@pytest.mark.parametrize(
    ("observable", "gate_name", "reason"),
    [
        (
            {"transformation_scope": "product_judgment"},
            GateName.AUTOMATABILITY,
            ReasonCode.AUTOMATABILITY_LOW,
        ),
        (
            {"targeted_test_signal": "absent"},
            GateName.VERIFIABILITY_EXISTS,
            ReasonCode.VERIFIABILITY_MISSING,
        ),
    ],
)
def test_gate_rubric_one_fails(
    simulate_config: PipelineConfig,
    observable: dict[str, Any],
    gate_name: GateName,
    reason: ReasonCode,
) -> None:
    """A resolved rubric value of 1 fails the gate; `>= 2` is the pass line."""
    candidate = codeql_candidate(**observable)

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is gate_name
    assert evaluation.gate_results[gate_name].reason is reason


def test_gate_rubric_two_is_the_pass_line(simulate_config: PipelineConfig) -> None:
    """A resolved value of exactly 2 passes: the predicate is `>= 2`, not `> 2`."""
    candidate = codeql_candidate(
        transformation_scope="constrained_transform",
        targeted_test_signal="weak",
    )

    factors = resolve_factors(candidate, simulate_config)
    evaluation = evaluate_gates(candidate, simulate_config)

    assert (factors.automatability, factors.verifiability) == (2, 2)
    assert evaluation.gate_passed is True


def test_gate_applies_hard_conditions_beyond_rubric(simulate_config: PipelineConfig) -> None:
    """§17 — every resolved rubric >= 2 still fails on a lane-specific hard condition."""
    candidate = codeql_candidate(file_path="superset-frontend/src/components/Chart.tsx")

    factors = resolve_factors(candidate, simulate_config)
    evaluation = evaluate_gates(candidate, simulate_config)

    assert min(factors.automatability, factors.verifiability) >= 2
    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.VERIFIABILITY_EXISTS
    assert (
        evaluation.gate_results[GateName.VERIFIABILITY_EXISTS].reason
        is ReasonCode.OUT_OF_SCOPE_FRONTEND
    )
    assert evaluation.hard_condition_failures == [ReasonCode.OUT_OF_SCOPE_FRONTEND]


def test_gate_lane1_scope_accepts_superset_python_paths(simulate_config: PipelineConfig) -> None:
    """Only `superset/**/*.py` alert paths clear the §5 LANE 1 scope check."""
    candidate = codeql_candidate(file_path="superset/views/core.py")

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.hard_condition_failures == []
    assert evaluation.gate_results[GateName.VERIFIABILITY_EXISTS].passed is True


def test_broad_class_skip_is_human_routed(simulate_config: PipelineConfig) -> None:
    """§17 — `enclosed_tests` above the ceiling is `class_scope_too_broad`."""
    candidate = lane2_candidate(
        nodeid=PARENT_NODEID,
        kind=DefinitionKind.CLASS,
        class_scope="TestPostChartDataApi",
        enclosed_tests=52,
        collects_single_item=False,
    )

    evaluation = evaluate_gates(candidate, simulate_config)

    assert simulate_config.lane2_class_breadth_max == 5
    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert (
        evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.CLASS_SCOPE_TOO_BROAD
    )


def test_zero_breadth_class_skip_is_class_breadth_unknown(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — a `kind = class` row with `enclosed_tests = 0` is `class_breadth_unknown`."""
    candidate = lane2_candidate(
        nodeid=PARENT_NODEID,
        kind=DefinitionKind.CLASS,
        class_scope="TestPostChartDataApi",
        enclosed_tests=0,
    )

    evaluation = evaluate_gates(candidate, simulate_config, live_enclosed_tests=None)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert (
        evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.CLASS_BREADTH_UNKNOWN
    )


def test_class_skip_within_ceiling_passes(simulate_config: PipelineConfig) -> None:
    """At or below `lane2_class_breadth_max` a class row clears the breadth condition."""
    candidate = lane2_candidate(
        nodeid=PARENT_NODEID,
        kind=DefinitionKind.CLASS,
        class_scope="TestPostChartDataApi",
        enclosed_tests=4,
        transformation_scope="bounded_class",
    )

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.hard_condition_failures == []
    assert evaluation.gate_passed is True


def test_live_collection_count_overrides_ast_lower_bound(
    simulate_config: PipelineConfig,
) -> None:
    """§17 — a live `--collect-only` count wins over the AST lower bound, both ways."""
    zero_ast = lane2_candidate(
        nodeid=PARENT_NODEID,
        kind=DefinitionKind.CLASS,
        class_scope="TestPostChartDataApi",
        enclosed_tests=0,
        transformation_scope="bounded_class",
    )

    broad = evaluate_gates(zero_ast, simulate_config, live_enclosed_tests=11)
    narrow = evaluate_gates(zero_ast, simulate_config, live_enclosed_tests=3)

    assert broad.gate_passed is False
    assert broad.hard_condition_failures == [ReasonCode.CLASS_SCOPE_TOO_BROAD]
    assert narrow.gate_passed is True
    assert narrow.hard_condition_failures == []


def test_live_collection_count_overrides_understated_ast_bound(
    simulate_config: PipelineConfig,
) -> None:
    """An AST bound below the ceiling does not rescue a class that collects more items."""
    candidate = lane2_candidate(
        nodeid=PARENT_NODEID,
        kind=DefinitionKind.CLASS,
        class_scope="TestPostChartDataApi",
        enclosed_tests=2,
        transformation_scope="bounded_class",
    )

    evaluation = evaluate_gates(candidate, simulate_config, live_enclosed_tests=25)

    assert evaluation.gate_passed is False
    assert evaluation.hard_condition_failures == [ReasonCode.CLASS_SCOPE_TOO_BROAD]


def test_child_of_enclosing_skip_fails_automatability(simulate_config: PipelineConfig) -> None:
    """§4.2/§9.2 — an enclosed child is `blocked_by_enclosing_skip` at the gate."""
    child = lane2_candidate(
        candidate_id="child",
        nodeid=CHILD_NODEID,
        class_scope="TestPostChartDataApi",
        enclosing_skip_nodeid=PARENT_NODEID,
    )

    evaluation = evaluate_gates(child, simulate_config)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert (
        evaluation.gate_results[GateName.AUTOMATABILITY].reason
        is ReasonCode.BLOCKED_BY_ENCLOSING_SKIP
    )


@pytest.mark.parametrize(
    ("qualname", "internal_caller", "override_surface", "public_api_surface", "reason"),
    [
        (
            "DatabaseRestApi.table_extra_metadata_deprecated",
            False,
            False,
            True,
            ReasonCode.PUBLIC_API_SURFACE,
        ),
        (
            "BaseEngineSpec.get_url_for_impersonation",
            True,
            True,
            False,
            ReasonCode.INTERNAL_CALLER,
        ),
        (
            "BaseEngineSpec.get_url_for_impersonation",
            False,
            True,
            False,
            ReasonCode.INTERNAL_CALLER,
        ),
    ],
)
def test_deprecation_caller_and_override_surface_fail_automatability(
    simulate_config: PipelineConfig,
    qualname: str,
    internal_caller: bool,
    override_surface: bool,
    public_api_surface: bool,
    reason: ReasonCode,
) -> None:
    """§4.2 — a live caller or an override surface fails the LANE 3 hard condition."""
    candidate = lane3_candidate(
        qualname=qualname,
        internal_caller=internal_caller,
        override_surface=override_surface,
        public_api_surface=public_api_surface,
    )

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is reason


def test_normalize_indexes_passes_the_deprecation_gate(simulate_config: PipelineConfig) -> None:
    """§4.2 — `normalize_indexes` is the one automatable LANE 3 candidate."""
    candidate = lane3_candidate()

    evaluation = evaluate_gates(candidate, simulate_config)

    assert evaluation.hard_condition_failures == []
    assert evaluation.gate_passed is True


def test_hard_condition_reasons_is_derived_and_complete() -> None:
    """§17 — every §4 hard condition is registered, and the registry derives the reason set."""
    derived = frozenset(
        reason
        for conditions in LANE_HARD_CONDITIONS.values()
        for _, reasons, _ in conditions
        for reason in reasons
    )

    assert HARD_CONDITION_REASONS == derived
    assert HARD_CONDITION_REASONS == frozenset(EXPECTED_HARD_CONDITION_GATES)


def test_each_hard_condition_is_registered_against_the_gate_the_plan_assigns() -> None:
    """§5 routes `out_of_scope_frontend` to verifiability; §4.2 sends the rest to automatability."""
    registered = {
        reason: gate_name
        for conditions in LANE_HARD_CONDITIONS.values()
        for gate_name, reasons, _ in conditions
        for reason in reasons
    }

    assert registered == EXPECTED_HARD_CONDITION_GATES


def test_every_hard_condition_records_its_own_reason(simulate_config: PipelineConfig) -> None:
    """No hard condition may collapse into a generic `automatability_low`."""
    cases: dict[ReasonCode, Candidate] = {
        ReasonCode.OUT_OF_SCOPE_FRONTEND: codeql_candidate(file_path="superset-frontend/src/x.ts"),
        ReasonCode.CLASS_SCOPE_TOO_BROAD: lane2_candidate(
            nodeid=PARENT_NODEID,
            kind=DefinitionKind.CLASS,
            enclosed_tests=52,
            collects_single_item=False,
        ),
        ReasonCode.CLASS_BREADTH_UNKNOWN: lane2_candidate(
            nodeid=PARENT_NODEID, kind=DefinitionKind.CLASS, enclosed_tests=0
        ),
        ReasonCode.BLOCKED_BY_ENCLOSING_SKIP: lane2_candidate(
            nodeid=CHILD_NODEID, enclosing_skip_nodeid=PARENT_NODEID
        ),
        ReasonCode.PUBLIC_API_SURFACE: lane3_candidate(public_api_surface=True),
        ReasonCode.INTERNAL_CALLER: lane3_candidate(internal_caller=True, caller_count=2),
    }

    observed = {
        expected: reason_of(EXPECTED_HARD_CONDITION_GATES[expected], candidate, simulate_config)
        for expected, candidate in cases.items()
    }

    assert observed == {expected: expected for expected in cases}
    assert set(observed) == set(HARD_CONDITION_REASONS)
