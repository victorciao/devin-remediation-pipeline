"""Strict data contracts shared by the remediation pipeline modules."""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


class Lane(str, Enum):
    """Remediation source lanes."""

    CODEQL = "codeql"
    SKIPPED_TESTS = "skipped_tests"
    DEPRECATIONS = "deprecations"


class DefinitionKind(str, Enum):
    """Definition shape used by skipped-test candidates."""

    CLASS = "class"
    FUNCTION = "function"


class ReasonCode(str, Enum):
    """Normative reasons used when a candidate or run cannot proceed."""

    TRIGGER_MISSING = "trigger_missing"
    AUTOMATABILITY_LOW = "automatability_low"
    VERIFIABILITY_MISSING = "verifiability_missing"
    OUT_OF_SCOPE_FRONTEND = "out_of_scope_frontend"
    CLASS_SCOPE_TOO_BROAD = "class_scope_too_broad"
    CLASS_BREADTH_UNKNOWN = "class_breadth_unknown"
    BLOCKED_BY_ENCLOSING_SKIP = "blocked_by_enclosing_skip"
    SUPPRESSED_BY_CONTAINMENT = "suppressed_by_containment"
    PUBLIC_API_SURFACE = "public_api_surface"
    INTERNAL_CALLER = "internal_caller"
    NOT_EOL = "not_eol"
    CONDITIONAL_ENVIRONMENT_GUARD = "conditional_environment_guard"
    EXPECTED_FAILURE_XFAIL = "expected_failure_xfail"
    STALE_SKIP = "stale_skip"
    INVALID_RED_BASELINE = "invalid_red_baseline"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BUDGET_OVERFLOW = "budget_overflow"
    TOKEN_CAPABILITY_MISSING = "token_capability_missing"
    CI_EVIDENCE_UNAVAILABLE = "ci_evidence_unavailable"
    CI_CHECK_FAILED = "ci_check_failed"
    DCO_TRAILER_MISSING = "dco_trailer_missing"
    CLOSED_PULL_REQUEST = "closed_pull_request"
    MERGED_EXTERNALLY_UNVERIFIED = "merged_externally_unverified"
    AWAITING_WORKFLOW_APPROVAL = "awaiting_workflow_approval"
    CI_WORKFLOWS_ABSENT = "ci_workflows_absent"
    GUARDRAIL_CLAMPED = "guardrail_clamped"
    SESSION_CEILING = "session_ceiling"
    SESSION_FAILED = "session_failed"
    COLLECTION_ERROR = "collection_error"
    RUBRIC_FACTOR_UNRESOLVED = "rubric_factor_unresolved"
    SESSION_BLOCKED = "session_blocked"
    CRITERION_NOT_MET = "criterion_not_met"
    GREEN_NOT_REACHED = "green_not_reached"
    ALERT_STILL_PRESENT = "alert_still_present"
    SYMBOL_STILL_REFERENCED = "symbol_still_referenced"
    SUITE_REGRESSED = "suite_regressed"
    MANUAL_MERGE_REQUIRED = "manual_merge_required"
    HUMAN_ROUTED = "human_routed"
    ARTIFACT_VALIDATION_FAILED = "artifact_validation_failed"
    LABEL_CAPABILITY_UNAVAILABLE = "label_capability_unavailable"
    MARKER_SEARCH_FAILED = "marker_search_failed"
    MARKER_SEARCH_UNCONFIGURED = "marker_search_unconfigured"


class CandidateState(str, Enum):
    """Lifecycle states in the append-only candidate state store."""

    ENUMERATED = "enumerated"
    GATED = "gated"
    SCORED = "scored"
    DISPATCHING = "dispatching"
    ISSUE_CREATED = "issue_created"
    SESSION_DONE = "session_done"
    VERIFIED = "verified"
    PR_CREATED = "pr_created"
    AWAITING_HUMAN_MERGE = "awaiting_human_merge"
    MERGED = "merged"
    TERMINAL = "terminal"
    BLOCKED_BY_ENCLOSING_SKIP = "blocked_by_enclosing_skip"
    SUPPRESSED_BY_CONTAINMENT = "suppressed_by_containment"
    DEFERRED = "deferred"


class RetryDecision(str, Enum):
    """Resolved decision for a retried Devin session creation."""

    PROCEED = "proceed"
    FATAL_DEDUPE_HIT = "fatal_dedupe_hit"
    PROCEED_ID_DIFFERS = "proceed_id_differs"


class Action(str, Enum):
    """Artifact action selected by tier dispatch."""

    OPEN_PR = "open_pr"
    OPEN_ISSUE = "open_issue"
    LOG_ONLY = "log_only"
    DEFERRED = "deferred"
    HUMAN_REVIEW = "human_review"


class GateName(str, Enum):
    """Named binary gate predicates."""

    TRIGGER_EXISTS = "trigger_exists"
    AUTOMATABILITY = "automatability"
    VERIFIABILITY_EXISTS = "verifiability_exists"


class Tier(str, Enum):
    """Score-derived dispatch tier."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BaselineStatus(str, Enum):
    """Classification of a reviewer red-baseline run."""

    MISSING = "missing"
    VALID = "valid"
    STALE_SKIP = "stale_skip"
    INVALID_RED_BASELINE = "invalid_red_baseline"


class ItemOutcome(str, Enum):
    """Observed outcome for one collected test item."""

    FAILED = "FAILED"
    PASSED = "PASSED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class GateResult(StrictModel):
    """Outcome of one named candidate gate."""

    passed: bool
    reason: ReasonCode | None = None


class ExpectedFailure(StrictModel):
    """Planner-provided four-field red-baseline expectation."""

    nodeid: str = Field(min_length=1)
    exception_type: str = Field(min_length=1)
    message_pattern: str = Field(min_length=1)
    assert_location: str | None = None


class PerItemOutcome(StrictModel):
    """Observed result and signature for one collected test item."""

    nodeid: str = Field(min_length=1)
    outcome: ItemOutcome
    exception_type: str | None = None
    message: str | None = None
    assert_location: str | None = None
    expected_reason_match: bool | None = None


class RedBaselineResult(StrictModel):
    """Aggregate red-baseline result, including multi-item descendants."""

    status: BaselineStatus
    per_item_outcomes: list[PerItemOutcome] = Field(default_factory=list)
    still_skipped_descendants: list[str] = Field(default_factory=list)
    representative_nodeid: str | None = None
    expected_failure: ExpectedFailure | None = None


class MergeMode(str, Enum):
    """Who owns the merge of a high-tier candidate's pull request."""

    AUTO = "auto"
    MANUAL = "manual"


class CheckRunConclusion(StrictModel):
    """One check run observed on a pull-request head."""

    name: str = Field(min_length=1)
    conclusion: str | None = None
    status: str | None = None


class CriterionEvidence(StrictModel):
    """Evidence the orchestrator observed for one candidate's criterion."""

    criterion: str = Field(min_length=1)
    satisfied: bool | None = None
    stage: Literal["pre_pr", "post_pr"] = "pre_pr"
    commands: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    reason: ReasonCode | None = None


class Candidate(StrictModel):
    """A normalized remediation candidate and its current pipeline state."""

    candidate_id: str = Field(min_length=1)
    run_id: str | None = None
    lane: Lane
    repo: str = Field(min_length=1)
    stable_locator: str = Field(min_length=1)
    trigger_exists: bool | None = None
    verifiability_exists: bool | None = None

    # LANE 1 locator and drift payload.
    rule_id: str | None = None
    file_path: str | None = None
    normalized_symbol: str | None = None
    alert_number: int | None = None
    security_severity_level: str | None = None
    rule_precision: str | None = None
    blast_radius: str | None = None
    updated_at_fresh: bool | None = None
    updated_at: datetime | None = None
    position_digest: str | None = None
    region_digest: str | None = None
    region_source: str | None = None
    symbol_relative_offset: int | None = None
    symbol_source: str | None = None
    base_sha: str | None = None
    superseded_by: str | None = None
    supersedes: str | None = None

    # LANE 2 breadth, nesting, and locator payload.
    nodeid: str | None = None
    class_scope: str | None = None
    kind: DefinitionKind | None = None
    enclosed_tests: int | None = Field(default=None, ge=0)
    live_enclosed_tests: int | None = Field(default=None, ge=0)
    parametrized: bool | None = None
    collects_single_item: bool | None = None
    enclosing_skip_nodeid: str | None = None
    related_candidate_id: str | None = None
    skip_reason: str | None = None
    decorator: str | None = None
    resolved_decorator: str | None = None

    # LANE 3 locator and deprecation payload.
    module: str | None = None
    qualname: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
    current_major: int | None = Field(default=None, ge=0)
    caller_count: int | None = Field(default=None, ge=0)
    override_count: int | None = Field(default=None, ge=0)
    targeted_test_signal: str | None = None
    transformation_scope: str | None = None
    scope_is_test_only: bool | None = None
    public_api_surface: bool | None = None
    internal_caller: bool | None = None
    override_surface: bool | None = None
    line: int | None = Field(default=None, ge=1)
    decorator_line: int | None = Field(default=None, ge=1)

    gate_results: dict[GateName, GateResult] = Field(default_factory=dict)
    gate_passed: bool | None = None
    failed_gate: GateName | None = None
    score: float | None = Field(default=None, ge=0)
    business_impact: int | None = Field(default=None, ge=1, le=5)
    verifiability: int | None = Field(default=None, ge=1, le=5)
    automatability: int | None = Field(default=None, ge=1, le=5)
    signal_quality: int | None = Field(default=None, ge=1, le=5)
    risk: int | None = Field(default=None, ge=1, le=5)
    factor_rows: dict[str, str] = Field(default_factory=dict)
    tier: Tier | None = None
    action: Action | None = None
    state: CandidateState = CandidateState.ENUMERATED
    reason: ReasonCode | None = None
    reason_detail: str | None = None
    pr_url: str | None = None
    issue_url: str | None = None
    issue_adopted: bool = False
    merged_at: str | None = None
    merge_verified: bool = False
    auto_merge_requested: bool = False
    ci_evidence_mode: str | None = None
    issue_number: int | None = Field(default=None, ge=1)
    pr_number: int | None = Field(default=None, ge=1)
    head_branch: str | None = None
    head_sha: str | None = None
    artifact_simulated: bool = False
    marker_search_outcome: str | None = None
    auto_merge_eligible: bool | None = None
    labels: list[str] = Field(default_factory=list)
    success_criterion: str | None = None
    criterion_evidence: CriterionEvidence | None = None
    merge_mode: MergeMode | None = None
    suite_scope: list[str] = Field(default_factory=list)
    check_run_conclusions: list[CheckRunConclusion] = Field(default_factory=list)
    session_id: str | None = None
    session_attempts: int = Field(default=0, ge=0)
    test_added: bool | None = None
    test_paths: list[str] = Field(default_factory=list)
    test_author: str | None = None
    test_exempt_reason: ReasonCode | None = None
    expected_failure: ExpectedFailure | None = None
    red_baseline: RedBaselineResult | None = None
    lifted_markers: list[str] = Field(default_factory=list)
    test_nodeid: str | None = None
    claimed_test_nodeid: str | None = None
    claimed_test_paths: list[str] = Field(default_factory=list)
    claimed_suite_scope: list[str] = Field(default_factory=list)
    fix_summary: str | None = None


class EventRecord(StrictModel):
    """Layer 1 per-candidate structured event record."""

    run_id: str = Field(min_length=1)
    lane: Lane
    candidate_id: str = Field(min_length=1)
    gate_passed: bool | None = None
    failed_gate: GateName | None = None
    gate_results: dict[GateName, GateResult] = Field(default_factory=dict)
    score: float | None = Field(default=None, ge=0)
    business_impact: int | None = Field(default=None, ge=1, le=5)
    verifiability: int | None = Field(default=None, ge=1, le=5)
    automatability: int | None = Field(default=None, ge=1, le=5)
    signal_quality: int | None = Field(default=None, ge=1, le=5)
    risk: int | None = Field(default=None, ge=1, le=5)
    factor_rows: dict[str, str] = Field(default_factory=dict)
    tier: Tier | None = None
    action: Action | None = None
    merge_mode: MergeMode | None = None
    session_id: str | None = None
    session_attempts: int = Field(default=0, ge=0)
    success_criterion: str | None = None
    criterion_evidence: CriterionEvidence | None = None
    suite_scope: list[str] = Field(default_factory=list)
    check_run_conclusions: list[CheckRunConclusion] = Field(default_factory=list)
    pr_url: str | None = None
    issue_url: str | None = None
    issue_adopted: bool = False
    issue_number: int | None = Field(default=None, ge=1)
    pr_number: int | None = Field(default=None, ge=1)
    merged_at: str | None = None
    merge_verified: bool = False
    auto_merge_requested: bool = False
    ci_evidence_mode: str | None = None
    test_added: bool | None = None
    test_nodeid: str | None = None
    test_paths: list[str] = Field(default_factory=list)
    test_author: str | None = None
    test_exempt_reason: ReasonCode | None = None
    terminal_outcome: CandidateState | None = None
    reason: ReasonCode | None = None
    reason_detail: str | None = None
    red_baseline: RedBaselineResult | None = None
    enclosed_tests: int | None = Field(default=None, ge=0)
    parametrized: bool | None = None
    collects_single_item: bool | None = None
    lifted_markers: list[str] = Field(default_factory=list)
    related_candidate_id: str | None = None
    marker_search_outcome: str | None = None
    artifact_simulated: bool = False
    attempt: int = Field(default=1, ge=1)
    is_new_session_raw: bool | None = None
    retry_decision: RetryDecision = RetryDecision.PROCEED


class RunEventRecord(StrictModel):
    """Layer 1 run-level capability evidence."""

    event_type: Literal["run_capabilities", "ci_mode_transition", "marker_search_failure"] = (
        "run_capabilities"
    )
    reason_detail: str | None = None
    run_id: str = Field(min_length=1)
    token_login: str | None = None
    token_scopes: list[str] = Field(default_factory=list)
    mode_from: str | None = None
    mode_to: str | None = None
    transition_reason: ReasonCode | None = None


NEEDS_HUMAN_REVIEW_LABEL = "needs-human-review"
