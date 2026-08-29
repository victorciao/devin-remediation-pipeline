"""Typed accessors for the production interfaces required by the §17 test plan.

Every module under ``src/pipeline`` other than ``schemas.py`` and ``config.py`` is a stub at
the time these tests were authored, so importing their names directly would make the whole
suite un-typecheckable.  Each Protocol below is therefore the interface the implementation
plan implies, and the accessor functions resolve it at call time.  A missing name surfaces as
an ``AttributeError`` inside the test that needs it — that is the intended red signal for the
implementer, and the Protocols are the contract to build against.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

from pipeline.config import CiEvidenceMode, IssueSink, PipelineConfig
from pipeline.schemas import (
    Candidate,
    CandidateState,
    EventRecord,
    ExpectedFailure,
    Lane,
    PerItemOutcome,
    ReasonCode,
    RedBaselineResult,
    RetryDecision,
)

# --------------------------------------------------------------------------------------
# Structural shapes exchanged with the pipeline
# --------------------------------------------------------------------------------------


class ReviewFindingLike(Protocol):
    """A code-review finding from the §9 severity taxonomy."""

    severity: str
    criterion_id: str | None
    note: str
    resolved: bool


class PreflightResult(Protocol):
    """Outcome of the §0d/§7 dispatch preflight against the target repository."""

    ok: bool
    issue_sink: IssueSink
    reason: ReasonCode | None
    aborted_before_write: bool


class ArtifactResult(Protocol):
    """Result of the §7 dual-artifact creation sequence."""

    issue_number: int | None
    issue_url: str | None
    pr_url: str | None
    state: CandidateState
    operations: Sequence[str]


class ValidationResult(Protocol):
    """Result of validating a rendered body against a locked template."""

    valid: bool
    missing_sections: Sequence[str]
    out_of_order_sections: Sequence[str]


class Rubrics(Protocol):
    """Normalized per-lane rubric tables loaded from ``config/rubrics.yaml``."""

    lanes: Mapping[Lane, Mapping[str, Mapping[str, int]]]

    def value(self, lane: Lane, factor: str, observation: str) -> int: ...


class DriftMatch(Protocol):
    """Outcome of the §14.1 two-condition drift match."""

    linked: bool
    prior_candidate_id: str | None
    reason: str | None


class SkipRecord(Protocol):
    """One enumerated or excluded LANE 2 decorator instance."""

    path: str
    line: int
    decorator_line: int
    symbol: str
    resolved_decorator: str
    reason: str | None
    kind: str
    class_scope: str | None
    nodeid: str | None
    enclosed_tests: int
    parametrized: int
    collects_single_item: int
    enclosing_skip_nodeid: str | None
    excluded_reason: ReasonCode | None


class SkipEnumeration(Protocol):
    """Included and excluded LANE 2 decorator instances for a tree."""

    included: Sequence[SkipRecord]
    excluded: Sequence[SkipRecord]


class DiffClassification(Protocol):
    """Result of the §9.3 role-aware diff classifier."""

    allowed: bool
    reason: ReasonCode | None


class CriterionMapping(Protocol):
    """Result of checking reviewer tests against planner acceptance criteria."""

    ok: bool
    unmapped_nodeids: Sequence[str]
    reason: ReasonCode | None


class ReviewOutcome(Protocol):
    """Terminal outcome of the §9 code-review loop for one candidate."""

    converged: bool
    iterations: int
    state: CandidateState
    reason: ReasonCode | None
    auto_merge_eligible: bool
    labels: Sequence[str]


class CiModeTransition(Protocol):
    """Result of the §10.1 one-way ``local -> github`` re-resolution."""

    mode: CiEvidenceMode
    transitioned: bool
    reason: ReasonCode | None


class CiWaitResult(Protocol):
    """Result of waiting ``ci_wait_timeout_s`` for required contexts."""

    mode: CiEvidenceMode
    reason: ReasonCode | None
    auto_merge_eligible: bool


class CapabilityReport(Protocol):
    """Outcome of the §3 0d target-repository capability probe."""

    has_issues: bool
    ci_evidence_mode: CiEvidenceMode
    alert_source: str
    auto_merge_enabled: bool
    reasons: Sequence[ReasonCode]
    token_login: str | None
    token_scopes: Sequence[str]


class KpiRollup(Protocol):
    """Layer 3 rolling KPI rollup."""

    merge_rate: float | None
    session_failure_rate: float | None
    test_inclusion_rate: float | None
    criterion_coverage_rate: float | None
    expected_reason_match_rate: float | None
    disagreement_unresolved_rate: float | None
    burndown: Mapping[str, object]
    alerts: Sequence[str]


class RunResult(Protocol):
    """Result of one end-to-end SIMULATE run."""

    run_id: str
    candidates: Sequence[Candidate]
    events: Sequence[EventRecord]
    report_path: Path


# --------------------------------------------------------------------------------------
# Module protocols
# --------------------------------------------------------------------------------------


class AutoMergeDecision(Protocol):
    """Computed auto-merge eligibility and the labels that follow from it."""

    eligible: bool
    reason: ReasonCode | None
    labels: Sequence[str]


class _AutoMergeEligible(Protocol):
    def __call__(
        self,
        candidate: Candidate,
        config: PipelineConfig,
        *,
        findings: Sequence[ReviewFindingLike] = ...,
        ci_green: bool = ...,
        test_added: bool = ...,
        existing_suite_green: bool = ...,
    ) -> AutoMergeDecision: ...


class _CreateArtifacts(Protocol):
    def __call__(
        self,
        candidate: Candidate,
        config: PipelineConfig,
        *,
        client: object,
        pr_title: str,
        pr_body: str,
        issue_body: str,
        reports_dir: Path | None = ...,
    ) -> ArtifactResult: ...


class _Preflight(Protocol):
    def __call__(self, config: PipelineConfig, *, client: object) -> PreflightResult: ...


class _RunPipeline(Protocol):
    def __call__(
        self,
        config: PipelineConfig,
        *,
        client: object,
        workdir: Path,
    ) -> RunResult: ...


class DispatchModule(Protocol):
    auto_merge_eligible: _AutoMergeEligible
    preflight: _Preflight
    create_artifacts: _CreateArtifacts
    run_pipeline: _RunPipeline


class _DriftMatchFn(Protocol):
    def __call__(
        self,
        candidate: Candidate,
        *,
        scan: Sequence[Candidate],
        state_rows: Sequence[Candidate],
    ) -> DriftMatch: ...


class DedupeModule(Protocol):
    compute_candidate_id: Callable[[Lane, str, str], str]
    position_digest: Callable[[Mapping[str, int]], str]
    region_digest: Callable[[str], str]
    marker: Callable[[str], str]
    drift_match: _DriftMatchFn
    load_state: Callable[[Path], list[Candidate]]
    append_state: Callable[[Path, Candidate], None]


class _RenderPrBody(Protocol):
    def __call__(
        self,
        candidate: Candidate,
        *,
        implementation_plan: str,
        tests: str,
        issue_number: int | None = ...,
    ) -> str: ...


class _RenderIssueBody(Protocol):
    def __call__(self, candidate: Candidate, *, pr_url: str | None = ...) -> str: ...


class RenderModule(Protocol):
    render_pr_body: _RenderPrBody
    render_pr_title: Callable[[Candidate], str]
    render_issue_body: _RenderIssueBody
    select_issue_template: Callable[[Candidate], str]
    validate_pr_body: Callable[[str], ValidationResult]
    validate_issue_body: Callable[[str, str], ValidationResult]
    load_pr_title_regex: Callable[[Path], re.Pattern[str]]


class _ComputeKpis(Protocol):
    def __call__(
        self,
        events: Sequence[EventRecord],
        *,
        config: PipelineConfig,
        baseline: Mapping[str, object] | None = ...,
        merge_outcomes: Sequence[str] = ...,
        session_outcomes: Sequence[str] = ...,
    ) -> KpiRollup: ...


class KpisModule(Protocol):
    compute_kpis: _ComputeKpis
    render_markdown: Callable[[KpiRollup], str]


class EventsModule(Protocol):
    append_event: Callable[[Path, EventRecord], None]
    read_events: Callable[[Path], list[EventRecord]]


class ReportModule(Protocol):
    render_run_report: Callable[[Sequence[EventRecord], PipelineConfig], str]


class _EnumerateCodeqlCandidates(Protocol):
    def __call__(
        self,
        alerts: Sequence[Mapping[str, object]],
        config: PipelineConfig,
    ) -> list[Candidate]: ...


class CodeqlLaneModule(Protocol):
    load_alerts: Callable[[Path], list[Mapping[str, object]]]
    enumerate_candidates: _EnumerateCodeqlCandidates


class _ToLane2Candidates(Protocol):
    def __call__(
        self,
        enumeration: SkipEnumeration,
        config: PipelineConfig,
    ) -> list[Candidate]: ...


class SkippedTestsLaneModule(Protocol):
    enumerate_tree: Callable[[Path], SkipEnumeration]
    to_candidates: _ToLane2Candidates


class _EnumerateDeprecations(Protocol):
    def __call__(
        self,
        records: Sequence[Mapping[str, object]],
        config: PipelineConfig,
        *,
        current_major: int,
    ) -> list[Candidate]: ...


class DeprecationsLaneModule(Protocol):
    resolve_current_major: Callable[[str], int]
    enumerate_candidates: _EnumerateDeprecations
    dropped_candidates: _EnumerateDeprecations


class _ResolveRetry(Protocol):
    def __call__(
        self,
        *,
        attempt: int,
        is_new_session: bool | None,
        session_id: str,
        previous_session_id: str | None,
    ) -> RetryDecision: ...


class _ClassifyDiff(Protocol):
    def __call__(self, diff: str, *, role: str) -> DiffClassification: ...


class _ClassifyRedBaseline(Protocol):
    def __call__(
        self,
        expected: ExpectedFailure,
        outcomes: Sequence[PerItemOutcome],
        *,
        descendant_marker_nodeids: Sequence[str] = ...,
        lifted_markers: Sequence[str] = ...,
        enclosing_skip_nodeid: str | None = ...,
    ) -> RedBaselineResult: ...


class _RunReviewLoop(Protocol):
    def __call__(
        self,
        candidate: Candidate,
        config: PipelineConfig,
        *,
        rounds: Sequence[Sequence[ReviewFindingLike]],
        ci_green: bool = ...,
        test_added: bool = ...,
    ) -> ReviewOutcome: ...


class _ValidateCriterionMapping(Protocol):
    def __call__(
        self,
        planner_output: Mapping[str, object],
        reviewer_output: Mapping[str, object],
    ) -> CriterionMapping: ...


class _AssertDistinctRoles(Protocol):
    def __call__(
        self,
        *,
        planner_session_id: str,
        implementer_session_id: str,
        reviewer_session_id: str,
    ) -> None: ...


class _EnforceSessionCeiling(Protocol):
    def __call__(self, *, used: int, requested: int, ceiling: int) -> None: ...


class SessionClientModule(Protocol):
    resolve_retry: _ResolveRetry
    classify_diff: _ClassifyDiff
    classify_red_baseline: _ClassifyRedBaseline
    run_review_loop: _RunReviewLoop
    validate_criterion_mapping: _ValidateCriterionMapping
    assert_distinct_roles: _AssertDistinctRoles
    enforce_session_ceiling: _EnforceSessionCeiling
    SessionCeilingExceeded: type[Exception]


class _ProbeCapabilities(Protocol):
    def __call__(self, config: PipelineConfig, *, client: object) -> CapabilityReport: ...


class _MaybeUpgradeCiMode(Protocol):
    def __call__(
        self,
        current: CiEvidenceMode,
        *,
        reported_contexts: Sequence[str],
        awaiting_workflow_approval: bool = ...,
        already_upgraded: bool = ...,
    ) -> CiModeTransition: ...


class _WaitForRequiredContexts(Protocol):
    def __call__(
        self,
        config: PipelineConfig,
        *,
        client: object,
        elapsed_s: int,
        reported_contexts: Sequence[str] = ...,
    ) -> CiWaitResult: ...


class _RequestWithBackoff(Protocol):
    def __call__(
        self,
        call: Callable[[], object],
        *,
        sleep: Callable[[float], None],
        now: Callable[[], float],
        max_retries: int = ...,
    ) -> object: ...


class GithubClientModule(Protocol):
    probe_capabilities: _ProbeCapabilities
    maybe_upgrade_ci_mode: _MaybeUpgradeCiMode
    wait_for_required_contexts: _WaitForRequiredContexts
    request_with_backoff: _RequestWithBackoff
    REQUIRED_CONTEXTS: Sequence[str]


def _module(name: str) -> object:
    return importlib.import_module(name)


def dispatch() -> DispatchModule:
    """Return ``pipeline.dispatch`` typed as its §6/§7 contract."""
    return cast(DispatchModule, _module("pipeline.dispatch"))


def dedupe() -> DedupeModule:
    """Return ``pipeline.dedupe`` typed as its §14.1 contract."""
    return cast(DedupeModule, _module("pipeline.dedupe"))


def render() -> RenderModule:
    """Return ``pipeline.templates.render`` typed as its §8 contract."""
    return cast(RenderModule, _module("pipeline.templates.render"))


def kpis() -> KpisModule:
    """Return ``pipeline.observability.kpis`` typed as its §11 contract."""
    return cast(KpisModule, _module("pipeline.observability.kpis"))


def events() -> EventsModule:
    """Return ``pipeline.observability.events`` typed as its §11 contract."""
    return cast(EventsModule, _module("pipeline.observability.events"))


def report() -> ReportModule:
    """Return ``pipeline.observability.report`` typed as its §11 contract."""
    return cast(ReportModule, _module("pipeline.observability.report"))


def codeql_lane() -> CodeqlLaneModule:
    """Return ``pipeline.lanes.codeql`` typed as its §5 contract."""
    return cast(CodeqlLaneModule, _module("pipeline.lanes.codeql"))


def skipped_tests_lane() -> SkippedTestsLaneModule:
    """Return ``pipeline.lanes.skipped_tests`` typed as its §5 contract."""
    return cast(SkippedTestsLaneModule, _module("pipeline.lanes.skipped_tests"))


def deprecations_lane() -> DeprecationsLaneModule:
    """Return ``pipeline.lanes.deprecations`` typed as its §5 contract."""
    return cast(DeprecationsLaneModule, _module("pipeline.lanes.deprecations"))


def session_client() -> SessionClientModule:
    """Return ``pipeline.session_client`` typed as its §9/§12 contract."""
    return cast(SessionClientModule, _module("pipeline.session_client"))


def github_client() -> GithubClientModule:
    """Return ``pipeline.github_client`` typed as its §10 contract."""
    return cast(GithubClientModule, _module("pipeline.github_client"))
