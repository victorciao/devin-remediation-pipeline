"""Strict data contracts shared by the remediation pipeline modules."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and coercion."""

    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


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
    CONDITIONAL_ENVIRONMENT_GUARD = "conditional_environment_guard"
    EXPECTED_FAILURE_XFAIL = "expected_failure_xfail"
    STALE_SKIP = "stale_skip"
    INVALID_RED_BASELINE = "invalid_red_baseline"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    TOKEN_CAPABILITY_MISSING = "token_capability_missing"
    CI_EVIDENCE_UNAVAILABLE = "ci_evidence_unavailable"
    AWAITING_WORKFLOW_APPROVAL = "awaiting_workflow_approval"
    ARTIFACT_DEGRADED = "artifact_degraded"
    GUARDRAIL_CLAMPED = "guardrail_clamped"
    DISAGREEMENT_UNRESOLVED = "disagreement_unresolved"
    NEEDS_HUMAN_REVIEW = "needs-human-review"
    IMPLEMENTER_TEST_EDIT = "implementer_test_edit"
    ROLE_COLLISION = "role_collision"
    SESSION_CEILING_EXCEEDED = "session_ceiling_exceeded"
    COLLECTION_ERROR = "collection_error"


class CandidateState(str, Enum):
    """Lifecycle states in the append-only candidate state store."""

    ENUMERATED = "enumerated"
    GATED = "gated"
    SCORED = "scored"
    DISPATCHING = "dispatching"
    ISSUE_CREATED = "issue_created"
    PR_CREATED = "pr_created"
    CONVERGED = "converged"
    TERMINAL = "terminal"
    COMMENT_CREATED = "comment_created"
    BLOCKED_BY_ENCLOSING_SKIP = "blocked_by_enclosing_skip"
    SUPPRESSED_BY_CONTAINMENT = "suppressed_by_containment"
    DEFERRED = "deferred"
    NEEDS_HUMAN_REVIEW = "needs-human-review"


class Action(str, Enum):
    """Artifact action selected by tier dispatch."""

    OPEN_PR = "open_pr"
    OPEN_ISSUE = "open_issue"
    LOG_ONLY = "log_only"
    DEFERRED = "deferred"
    HUMAN_REVIEW = "human_review"


class GateName(str, Enum):
    """Named gate predicates and lane-specific hard conditions."""

    TRIGGER_EXISTS = "trigger_exists"
    AUTOMATABILITY = "automatability"
    VERIFIABILITY_EXISTS = "verifiability_exists"
    LANE1_SCOPE = "lane1_scope"
    LANE2_BREADTH = "lane2_breadth"
    LANE2_OVERLAP = "lane2_overlap"
    NO_INTERNAL_CALLERS_AND_NO_OVERRIDE_SURFACE = "no_internal_callers_and_no_override_surface"


class Tier(str, Enum):
    """Score-derived dispatch tier."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class BaselineStatus(str, Enum):
    """Classification of a reviewer red-baseline run."""

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


class Candidate(StrictModel):
    """A normalized remediation candidate and its current pipeline state."""

    candidate_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    repo: str = Field(min_length=1)
    stable_locator: str = Field(min_length=1)

    # LANE 1 locator and drift payload.
    rule_id: str | None = None
    file_path: str | None = None
    normalized_symbol: str | None = None
    alert_number: int | None = None
    security_severity_level: str | None = None
    updated_at: datetime | None = None
    position_digest: str | None = None
    region_digest: str | None = None
    symbol_relative_offset: int | None = None
    base_sha: str | None = None
    superseded_by: str | None = None
    supersedes: str | None = None

    # LANE 2 breadth, nesting, and locator payload.
    nodeid: str | None = None
    class_scope: str | None = None
    kind: str | None = None
    enclosed_tests: int | None = Field(default=None, ge=0)
    parametrized: bool | None = None
    collects_single_item: bool | None = None
    enclosing_skip_nodeid: str | None = None
    related_candidate_id: str | None = None
    skip_reason: str | None = None

    # LANE 3 locator and deprecation payload.
    module: str | None = None
    qualname: str | None = None
    deprecated_in: str | None = None
    removed_in: str | None = None
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
    tier: Tier | None = None
    action: Action | None = None
    state: CandidateState = CandidateState.ENUMERATED
    reason: ReasonCode | None = None
    expected_failure: ExpectedFailure | None = None
    red_baseline: RedBaselineResult | None = None


class EventRecord(StrictModel):
    """Layer 1 per-candidate structured event record."""

    run_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
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
    tier: Tier | None = None
    action: Action | None = None
    planner_session_id: str | None = None
    implementer_session_id: str | None = None
    reviewer_session_id: str | None = None
    iterations: int = Field(default=0, ge=0)
    pr_url: str | None = None
    issue_url: str | None = None
    test_added: bool | None = None
    test_paths: list[str] = Field(default_factory=list)
    test_author: str | None = None
    test_exempt_reason: ReasonCode | None = None
    terminal_outcome: CandidateState | None = None
    reason: ReasonCode | None = None
    red_baseline: RedBaselineResult | None = None
    enclosed_tests: int | None = Field(default=None, ge=0)
    parametrized: bool | None = None
    collects_single_item: bool | None = None
    lifted_markers: list[str] = Field(default_factory=list)
    related_candidate_id: str | None = None
    token_login: str | None = None
    token_scopes: list[str] = Field(default_factory=list)


Layer1Event = EventRecord
