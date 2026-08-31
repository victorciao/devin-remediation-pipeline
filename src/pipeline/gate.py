"""Pure binary gate evaluation for remediation candidates."""

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import PipelineConfig
from pipeline.rubric import ResolvedFactors, RubricTables, resolve_factors
from pipeline.schemas import Candidate, GateName, GateResult, Lane, ReasonCode


class GateEvaluation(BaseModel):
    """Complete gate outcome, including every evaluated predicate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    gate_results: dict[GateName, GateResult] = Field(default_factory=dict)
    gate_passed: bool
    failed_gate: GateName | None = None
    failed_gates: list[GateName] = Field(default_factory=list)
    hard_condition_failures: list[ReasonCode] = Field(default_factory=list)
    resolved_factors: ResolvedFactors | None = None


HardCondition = Callable[[Candidate, PipelineConfig, int | None], ReasonCode | None]
HardConditionSpec = tuple[GateName, frozenset[ReasonCode], HardCondition]


def _lane1_scope(
    candidate: Candidate,
    config: PipelineConfig,
    live_count: int | None,
) -> ReasonCode | None:
    del config, live_count
    if candidate.file_path is None:
        return None
    if not candidate.file_path.startswith("superset/") or not candidate.file_path.endswith(".py"):
        return ReasonCode.OUT_OF_SCOPE_FRONTEND
    return None


def _lane2_breadth(
    candidate: Candidate,
    config: PipelineConfig,
    live_count: int | None,
) -> ReasonCode | None:
    if candidate.kind is None:
        return None
    live_obtained = live_count is not None or candidate.live_enclosed_tests is not None
    count = live_count if live_count is not None else candidate.live_enclosed_tests
    if count is None:
        count = candidate.enclosed_tests
    if candidate.kind.value == "class" and not live_obtained and (count is None or count == 0):
        return ReasonCode.CLASS_BREADTH_UNKNOWN
    if count is not None and count > config.lane2_class_breadth_max:
        return ReasonCode.CLASS_SCOPE_TOO_BROAD
    return None


def _lane2_overlap(
    candidate: Candidate,
    config: PipelineConfig,
    live_count: int | None,
) -> ReasonCode | None:
    del config, live_count
    if candidate.enclosing_skip_nodeid:
        return ReasonCode.BLOCKED_BY_ENCLOSING_SKIP
    return None


def _lane3_callers(
    candidate: Candidate,
    config: PipelineConfig,
    live_count: int | None,
) -> ReasonCode | None:
    del config, live_count
    if candidate.public_api_surface:
        return ReasonCode.PUBLIC_API_SURFACE
    if candidate.internal_caller or candidate.override_surface:
        return ReasonCode.INTERNAL_CALLER
    return None


LANE_HARD_CONDITIONS: Mapping[Lane, tuple[HardConditionSpec, ...]] = {
    Lane.CODEQL: (
        (
            GateName.VERIFIABILITY_EXISTS,
            frozenset({ReasonCode.OUT_OF_SCOPE_FRONTEND}),
            _lane1_scope,
        ),
    ),
    Lane.SKIPPED_TESTS: (
        (
            GateName.AUTOMATABILITY,
            frozenset(
                {
                    ReasonCode.CLASS_SCOPE_TOO_BROAD,
                    ReasonCode.CLASS_BREADTH_UNKNOWN,
                }
            ),
            _lane2_breadth,
        ),
        (
            GateName.AUTOMATABILITY,
            frozenset({ReasonCode.BLOCKED_BY_ENCLOSING_SKIP}),
            _lane2_overlap,
        ),
    ),
    Lane.DEPRECATIONS: (
        (
            GateName.AUTOMATABILITY,
            frozenset({ReasonCode.PUBLIC_API_SURFACE, ReasonCode.INTERNAL_CALLER}),
            _lane3_callers,
        ),
    ),
}
HARD_CONDITION_REASONS = frozenset(
    reason
    for conditions in LANE_HARD_CONDITIONS.values()
    for _, reasons, _ in conditions
    for reason in reasons
)


def _trigger_exists(candidate: Candidate) -> bool:
    if candidate.trigger_exists is not None:
        return candidate.trigger_exists
    if candidate.lane is Lane.CODEQL:
        return candidate.rule_id is not None
    if candidate.lane is Lane.SKIPPED_TESTS:
        return candidate.nodeid is not None
    return candidate.module is not None and candidate.qualname is not None


def _verifiability_exists(candidate: Candidate) -> bool:
    if candidate.verifiability_exists is not None:
        return candidate.verifiability_exists
    if candidate.lane is Lane.CODEQL:
        return candidate.file_path is not None
    if candidate.lane is Lane.SKIPPED_TESTS:
        return candidate.nodeid is not None
    return candidate.module is not None and candidate.qualname is not None


def evaluate_gates(
    candidate: Candidate,
    config: PipelineConfig,
    *,
    live_enclosed_tests: int | None = None,
    rubrics: RubricTables | None = None,
    resolved_factors: ResolvedFactors | None = None,
) -> GateEvaluation:
    """Evaluate all binary gates before scoring, preserving each failure reason."""
    factors = (
        resolved_factors
        if resolved_factors is not None
        else resolve_factors(candidate, config, rubrics)
    )
    results: dict[GateName, GateResult] = {}
    failures: list[GateName] = []

    trigger_result = GateResult(
        passed=_trigger_exists(candidate),
        reason=None if _trigger_exists(candidate) else ReasonCode.TRIGGER_MISSING,
    )
    results[GateName.TRIGGER_EXISTS] = trigger_result
    if not trigger_result.passed:
        failures.append(GateName.TRIGGER_EXISTS)

    live_count = live_enclosed_tests
    if live_count is None:
        live_count = candidate.live_enclosed_tests
    hard_failures: list[tuple[GateName, ReasonCode]] = []
    for gate_name, allowed_reasons, condition in LANE_HARD_CONDITIONS.get(candidate.lane, ()):
        reason = condition(candidate, config, live_count)
        if reason is not None:
            if reason not in allowed_reasons:
                raise ValueError(f"hard condition returned an unregistered reason: {reason.value}")
            hard_failures.append((gate_name, reason))

    automatability_passed = factors.automatability >= 2
    automatability_reason = None if automatability_passed else ReasonCode.AUTOMATABILITY_LOW
    automatability_failures = [
        reason for gate_name, reason in hard_failures if gate_name is GateName.AUTOMATABILITY
    ]
    if automatability_failures:
        automatability_passed = False
        automatability_reason = automatability_failures[0]
    automatability_result = GateResult(
        passed=automatability_passed,
        reason=automatability_reason,
    )
    results[GateName.AUTOMATABILITY] = automatability_result
    if not automatability_result.passed:
        failures.append(GateName.AUTOMATABILITY)

    verifiability_passed = factors.verifiability >= 2
    has_verifiability = _verifiability_exists(candidate)
    verifiability_passed = verifiability_passed and has_verifiability
    verifiability_failures = [
        reason for gate_name, reason in hard_failures if gate_name is GateName.VERIFIABILITY_EXISTS
    ]
    if verifiability_failures:
        verifiability_passed = False
    verifiability_result = GateResult(
        passed=verifiability_passed,
        reason=(
            verifiability_failures[0]
            if verifiability_failures
            else None
            if verifiability_passed
            else ReasonCode.VERIFIABILITY_MISSING
        ),
    )
    results[GateName.VERIFIABILITY_EXISTS] = verifiability_result
    if not verifiability_result.passed:
        failures.append(GateName.VERIFIABILITY_EXISTS)

    return GateEvaluation(
        gate_results=results,
        gate_passed=not failures,
        failed_gate=failures[0] if failures else None,
        failed_gates=failures,
        hard_condition_failures=[reason for _, reason in hard_failures],
        resolved_factors=factors,
    )
