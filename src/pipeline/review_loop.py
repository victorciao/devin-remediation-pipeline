"""Pure review-loop decisions for runtime remediation."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from pipeline.config import PipelineConfig
from pipeline.red_baseline import classify_red_baseline
from pipeline.schemas import (
    Action,
    BaselineStatus,
    Candidate,
    CandidateState,
    ExpectedFailure,
    PerItemOutcome,
    ReasonCode,
    RedBaselineResult,
)


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
    diff_reviewed: bool = False
    red_result: RedBaselineResult | None = None


@dataclass(frozen=True)
class ReviewLoopResult:
    """Final convergence decision; dispatch owns auto-merge eligibility."""

    converged: bool
    iterations: int
    state: CandidateState
    reason: ReasonCode | None = None
    disagreement_summary: str | None = None
    reviewer_only: bool = False
    needs_human_review: bool = False
    red_result: RedBaselineResult | None = None


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


def _terminal_reason(iteration: ReviewIteration) -> ReasonCode:
    if any(
        "implementer diff violates production-only policy" in finding.note
        for finding in iteration.findings
    ):
        return ReasonCode.IMPLEMENTER_TEST_EDIT
    return ReasonCode.DISAGREEMENT_UNRESOLVED


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
            iterations=1,
            state=CandidateState.TERMINAL,
            reason=ReasonCode.STALE_SKIP,
            reviewer_only=True,
            red_result=iteration.red_result,
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
            iterations=1,
            state=CandidateState.CONVERGED,
            red_result=iteration.red_result,
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
    ordinal = 1
    while ordinal <= config.iteration_cap:
        if not iteration.diff_reviewed:
            if rerun is None:
                return ReviewLoopResult(
                    converged=False,
                    iterations=ordinal - 1,
                    state=CandidateState.TERMINAL,
                    reason=_terminal_reason(iteration),
                    disagreement_summary=_disagreement_summary(iteration),
                    needs_human_review=True,
                    red_result=iteration.red_result,
                )
            iteration = rerun(ordinal)
            continue
        if iteration.red_baseline is BaselineStatus.INVALID_RED_BASELINE:
            if reauthor_attempts >= 1:
                return ReviewLoopResult(
                    converged=False,
                    iterations=ordinal,
                    state=CandidateState.TERMINAL,
                    reason=_terminal_reason(iteration),
                    disagreement_summary=_disagreement_summary(iteration),
                    needs_human_review=True,
                    red_result=iteration.red_result,
                )
            reauthor_attempts += 1
        decision = evaluate_review_iteration(
            iteration,
            major_only_requires_human=config.major_only_requires_human,
        )
        if decision is not None:
            return ReviewLoopResult(
                converged=decision.converged,
                iterations=ordinal,
                state=decision.state,
                reason=decision.reason,
                disagreement_summary=decision.disagreement_summary,
                reviewer_only=decision.reviewer_only,
                red_result=iteration.red_result,
            )
        if ordinal == config.iteration_cap or rerun is None:
            return ReviewLoopResult(
                converged=False,
                iterations=ordinal,
                state=CandidateState.TERMINAL,
                reason=_terminal_reason(iteration),
                disagreement_summary=_disagreement_summary(iteration),
                needs_human_review=True,
                red_result=iteration.red_result,
            )
        iteration = rerun(ordinal + 1)
        ordinal += 1
    raise AssertionError("review loop exhausted without a decision")


def apply_review_result(candidate: Candidate, result: ReviewLoopResult) -> Candidate:
    """Apply convergence, routing, and auto-merge outcomes to a candidate."""
    update: dict[str, object] = {
        "state": result.state,
        "reason": result.reason,
        "unresolved_major": result.reason is ReasonCode.DISAGREEMENT_UNRESOLVED,
    }
    if result.red_result is not None:
        update["red_baseline"] = result.red_result
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

    expected: ExpectedFailure | None = None
    if isinstance(raw_criteria, Sequence) and not isinstance(raw_criteria, str):
        for raw in raw_criteria:
            if not isinstance(raw, Mapping):
                continue
            raw_expected = raw.get("expected_failure")
            if not isinstance(raw_expected, Mapping):
                continue
            try:
                expected = ExpectedFailure.model_validate(raw_expected, strict=True)
            except (TypeError, ValueError):
                continue
            break
    baseline = reviewer_output.get("red_baseline")
    baseline_status = BaselineStatus.INVALID_RED_BASELINE
    red_result: RedBaselineResult | None = None
    if isinstance(baseline, Mapping) and expected is not None:
        raw_observed = baseline.get("observed", baseline.get("observed_fields"))
        observed_items: list[PerItemOutcome] = []
        if isinstance(raw_observed, Mapping):
            raw_outcomes = raw_observed.get(
                "per_item_outcomes",
                raw_observed.get("outcomes"),
            )
            if isinstance(raw_outcomes, Sequence) and not isinstance(raw_outcomes, str):
                for raw_outcome in raw_outcomes:
                    if not isinstance(raw_outcome, Mapping):
                        continue
                    try:
                        observed_items.append(
                            PerItemOutcome.model_validate(raw_outcome, strict=True)
                        )
                    except (TypeError, ValueError):
                        continue
            else:
                try:
                    observed_items.append(PerItemOutcome.model_validate(raw_observed, strict=True))
                except (TypeError, ValueError):
                    pass
        if observed_items:
            red_result = classify_red_baseline(expected, observed_items)
            baseline_status = red_result.status
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
    diff_reviewed_value = reviewer_output.get("diff_reviewed")
    diff_reviewed = False
    if isinstance(diff_reviewed_value, Mapping):
        base_sha = diff_reviewed_value.get("base_sha")
        head_sha = diff_reviewed_value.get("head_sha")
        files_read = diff_reviewed_value.get("files_read")
        has_files = isinstance(files_read, Sequence) and not isinstance(files_read, str)
        changed = implementer_output.get("files_changed") if implementer_output else None
        changed_count = (
            isinstance(changed, Sequence) and not isinstance(changed, str) and bool(changed)
        )
        diff_reviewed = (
            isinstance(base_sha, str)
            and bool(base_sha)
            and isinstance(head_sha, str)
            and bool(head_sha)
            and has_files
            and (bool(files_read) or not changed_count)
        )
    return ReviewIteration(
        red_baseline=baseline_status,
        green=green,
        findings=tuple(findings),
        planner_criteria=frozenset(planner_criteria),
        reviewer_criteria=frozenset(reviewer_criteria),
        addressed_criteria=frozenset(addressed),
        diff_reviewed=diff_reviewed,
        red_result=red_result,
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
