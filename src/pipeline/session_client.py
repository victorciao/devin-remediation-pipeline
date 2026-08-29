"""Injected Devin transport, role separation, and retry lifecycle."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.red_baseline import (
    DiffInspection,
    classify_implementer_diff,
    inspect_reviewer_diff,
)
from pipeline.review_loop import (
    FindingSeverity,
    ReviewFinding,
    ReviewIteration,
    ReviewLoopResult,
    review_iteration_from_payload,
    run_review_loop,
)
from pipeline.schemas import (
    Candidate,
    CandidateState,
    EventRecord,
    ReasonCode,
    RetryDecision,
)


class SessionRole(str, Enum):
    """The three non-interchangeable runtime roles."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


class DevinTransport(Protocol):
    """Minimal remote transport required by the session client."""

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Create a remote session."""

    def get(self, path: str) -> Mapping[str, object]:
        """Read a remote session."""


@dataclass(frozen=True)
class RoleLimits:
    """Timeout and per-session ACU limit for one role."""

    session_timeout_s: float = 5400.0
    max_acu_limit: float = 100.0


@dataclass(frozen=True)
class SessionAttempt:
    """Creation evidence, including the raw tri-state retry field."""

    role: SessionRole
    candidate_id: str
    attempt: int
    session_id: str
    is_new_session_raw: bool | None
    retry_decision: RetryDecision


@dataclass(frozen=True)
class SessionSnapshot:
    """Terminal or intermediate response returned by the Devin API."""

    session_id: str
    status_enum: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class RoleRun:
    """Completed role result and its creation evidence."""

    attempt: SessionAttempt
    snapshot: SessionSnapshot


@dataclass(frozen=True)
class OrchestrationResult:
    """Planner result followed by concurrent implementer and reviewer results."""

    planner: RoleRun
    implementer: RoleRun
    reviewer: RoleRun
    review: ReviewLoopResult | None = None


class SessionCeilingError(RuntimeError):
    """Raised when a run exceeds its configured session or cost ceiling."""

    reason = ReasonCode.SESSION_CEILING


class SessionDedupeError(RuntimeError):
    """Raised when an idempotent retry returns an existing session."""


class RoleCollisionError(ConfigError):
    """Raised when two runtime roles receive the same session identity."""

    reason = ReasonCode.ROLE_COLLISION


KNOWN_STATUSES = frozenset(
    {
        "working",
        "blocked",
        "expired",
        "finished",
        "suspend_requested",
        "suspend_requested_frontend",
        "resume_requested",
        "resume_requested_frontend",
        "resumed",
    }
)
TERMINAL_STATUSES = frozenset({"blocked", "expired", "finished"})


def resolve_retry_decision(
    attempt: int,
    session_id: str,
    previous_session_id: str | None,
    is_new_session_raw: bool | None,
) -> RetryDecision:
    """Resolve the §12 tri-state new-session check for a retry."""
    if attempt <= 1 or is_new_session_raw is True:
        return RetryDecision.PROCEED
    if is_new_session_raw is False:
        return RetryDecision.FATAL_DEDUPE_HIT
    if previous_session_id == session_id:
        return RetryDecision.FATAL_DEDUPE_HIT
    return RetryDecision.PROCEED_ID_DIFFERS


def event_with_attempt(
    event: EventRecord,
    attempt: SessionAttempt,
) -> EventRecord:
    """Record raw and resolved retry evidence in a Layer 1 event."""
    return event.model_copy(
        update={
            "attempt": attempt.attempt,
            "is_new_session_raw": attempt.is_new_session_raw,
            "retry_decision": attempt.retry_decision,
        }
    )


def event_with_ceiling(event: EventRecord, error: SessionCeilingError) -> EventRecord:
    """Record a hard session or cost ceiling abort in a Layer 1 event."""
    return event.model_copy(
        update={
            "reason": error.reason,
            "terminal_outcome": CandidateState.TERMINAL,
        }
    )


class SessionClient:
    """Create and poll role sessions through one injectable transport seam."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        transport: DevinTransport | None = None,
        role_limits: Mapping[SessionRole, RoleLimits] | None = None,
        max_sessions: int | None = None,
        max_total_acu: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.mode is Mode.LIVE and transport is None:
            raise ConfigError("live session orchestration requires a transport")
        self._config = config
        self._transport = transport
        self._limits = dict(role_limits or {})
        self._max_sessions = config.max_sessions if max_sessions is None else max_sessions
        self._max_total_acu = config.max_total_acu if max_total_acu is None else max_total_acu
        self._clock = clock
        self._sleeper = sleeper
        self._session_count = 0
        self._total_acu = 0.0
        self._previous: dict[tuple[str, SessionRole], str] = {}
        self._issued_roles: dict[str, SessionRole] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> PipelineConfig:
        """Return the configuration used for this client."""
        return self._config

    def _limit(self, role: SessionRole) -> RoleLimits:
        return self._limits.get(role, RoleLimits())

    def _reserve(self) -> None:
        with self._lock:
            if self._session_count >= self._max_sessions:
                raise SessionCeilingError("per-run session ceiling exceeded")
            self._session_count += 1

    @staticmethod
    def _session_id(response: Mapping[str, object]) -> str:
        value = response.get("session_id", response.get("id"))
        if not isinstance(value, str) or not value:
            raise ValueError("session creation response lacks session_id")
        return value

    @staticmethod
    def _is_new(response: Mapping[str, object]) -> bool | None:
        value = response.get("is_new_session")
        return value if isinstance(value, bool) else None

    def create_session(
        self,
        role: SessionRole,
        candidate_id: str,
        prompt: str,
        *,
        attempt: int = 1,
        structured_output_schema: Mapping[str, object] | None = None,
    ) -> SessionAttempt:
        """Create one role session with idempotency and retry evidence."""
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        self._reserve()
        request_prompt = f"attempt:{attempt}\n{prompt}"
        payload: dict[str, object] = {
            "prompt": request_prompt,
            "idempotent": True,
            "tags": ["devin-remediation", candidate_id, role.value, f"attempt:{attempt}"],
            "max_acu_limit": self._limit(role).max_acu_limit,
        }
        if structured_output_schema is not None:
            payload["structured_output_schema"] = structured_output_schema
        if self._config.mode is Mode.SIMULATE:
            session_id = hashlib.sha256(
                f"{candidate_id}|{role.value}|{attempt}".encode()
            ).hexdigest()[:16]
            response: Mapping[str, object] = {
                "session_id": f"simulate-{session_id}",
                "is_new_session": True,
            }
        else:
            if self._transport is None:
                raise ConfigError("live session orchestration requires a transport")
            response = self._transport.post("/v1/sessions", payload)
        session_id = self._session_id(response)
        previous = self._previous.get((candidate_id, role))
        raw = self._is_new(response)
        decision = resolve_retry_decision(attempt, session_id, previous, raw)
        if decision is RetryDecision.FATAL_DEDUPE_HIT:
            raise SessionDedupeError(
                f"retry returned an existing {role.value} session: {session_id}"
            )
        with self._lock:
            issued_role = self._issued_roles.get(session_id)
            if issued_role is not None and issued_role is not role:
                raise RoleCollisionError(
                    f"session {session_id} was issued to both {issued_role.value} and {role.value}"
                )
            self._issued_roles[session_id] = role
        self._previous[(candidate_id, role)] = session_id
        return SessionAttempt(role, candidate_id, attempt, session_id, raw, decision)

    def poll_session(self, role: SessionRole, session_id: str) -> SessionSnapshot:
        """Poll until the API reports a terminal status or the role times out."""
        if self._config.mode is Mode.SIMULATE:
            output: Mapping[str, object]
            if role is SessionRole.PLANNER:
                output = {"criteria": [], "files_in_scope": [], "out_of_scope": []}
            elif role is SessionRole.IMPLEMENTER:
                output = {
                    "files_changed": [],
                    "criteria_addressed": [],
                    "commands_run": [],
                }
            else:
                output = {
                    "tests": [],
                    "red_baseline": {"observed": []},
                    "green_result": {"passed": True},
                    "diff_reviewed": {
                        "base_sha": "simulate-base",
                        "head_sha": "simulate-head",
                        "files_read": [],
                    },
                    "findings": [],
                }
            return SessionSnapshot(
                session_id,
                "finished",
                {"session_id": session_id, "structured_output": output},
            )
        if self._transport is None:
            raise ConfigError("live session orchestration requires a transport")
        deadline = self._clock() + self._limit(role).session_timeout_s
        while True:
            response = self._transport.get(f"/v1/sessions/{session_id}")
            status = response.get("status_enum", response.get("status"))
            if not isinstance(status, str):
                status = "unknown"
            snapshot = SessionSnapshot(session_id, status, response)
            if status in TERMINAL_STATUSES:
                acu = response.get("acu_used", response.get("acu"))
                used = float(acu) if isinstance(acu, (int, float)) else 0.0
                with self._lock:
                    self._total_acu += used
                    if used > self._limit(role).max_acu_limit:
                        raise SessionCeilingError(f"{role.value} session exceeded max_acu_limit")
                    if self._total_acu > self._max_total_acu:
                        raise SessionCeilingError("per-run ACU ceiling exceeded")
                return snapshot
            if self._clock() >= deadline:
                raise TimeoutError(f"{role.value} session timed out: {session_id}")
            self._sleeper(1.0)


ROLE_OUTPUT_SCHEMAS: Mapping[SessionRole, Mapping[str, object]] = {
    SessionRole.PLANNER: {
        "type": "object",
        "required": ["criteria", "files_in_scope", "out_of_scope"],
        "properties": {
            "criteria": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "statement", "expected_failure", "verify_command"],
                    "properties": {
                        "id": {"type": "string"},
                        "statement": {"type": "string"},
                        "expected_failure": {
                            "type": "object",
                            "required": ["nodeid", "exception_type", "message_pattern"],
                            "properties": {
                                "nodeid": {"type": "string"},
                                "exception_type": {"type": "string"},
                                "message_pattern": {"type": "string"},
                                "assert_location": {"type": "string"},
                            },
                        },
                        "verify_command": {"type": "string"},
                    },
                },
            },
            "files_in_scope": {"type": "array", "items": {"type": "string"}},
            "out_of_scope": {"type": "array", "items": {"type": "string"}},
        },
    },
    SessionRole.IMPLEMENTER: {
        "type": "object",
        "required": ["files_changed", "criteria_addressed", "commands_run"],
        "properties": {
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "criteria_addressed": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
        },
    },
    SessionRole.REVIEWER: {
        "type": "object",
        "required": ["tests", "red_baseline", "green_result", "diff_reviewed", "findings"],
        "properties": {
            "tests": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["path", "nodeid", "criterion_id"],
                    "properties": {
                        "path": {"type": "string"},
                        "nodeid": {"type": "string"},
                        "criterion_id": {"type": ["string", "null"]},
                    },
                },
            },
            "red_baseline": {"type": "object"},
            "green_result": {"type": "object"},
            "diff_reviewed": {
                "type": "object",
                "required": ["base_sha", "head_sha", "files_read"],
                "properties": {
                    "base_sha": {"type": "string"},
                    "head_sha": {"type": "string"},
                    "files_read": {"type": "array", "items": {"type": "string"}},
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["severity", "criterion_id", "note"],
                    "properties": {
                        "severity": {"type": "string"},
                        "criterion_id": {"type": ["string", "null"]},
                        "note": {"type": "string"},
                        "reason": {"type": ["string", "null"]},
                    },
                },
            },
        },
    },
}


class RuntimeOrchestrator:
    """Run three distinct roles with no plan-review or adjudicator role."""

    def __init__(self, client: SessionClient) -> None:
        self._client = client

    def _run(
        self,
        role: SessionRole,
        candidate_id: str,
        prompt: str,
        attempt: int,
    ) -> RoleRun:
        evidence = self._client.create_session(
            role,
            candidate_id,
            prompt,
            attempt=attempt,
            structured_output_schema=ROLE_OUTPUT_SCHEMAS[role],
        )
        return RoleRun(evidence, self._client.poll_session(role, evidence.session_id))

    def run_planner(self, candidate_id: str, prompt: str, *, attempt: int = 1) -> RoleRun:
        """Run the candidate-specific planner session."""
        return self._run(SessionRole.PLANNER, candidate_id, prompt, attempt)

    def run_implementer(
        self,
        candidate_id: str,
        prompt: str,
        *,
        attempt: int = 1,
    ) -> RoleRun:
        """Run the production-only implementer session."""
        return self._run(SessionRole.IMPLEMENTER, candidate_id, prompt, attempt)

    def run_reviewer(self, candidate_id: str, prompt: str, *, attempt: int = 1) -> RoleRun:
        """Run the independent reviewer session."""
        return self._run(SessionRole.REVIEWER, candidate_id, prompt, attempt)

    @staticmethod
    def inspect_implementer_diff(diff_text: str) -> DiffInspection:
        """Apply the production-only diff policy to implementer output."""
        return classify_implementer_diff(diff_text)

    @staticmethod
    def inspect_reviewer_diff(
        diff_text: str,
        candidate: Candidate,
        *,
        lifted_markers: tuple[str, ...] = (),
    ) -> DiffInspection:
        """Apply reviewer ownership and nested-marker policy to reviewer output."""
        return inspect_reviewer_diff(
            diff_text,
            candidate,
            lifted_markers=lifted_markers,
        )

    def run_candidate(
        self,
        candidate_id: str,
        planner_prompt: str,
        implementer_prompt: str,
        reviewer_prompt: str,
        *,
        attempt: int = 1,
        candidate: Candidate | None = None,
    ) -> OrchestrationResult:
        """Run the review loop after a planner and concurrent role join."""
        planner = self.run_planner(candidate_id, planner_prompt, attempt=attempt)

        def concurrent_roles(role_attempt: int) -> OrchestrationResult:
            with ThreadPoolExecutor(max_workers=2) as executor:
                implementer_future = executor.submit(
                    self.run_implementer,
                    candidate_id,
                    implementer_prompt,
                    attempt=role_attempt,
                )
                reviewer_future = executor.submit(
                    self.run_reviewer,
                    candidate_id,
                    reviewer_prompt,
                    attempt=role_attempt,
                )
                implementer = implementer_future.result()
                reviewer = reviewer_future.result()
            return OrchestrationResult(planner, implementer, reviewer)

        def simulation_result(result: OrchestrationResult) -> OrchestrationResult:
            if self._client.config.mode is not Mode.SIMULATE or candidate is None:
                return result
            expected = candidate.expected_failure or {
                "nodeid": candidate.nodeid or candidate.stable_locator,
                "exception_type": "AssertionError",
                "message_pattern": "simulated failure",
            }
            if hasattr(expected, "model_dump"):
                expected_payload = expected.model_dump(mode="json")
            else:
                expected_payload = expected
            kind = hashlib.sha256(candidate.candidate_id.encode()).digest()[0] % 3
            if kind == 0:
                observed = {
                    "nodeid": expected_payload["nodeid"],
                    "outcome": "FAILED",
                    "exception_type": expected_payload["exception_type"],
                    "message": "simulated failure",
                    "assert_location": expected_payload.get("assert_location"),
                }
            elif kind == 1:
                observed = {
                    "nodeid": expected_payload["nodeid"],
                    "outcome": "FAILED",
                    "exception_type": "TypeError",
                    "message": "different simulated failure",
                }
            else:
                observed = {
                    "nodeid": expected_payload["nodeid"],
                    "outcome": "PASSED",
                }
            planner_payload = {
                "criteria": [
                    {
                        "id": "AC-1",
                        "statement": "Apply the remediation.",
                        "expected_failure": expected_payload,
                        "verify_command": "pytest fixtures/simulated_test.py",
                    }
                ],
                "files_in_scope": ["src/simulated_remediation.py"],
                "out_of_scope": ["tests/"],
            }
            implementer_payload = {
                "files_changed": [],
                "criteria_addressed": ["AC-1"],
                "commands_run": ["pytest fixtures/simulated_test.py"],
            }
            reviewer_payload = {
                "tests": [
                    {
                        "path": "fixtures/simulated_test.py",
                        "nodeid": expected_payload["nodeid"],
                        "criterion_id": "AC-1",
                    }
                ],
                "red_baseline": {"observed": observed},
                "green_result": {"passed": True},
                "diff_reviewed": {
                    "base_sha": "simulate-base",
                    "head_sha": "simulate-head",
                    "files_read": ["fixtures/simulated_test.py"],
                },
                "findings": [],
            }
            snapshots = (
                replace(
                    result.planner,
                    snapshot=replace(
                        result.planner.snapshot,
                        payload={
                            **result.planner.snapshot.payload,
                            "structured_output": planner_payload,
                        },
                    ),
                ),
                replace(
                    result.implementer,
                    snapshot=replace(
                        result.implementer.snapshot,
                        payload={
                            **result.implementer.snapshot.payload,
                            "structured_output": implementer_payload,
                        },
                    ),
                ),
                replace(
                    result.reviewer,
                    snapshot=replace(
                        result.reviewer.snapshot,
                        payload={
                            **result.reviewer.snapshot.payload,
                            "structured_output": reviewer_payload,
                        },
                    ),
                ),
            )
            return OrchestrationResult(*snapshots)

        current = simulation_result(concurrent_roles(attempt))

        def output(run: RoleRun) -> Mapping[str, object]:
            structured = run.snapshot.payload.get("structured_output")
            if isinstance(structured, Mapping):
                return structured
            return {}

        def iteration_from(result: OrchestrationResult) -> ReviewIteration:
            iteration = review_iteration_from_payload(
                output(result.planner),
                output(result.reviewer),
                output(result.implementer),
            )
            implementer_payload = output(result.implementer)
            reviewer_payload = output(result.reviewer)
            implementer_diff = implementer_payload.get(
                "committed_diff", implementer_payload.get("diff")
            )
            reviewer_diff = reviewer_payload.get("committed_diff", reviewer_payload.get("diff"))
            findings = list(iteration.findings)
            if isinstance(implementer_diff, str):
                inspection = self.inspect_implementer_diff(implementer_diff)
                if not inspection.accepted:
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "implementer diff violates production-only policy",
                            ReasonCode.IMPLEMENTER_TEST_EDIT,
                        )
                    )
            if candidate is not None and isinstance(reviewer_diff, str):
                inspection = self.inspect_reviewer_diff(
                    reviewer_diff,
                    candidate,
                    lifted_markers=tuple(candidate.lifted_markers),
                )
                if not inspection.accepted:
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "reviewer diff violates reviewer ownership policy",
                        )
                    )
            if findings != list(iteration.findings):
                iteration = ReviewIteration(
                    red_baseline=iteration.red_baseline,
                    green=iteration.green,
                    findings=tuple(findings),
                    planner_criteria=iteration.planner_criteria,
                    reviewer_criteria=iteration.reviewer_criteria,
                    addressed_criteria=iteration.addressed_criteria,
                    failing_test=iteration.failing_test,
                    pre_fix_signature=iteration.pre_fix_signature,
                    fix_rationale=iteration.fix_rationale,
                    diff_reviewed=iteration.diff_reviewed,
                    red_result=iteration.red_result,
                )
            return iteration

        def rerun(role_attempt: int) -> ReviewIteration:
            nonlocal current
            current = simulation_result(concurrent_roles(role_attempt))
            return iteration_from(current)

        review = run_review_loop(self._client.config, iteration_from(current), rerun)
        return OrchestrationResult(
            current.planner,
            current.implementer,
            current.reviewer,
            review,
        )


__all__ = [
    "DevinTransport",
    "OrchestrationResult",
    "ROLE_OUTPUT_SCHEMAS",
    "RoleLimits",
    "RoleRun",
    "RuntimeOrchestrator",
    "SessionAttempt",
    "SessionClient",
    "SessionDedupeError",
    "SessionRole",
    "SessionSnapshot",
    "SessionCeilingError",
    "RoleCollisionError",
    "event_with_attempt",
    "event_with_ceiling",
    "resolve_retry_decision",
]
