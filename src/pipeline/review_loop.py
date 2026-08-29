"""Pure review-loop decisions for runtime remediation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from pipeline.config import PipelineConfig
from pipeline.schemas import Action, BaselineStatus, Candidate, CandidateState, ReasonCode


class FindingSeverity(str, Enum):
    """Allowed reviewer finding severities."""

    BLOCKING = "blocking"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"


@dataclass(frozen=True)
class ReviewFinding:
    """One normalized reviewer finding."""

    severity: FindingSeverity
    criterion_id: str | None
    note: str


@dataclass(frozen=True)
class ReviewIteration:
    """Observable result of one red, green, and review iteration."""

    red_baseline: BaselineStatus
    green: bool
    findings: tuple[ReviewFinding, ...] = ()
    planner_criteria: frozenset[str] = frozenset()
    reviewer_criteria: frozenset[str] = frozenset()
    addressed_criteria: frozenset[str] = frozenset()
    failing_test: str | None = None
    pre_fix_signature: str | None = None
    fix_rationale: str | None = None


@dataclass(frozen=True)
class ReviewLoopResult:
    """Final convergence and independent auto-merge decisions."""

    converged: bool
    auto_merge_eligible: bool
    iterations: int
    state: CandidateState
    reason: ReasonCode | None = None
    disagreement_summary: str | None = None
    reviewer_only: bool = False
    needs_human_review: bool = False


def _criterion_findings(iteration: ReviewIteration) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    unmapped = iteration.reviewer_criteria - iteration.planner_criteria
    for criterion_id in sorted(unmapped):
        findings.append(
            ReviewFinding(
                FindingSeverity.MAJOR,
                criterion_id,
                f"reviewer test maps to unknown criterion {criterion_id}",
            )
        )
    unaddressed = iteration.planner_criteria - iteration.addressed_criteria
    for criterion_id in sorted(unaddressed):
        findings.append(
            ReviewFinding(
                FindingSeverity.MAJOR,
                criterion_id,
                f"planner criterion {criterion_id} is unaddressed",
            )
        )
    return findings


def _disagreement_summary(iteration: ReviewIteration) -> str:
    """Render the required human handoff for an unresolved disagreement."""
    failing_test = iteration.failing_test or "<unknown test>"
    signature = iteration.pre_fix_signature or "<unknown signature>"
    rationale = iteration.fix_rationale or "<no fix rationale supplied>"
    return (
        f"failing test: {failing_test}; mapped criterion: "
        f"{', '.join(sorted(iteration.reviewer_criteria)) or '<none>'}; "
        f"pre-fix signature: {signature}; fix rationale: {rationale}"
    )


def evaluate_review_iteration(
    iteration: ReviewIteration,
    *,
    major_only_requires_human: bool = True,
) -> ReviewLoopResult | None:
    """Return a terminal decision when one iteration has settled the loop."""
    findings = list(iteration.findings)
    findings.extend(_criterion_findings(iteration))
    if iteration.red_baseline is BaselineStatus.STALE_SKIP and not findings:
        return ReviewLoopResult(
            converged=True,
            auto_merge_eligible=False,
            iterations=1,
            state=CandidateState.TERMINAL,
            reason=ReasonCode.STALE_SKIP,
            reviewer_only=True,
        )
    if iteration.red_baseline is BaselineStatus.STALE_SKIP:
        findings.append(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "stale-skip reviewer output failed criterion mapping",
            )
        )
    if iteration.red_baseline is not BaselineStatus.VALID:
        findings.append(
            ReviewFinding(
                FindingSeverity.BLOCKING,
                None,
                "red baseline is invalid",
            )
        )
    if not iteration.green:
        findings.append(
            ReviewFinding(
                FindingSeverity.MAJOR,
                None,
                "reviewer test remains red after the implementer fix",
            )
        )
    has_blocking = any(finding.severity is FindingSeverity.BLOCKING for finding in findings)
    has_major = any(finding.severity is FindingSeverity.MAJOR for finding in findings)
    if not has_blocking and not has_major:
        return ReviewLoopResult(
            converged=True,
            auto_merge_eligible=not has_major,
            iterations=1,
            state=CandidateState.CONVERGED,
        )
    return None


def run_review_loop(
    config: PipelineConfig,
    initial: ReviewIteration,
    rerun: Callable[[int], ReviewIteration] | None = None,
) -> ReviewLoopResult:
    """Iterate implementer/reviewer decisions until convergence or the cap."""
    iteration = initial
    reauthor_attempts = 0
    for ordinal in range(1, config.iteration_cap + 1):
        if iteration.red_baseline is BaselineStatus.INVALID_RED_BASELINE:
            if reauthor_attempts >= 1:
                return ReviewLoopResult(
                    converged=False,
                    auto_merge_eligible=False,
                    iterations=ordinal,
                    state=CandidateState.TERMINAL,
                    reason=ReasonCode.DISAGREEMENT_UNRESOLVED,
                    disagreement_summary=_disagreement_summary(iteration),
                    needs_human_review=True,
                )
            reauthor_attempts += 1
        decision = evaluate_review_iteration(
            iteration,
            major_only_requires_human=config.major_only_requires_human,
        )
        if decision is not None:
            return ReviewLoopResult(
                converged=decision.converged,
                auto_merge_eligible=decision.auto_merge_eligible,
                iterations=ordinal,
                state=decision.state,
                reason=decision.reason,
                disagreement_summary=decision.disagreement_summary,
                reviewer_only=decision.reviewer_only,
            )
        if ordinal == config.iteration_cap or rerun is None:
            return ReviewLoopResult(
                converged=False,
                auto_merge_eligible=False,
                iterations=ordinal,
                state=CandidateState.TERMINAL,
                reason=ReasonCode.DISAGREEMENT_UNRESOLVED,
                disagreement_summary=_disagreement_summary(iteration),
                needs_human_review=True,
            )
        iteration = rerun(ordinal + 1)
    raise AssertionError("review loop exhausted without a decision")


def apply_review_result(candidate: Candidate, result: ReviewLoopResult) -> Candidate:
    """Apply convergence, routing, and auto-merge outcomes to a candidate."""
    update: dict[str, object] = {
        "state": result.state,
        "reason": result.reason,
        "auto_merge_eligible": result.auto_merge_eligible,
        "unresolved_major": result.reason is ReasonCode.DISAGREEMENT_UNRESOLVED,
    }
    if result.reviewer_only:
        update["action"] = Action.REVIEWER_ONLY_DIFF
    elif result.needs_human_review:
        update["action"] = Action.HUMAN_REVIEW
        if "needs-human-review" not in candidate.labels:
            update["labels"] = [*candidate.labels, "needs-human-review"]
    return candidate.model_copy(update=update)


def review_iteration_from_payload(
    planner_output: Mapping[str, object],
    reviewer_output: Mapping[str, object],
    implementer_output: Mapping[str, object] | None = None,
) -> ReviewIteration:
    """Normalize role structured outputs into a pure loop input."""
    raw_criteria = planner_output.get("criteria")
    planner_criteria: set[str] = set()
    if isinstance(raw_criteria, Sequence) and not isinstance(raw_criteria, str):
        for raw in raw_criteria:
            if isinstance(raw, Mapping):
                criterion_id = raw.get("id")
                if isinstance(criterion_id, str) and criterion_id:
                    planner_criteria.add(criterion_id)

    raw_tests = reviewer_output.get("tests")
    reviewer_criteria: set[str] = set()
    if isinstance(raw_tests, Sequence) and not isinstance(raw_tests, str):
        for raw in raw_tests:
            if isinstance(raw, Mapping):
                criterion_id = raw.get("criterion_id")
                if isinstance(criterion_id, str) and criterion_id:
                    reviewer_criteria.add(criterion_id)

    raw_findings = reviewer_output.get("findings")
    findings: list[ReviewFinding] = []
    if isinstance(raw_findings, Sequence) and not isinstance(raw_findings, str):
        for raw in raw_findings:
            if not isinstance(raw, Mapping):
                continue
            severity = raw.get("severity")
            note = raw.get("note")
            if not isinstance(severity, str) or not isinstance(note, str):
                continue
            try:
                parsed_severity = FindingSeverity(severity)
            except ValueError:
                parsed_severity = FindingSeverity.BLOCKING
            criterion_id = raw.get("criterion_id")
            findings.append(
                ReviewFinding(
                    parsed_severity,
                    criterion_id if isinstance(criterion_id, str) else None,
                    note,
                )
            )

    baseline = reviewer_output.get("red_baseline")
    baseline_status = BaselineStatus.INVALID_RED_BASELINE
    if isinstance(baseline, Mapping):
        raw_status = baseline.get("status")
        if isinstance(raw_status, str):
            try:
                baseline_status = BaselineStatus(raw_status)
            except ValueError:
                pass
    green_result = reviewer_output.get("green_result")
    green = False
    if isinstance(green_result, Mapping):
        green = green_result.get("passed") is True or green_result.get("status") in {
            "passed",
            "green",
            "success",
        }
    addressed: set[str] = set()
    raw_addressed = (
        implementer_output.get("criteria_addressed")
        if implementer_output is not None
        else reviewer_output.get("criteria_addressed")
    )
    if isinstance(raw_addressed, Sequence) and not isinstance(raw_addressed, str):
        addressed = {item for item in raw_addressed if isinstance(item, str)}
    return ReviewIteration(
        red_baseline=baseline_status,
        green=green,
        findings=tuple(findings),
        planner_criteria=frozenset(planner_criteria),
        reviewer_criteria=frozenset(reviewer_criteria),
        addressed_criteria=frozenset(addressed),
    )


__all__ = [
    "FindingSeverity",
    "ReviewFinding",
    "ReviewIteration",
    "ReviewLoopResult",
    "evaluate_review_iteration",
    "apply_review_result",
    "review_iteration_from_payload",
    "run_review_loop",
]
