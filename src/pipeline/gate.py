"""Pure binary gate evaluation for remediation candidates."""

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field

from pipeline.config import PipelineConfig
from pipeline.schemas import Candidate, GateName, GateResult, Lane, ReasonCode


class GateEvaluation(BaseModel):
    """Complete gate outcome, including every evaluated predicate."""

    model_config = ConfigDict(extra="forbid", strict=True)

    gate_results: dict[GateName, GateResult] = Field(default_factory=dict)
    gate_passed: bool
    failed_gate: GateName | None = None
    failed_gates: list[GateName] = Field(default_factory=list)
    risk_adjustment: int = 0


HardCondition = Callable[[Candidate, PipelineConfig, int | None], ReasonCode | None]


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


LANE_HARD_CONDITIONS: Mapping[Lane, tuple[tuple[GateName, HardCondition], ...]] = {
    Lane.CODEQL: ((GateName.LANE1_SCOPE, _lane1_scope),),
    Lane.SKIPPED_TESTS: (
        (GateName.LANE2_BREADTH, _lane2_breadth),
        (GateName.LANE2_OVERLAP, _lane2_overlap),
    ),
    Lane.DEPRECATIONS: ((GateName.NO_INTERNAL_CALLERS_AND_NO_OVERRIDE_SURFACE, _lane3_callers),),
}
HARD_CONDITIONS = LANE_HARD_CONDITIONS
HARD_CONDITION_REASONS = frozenset(
    {
        ReasonCode.OUT_OF_SCOPE_FRONTEND,
        ReasonCode.CLASS_SCOPE_TOO_BROAD,
        ReasonCode.CLASS_BREADTH_UNKNOWN,
        ReasonCode.BLOCKED_BY_ENCLOSING_SKIP,
        ReasonCode.PUBLIC_API_SURFACE,
        ReasonCode.INTERNAL_CALLER,
    }
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
) -> GateEvaluation:
    """Evaluate all binary gates before scoring, preserving each failure reason."""
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
    breadth = live_count if live_count is not None else candidate.enclosed_tests
    risk_adjustment = 0
    if (
        candidate.lane is Lane.SKIPPED_TESTS
        and candidate.kind is not None
        and candidate.kind.value == "class"
        and breadth is not None
        and breadth <= config.lane2_class_breadth_max
        and breadth > 0
    ):
        risk_adjustment = 1

    hard_reasons: dict[GateName, ReasonCode] = {}
    for gate_name, condition in LANE_HARD_CONDITIONS.get(candidate.lane, ()):
        reason = condition(candidate, config, live_count)
        if reason is not None:
            hard_reasons[gate_name] = reason

    automatability_passed = candidate.automatability is not None and candidate.automatability >= 2
    automatability_reason = None if automatability_passed else ReasonCode.AUTOMATABILITY_LOW
    for gate_name in (GateName.LANE2_BREADTH, GateName.LANE2_OVERLAP):
        if gate_name in hard_reasons:
            automatability_passed = False
            automatability_reason = hard_reasons[gate_name]
    automatability_result = GateResult(
        passed=automatability_passed,
        reason=automatability_reason,
    )
    results[GateName.AUTOMATABILITY] = automatability_result
    if not automatability_result.passed:
        failures.append(GateName.AUTOMATABILITY)

    verifiability_passed = candidate.verifiability is not None and candidate.verifiability >= 2
    has_verifiability = _verifiability_exists(candidate)
    verifiability_passed = verifiability_passed and has_verifiability
    verifiability_result = GateResult(
        passed=verifiability_passed,
        reason=None if verifiability_passed else ReasonCode.VERIFIABILITY_MISSING,
    )
    results[GateName.VERIFIABILITY_EXISTS] = verifiability_result
    if not verifiability_result.passed:
        failures.append(GateName.VERIFIABILITY_EXISTS)

    for gate_name, reason in hard_reasons.items():
        results[gate_name] = GateResult(passed=False, reason=reason)
        failures.append(gate_name)

    return GateEvaluation(
        gate_results=results,
        gate_passed=not failures,
        failed_gate=failures[0] if failures else None,
        failed_gates=failures,
        risk_adjustment=risk_adjustment,
    )


evaluate_gate = evaluate_gates
