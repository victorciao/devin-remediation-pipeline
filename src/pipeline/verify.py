"""Orchestrator-owned evaluation of each lane's declared success criterion.

Nothing a session reports about its own results is evidence here: every
evaluator observes the outcome itself through an injected observation seam and
returns either the evidence it observed or the terminal reason that stops the
candidate.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pipeline.config import CiEvidenceMode, Lane1AlertCheck, PipelineConfig
from pipeline.red_baseline import classify_red_baseline
from pipeline.schemas import (
    BaselineStatus,
    Candidate,
    CriterionEvidence,
    ExpectedFailure,
    ItemOutcome,
    Lane,
    PerItemOutcome,
    ReasonCode,
    RedBaselineResult,
)

LANE2_CRITERION = (
    "The re-enabled test goes red at base_sha with only the test-path diff applied, "
    "and green at the candidate head."
)
LANE1_CRITERION = (
    "The alert's stable locator is absent at the candidate head and the suite covering "
    "suite_scope passes there."
)
LANE3_CRITERION = (
    "The module:qualname no longer resolves at the candidate head, no internal caller or "
    "override surface references it, and the suite covering suite_scope passes there."
)


def declare_success_criterion(lane: Lane) -> str:
    """Return the criterion a lane declares for every candidate it emits."""
    if lane is Lane.SKIPPED_TESTS:
        return LANE2_CRITERION
    if lane is Lane.CODEQL:
        return LANE1_CRITERION
    return LANE3_CRITERION


@dataclass(frozen=True)
class SuiteResult:
    """Observed result of running the suite covering a candidate's scope."""

    passed: bool
    command: str
    failing_nodeids: tuple[str, ...] = ()
    reason: ReasonCode | None = None


@dataclass(frozen=True)
class SymbolObservation:
    """Observed resolution and reference surface of a deprecated symbol."""

    resolves: bool
    caller_count: int
    override_count: int
    command: str
    references: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlertObservation:
    """Observed alert locators at a candidate head, or their unavailability."""

    locators: tuple[str, ...]
    command: str
    available: bool = True
    detail: str | None = None


@dataclass(frozen=True)
class ItemRunResult:
    """Observed per-item outcomes of running one nodeid at one revision."""

    outcomes: tuple[PerItemOutcome, ...]
    command: str
    reason: ReasonCode | None = None


ItemRunner = Callable[[str, str], ItemRunResult]
SuiteRunner = Callable[[Sequence[str], str], SuiteResult]
SymbolProbe = Callable[[Candidate, str], SymbolObservation]
AlertProbe = Callable[[Candidate, str], AlertObservation]
Stage = Literal["pre_pr", "post_pr"]


@dataclass
class Observers:
    """The observation seams an evaluator is allowed to use."""

    run_item: ItemRunner | None = None
    run_suite: SuiteRunner | None = None
    probe_symbol: SymbolProbe | None = None
    probe_alerts: AlertProbe | None = None
    commands: list[str] = field(default_factory=list)


def _unobservable(criterion: str, commands: Sequence[str], detail: str) -> CriterionEvidence:
    return CriterionEvidence(
        criterion=criterion,
        satisfied=False,
        commands=list(commands),
        observations=[detail],
        reason=ReasonCode.CRITERION_NOT_MET,
    )


def _baseline_expectation(candidate: Candidate) -> ExpectedFailure:
    return candidate.expected_failure or ExpectedFailure(
        nodeid=candidate.nodeid or candidate.stable_locator,
        exception_type="AssertionError",
        message_pattern=".",
    )


def _descendants(candidate: Candidate) -> tuple[str, ...]:
    return tuple(
        marker for marker in candidate.lifted_markers if marker != candidate.enclosing_skip_nodeid
    )


def verify_lane2(
    candidate: Candidate,
    *,
    base_sha: str,
    head_sha: str,
    observers: Observers,
) -> tuple[CriterionEvidence, RedBaselineResult | None]:
    """Evaluate LANE 2's red-at-base / green-at-head criterion."""
    criterion = candidate.success_criterion or LANE2_CRITERION
    nodeid = candidate.nodeid
    if observers.run_item is None or nodeid is None:
        return (
            _unobservable(criterion, (), "no local checkout was available to run the nodeid"),
            None,
        )
    owner = getattr(observers.run_item, "__self__", None)
    with_diff = getattr(owner, "run_item_with_test_diff", None)
    if callable(with_diff):
        base_run = with_diff(base_sha, head_sha, nodeid, candidate.test_paths)
    else:
        base_run = observers.run_item(base_sha, nodeid)
    if base_run.reason is not None:
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                commands=[base_run.command],
                observations=["test capability was unavailable at base"],
                reason=base_run.reason,
            ),
            None,
        )
    baseline = classify_red_baseline(
        _baseline_expectation(candidate),
        base_run.outcomes,
        descendant_nodeids=_descendants(candidate),
    )
    commands = [base_run.command]
    observations = [
        f"{outcome.nodeid}: {outcome.outcome.value} at base" for outcome in base_run.outcomes
    ]
    if baseline.still_skipped_descendants:
        observations.append(
            "still_skipped_descendants: " + ", ".join(baseline.still_skipped_descendants)
        )
    if baseline.status is BaselineStatus.STALE_SKIP:
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                commands=commands,
                observations=observations,
                reason=ReasonCode.STALE_SKIP,
            ),
            baseline,
        )
    if baseline.status is not BaselineStatus.VALID:
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                commands=commands,
                observations=observations,
                reason=ReasonCode.INVALID_RED_BASELINE,
            ),
            baseline,
        )
    head_run = observers.run_item(head_sha, nodeid)
    commands.append(head_run.command)
    observations.extend(
        f"{outcome.nodeid}: {outcome.outcome.value} at head" for outcome in head_run.outcomes
    )
    if head_run.reason is not None:
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                commands=commands,
                observations=observations + ["test capability was unavailable at head"],
                reason=head_run.reason,
            ),
            baseline,
        )
    green = bool(head_run.outcomes) and all(
        outcome.outcome is ItemOutcome.PASSED for outcome in head_run.outcomes
    )
    if not green:
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                commands=commands,
                observations=observations,
                reason=ReasonCode.GREEN_NOT_REACHED,
            ),
            baseline,
        )
    return (
        CriterionEvidence(
            criterion=criterion,
            satisfied=True,
            commands=commands,
            observations=observations,
        ),
        baseline,
    )


def _suite_evidence(
    candidate: Candidate,
    criterion: str,
    *,
    head_sha: str,
    observers: Observers,
    commands: list[str],
    observations: list[str],
    config: PipelineConfig,
    stage: Stage,
) -> CriterionEvidence | None:
    """Return failing suite evidence, or None when the suite is green."""
    if config.ci_evidence_mode is not CiEvidenceMode.LOCAL:
        if observers.run_suite is None:
            return CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                stage=stage,
                commands=commands,
                observations=observations,
                reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            )
        scope = list(candidate.suite_scope)
        suite = observers.run_suite(scope, head_sha)
        commands.append(suite.command)
        observations.append(
            f"Python-Unit suite over {', '.join(scope) or 'default scope'} at {head_sha}: "
            f"{'success' if suite.passed else 'failure'}"
        )
        if suite.reason is not None:
            return CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                stage=stage,
                commands=commands,
                observations=observations,
                reason=suite.reason,
            )
        if not suite.passed:
            observations.extend(f"failing nodeid: {nodeid}" for nodeid in suite.failing_nodeids)
            return CriterionEvidence(
                criterion=criterion,
                satisfied=False,
                stage=stage,
                commands=commands,
                observations=observations,
                reason=ReasonCode.SUITE_REGRESSED,
            )
        if stage != "post_pr":
            return CriterionEvidence(
                criterion=criterion,
                satisfied=None,
                stage=stage,
                commands=commands,
                observations=observations,
                reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
            )
        return None
    if observers.run_suite is None:
        return _unobservable(
            criterion,
            commands,
            "no local checkout was available to run the suite over suite_scope",
        )
    scope = list(candidate.suite_scope)
    suite = observers.run_suite(scope, head_sha)
    commands.append(suite.command)
    observations.append(
        f"suite over {', '.join(scope) or 'default scope'}: "
        f"{'passed' if suite.passed else 'failed'}"
    )
    if suite.passed:
        return None
    observations.extend(f"failing nodeid: {nodeid}" for nodeid in suite.failing_nodeids)
    return CriterionEvidence(
        criterion=criterion,
        satisfied=False,
        commands=commands,
        observations=observations,
        reason=ReasonCode.SUITE_REGRESSED,
    )


def verify_lane1(
    candidate: Candidate,
    *,
    head_sha: str,
    observers: Observers,
    config: PipelineConfig,
    stage: Stage = "pre_pr",
) -> CriterionEvidence:
    """Evaluate LANE 1's alert-absence plus suite-green criterion."""
    criterion = candidate.success_criterion or LANE1_CRITERION
    commands: list[str] = []
    observations: list[str] = []
    alert_stage = (
        "post_pr" if config.lane1_alert_check is Lane1AlertCheck.PR_REF_ALERTS else "pre_pr"
    )
    if stage != alert_stage:
        suite_failure = _suite_evidence(
            candidate,
            criterion,
            head_sha=head_sha,
            observers=observers,
            commands=commands,
            observations=observations,
            config=config,
            stage="post_pr" if stage == "post_pr" else "pre_pr",
        )
        if suite_failure is not None:
            return suite_failure
        observations.append(
            f"alert absence is observed at the {alert_stage} stage under "
            f"{config.lane1_alert_check.value}"
        )
        return CriterionEvidence(
            criterion=criterion,
            satisfied=None,
            stage="pre_pr" if stage == "pre_pr" else "post_pr",
            commands=commands,
            observations=observations,
        )
    if observers.probe_alerts is None:
        return _unobservable(
            criterion,
            commands,
            f"alerts could not be read at the candidate head under "
            f"{config.lane1_alert_check.value}",
        )
    observation = observers.probe_alerts(candidate, head_sha)
    commands.append(observation.command)
    if not observation.available:
        return CriterionEvidence(
            criterion=criterion,
            satisfied=False,
            stage="post_pr" if stage == "post_pr" else "pre_pr",
            commands=commands,
            observations=[observation.detail or "alert analysis did not complete in time"],
            reason=ReasonCode.CRITERION_NOT_MET,
        )
    present = candidate.stable_locator in observation.locators
    observations.append(f"stable_locator {'still present' if present else 'absent'} at {head_sha}")
    if present:
        return CriterionEvidence(
            criterion=criterion,
            satisfied=False,
            stage="post_pr" if stage == "post_pr" else "pre_pr",
            commands=commands,
            observations=observations,
            reason=ReasonCode.ALERT_STILL_PRESENT,
        )
    suite_failure = _suite_evidence(
        candidate,
        criterion,
        head_sha=head_sha,
        observers=observers,
        commands=commands,
        observations=observations,
        config=config,
        stage="post_pr" if stage == "post_pr" else "pre_pr",
    )
    if suite_failure is not None:
        return suite_failure
    return CriterionEvidence(
        criterion=criterion,
        satisfied=True,
        stage="post_pr" if stage == "post_pr" else "pre_pr",
        commands=commands,
        observations=observations,
    )


def verify_lane3(
    candidate: Candidate,
    *,
    head_sha: str,
    observers: Observers,
    config: PipelineConfig,
    stage: Stage = "pre_pr",
) -> CriterionEvidence:
    """Evaluate LANE 3's symbol-absence plus suite-green criterion."""
    criterion = candidate.success_criterion or LANE3_CRITERION
    if observers.probe_symbol is None:
        return _unobservable(
            criterion,
            (),
            "no local checkout was available to re-check the symbol at head",
        )
    observation = observers.probe_symbol(candidate, head_sha)
    commands = [observation.command]
    resolution = "still resolves" if observation.resolves else "no longer resolves"
    observations = [
        f"symbol {resolution} at {head_sha}",
        f"callers: {observation.caller_count}, overrides: {observation.override_count}",
    ]
    observations.extend(f"reference: {reference}" for reference in observation.references)
    if observation.resolves or observation.caller_count or observation.override_count:
        return CriterionEvidence(
            criterion=criterion,
            satisfied=False,
            commands=commands,
            observations=observations,
            reason=ReasonCode.SYMBOL_STILL_REFERENCED,
        )
    suite_failure = _suite_evidence(
        candidate,
        criterion,
        head_sha=head_sha,
        observers=observers,
        commands=commands,
        observations=observations,
        config=config,
        stage="post_pr" if stage == "post_pr" else "pre_pr",
    )
    if suite_failure is not None:
        return suite_failure
    return CriterionEvidence(
        criterion=criterion,
        satisfied=True,
        stage="post_pr" if stage == "post_pr" else "pre_pr",
        commands=commands,
        observations=observations,
    )


def verify_candidate(
    candidate: Candidate,
    *,
    base_sha: str,
    head_sha: str,
    observers: Observers,
    config: PipelineConfig,
    stage: Stage = "pre_pr",
) -> tuple[CriterionEvidence, RedBaselineResult | None]:
    """Evaluate the candidate's lane criterion at the requested stage."""
    if candidate.lane is Lane.SKIPPED_TESTS:
        if stage != "pre_pr":
            criterion = candidate.success_criterion or LANE2_CRITERION
            post_commands: list[str] = []
            post_observations: list[str] = []
            suite_failure = _suite_evidence(
                candidate,
                criterion,
                head_sha=head_sha,
                observers=observers,
                commands=post_commands,
                observations=post_observations,
                config=config,
                stage="post_pr",
            )
            if suite_failure is not None:
                return suite_failure, candidate.red_baseline
            return (
                CriterionEvidence(
                    criterion=criterion,
                    satisfied=True,
                    stage="post_pr",
                    commands=post_commands,
                    observations=post_observations,
                ),
                candidate.red_baseline,
            )
        return verify_lane2(
            candidate,
            base_sha=base_sha,
            head_sha=head_sha,
            observers=observers,
        )
    if candidate.lane is Lane.CODEQL:
        return (
            verify_lane1(
                candidate,
                head_sha=head_sha,
                observers=observers,
                config=config,
                stage=stage,
            ),
            None,
        )
    if stage != "pre_pr":
        criterion = candidate.success_criterion or LANE3_CRITERION
        lane3_commands: list[str] = []
        lane3_observations: list[str] = []
        suite_failure = _suite_evidence(
            candidate,
            criterion,
            head_sha=head_sha,
            observers=observers,
            commands=lane3_commands,
            observations=lane3_observations,
            config=config,
            stage="post_pr",
        )
        if suite_failure is not None:
            return suite_failure, None
        return (
            CriterionEvidence(
                criterion=criterion,
                satisfied=True,
                stage="post_pr",
                commands=lane3_commands,
                observations=lane3_observations,
            ),
            None,
        )
    return (
        verify_lane3(
            candidate,
            head_sha=head_sha,
            observers=observers,
            config=config,
            stage=stage,
        ),
        None,
    )


def post_pr_criterion_pending(candidate: Candidate, config: PipelineConfig) -> bool:
    """Return whether this candidate still owes post-PR criterion evidence."""
    if candidate.lane is Lane.CODEQL and config.lane1_alert_check is Lane1AlertCheck.PR_REF_ALERTS:
        return True
    return config.ci_evidence_mode is not CiEvidenceMode.LOCAL


__all__ = [
    "AlertObservation",
    "AlertProbe",
    "ItemRunResult",
    "ItemRunner",
    "LANE1_CRITERION",
    "LANE2_CRITERION",
    "LANE3_CRITERION",
    "Observers",
    "SuiteResult",
    "SuiteRunner",
    "SymbolObservation",
    "SymbolProbe",
    "declare_success_criterion",
    "post_pr_criterion_pending",
    "verify_candidate",
    "verify_lane1",
    "verify_lane2",
    "verify_lane3",
]
