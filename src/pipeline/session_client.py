"""Injected Devin transport, role separation, and retry lifecycle."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from enum import Enum
from typing import Protocol

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.prompts import validate_planner_output
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
from pipeline.simulation_fixtures import simulation_result


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


class PlannerOutputError(ValueError):
    """Raised when planner output cannot be shared with the other roles."""


class SessionMessageError(RuntimeError):
    """Raised when a follow-up message cannot be processed by a role session."""


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
        self._accounted_sessions: set[str] = set()
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
        if self._config.role_session_snapshot_id is not None:
            payload["snapshot_id"] = self._config.role_session_snapshot_id
        try:
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
        except Exception:
            with self._lock:
                self._session_count -= 1
            raise
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
                    "committed_diff": "",
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
                    "committed_diff": "",
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
                self._record_terminal_usage(role, session_id, response)
                return snapshot
            if self._clock() >= deadline:
                raise TimeoutError(f"{role.value} session timed out: {session_id}")
            self._sleeper(1.0)

    def _record_terminal_usage(
        self,
        role: SessionRole,
        session_id: str,
        response: Mapping[str, object],
    ) -> None:
        """Account terminal-session ACU exactly once for a returned snapshot."""
        acu = response.get("acu_used", response.get("acu"))
        used = float(acu) if isinstance(acu, (int, float)) else 0.0
        with self._lock:
            if session_id in self._accounted_sessions:
                return
            self._accounted_sessions.add(session_id)
            self._total_acu += used
            if used > self._limit(role).max_acu_limit:
                raise SessionCeilingError(f"{role.value} session exceeded max_acu_limit")
            if self._total_acu > self._max_total_acu:
                raise SessionCeilingError("per-run ACU ceiling exceeded")

    def send_message(self, session_id: str, message: str) -> Mapping[str, object]:
        """Send a follow-up message to an existing role session."""
        if self._config.mode is Mode.SIMULATE:
            return {"detail": "simulation message suppressed"}
        if self._transport is None:
            raise ConfigError("live session orchestration requires a transport")
        response = self._transport.post(f"/v1/sessions/{session_id}/message", {"message": message})
        detail = response.get("detail")
        if isinstance(detail, str) and (
            "not running" in detail.casefold() or "not_running" in detail.casefold()
        ):
            raise SessionMessageError(f"reviewer session {session_id} is not running: {detail}")
        return response

    def poll_session_after_message(
        self,
        role: SessionRole,
        session_id: str,
        previous: SessionSnapshot,
    ) -> SessionSnapshot:
        """Wait for a follow-up message to change a session before accepting its output."""
        if self._config.mode is Mode.SIMULATE:
            return self.poll_session(role, session_id)
        if self._transport is None:
            raise ConfigError("live session orchestration requires a transport")
        deadline = self._clock() + self._limit(role).session_timeout_s
        previous_output = previous.payload.get("structured_output")
        previous_updated = previous.payload.get("updated_at")
        while True:
            response = self._transport.get(f"/v1/sessions/{session_id}")
            status = response.get("status_enum", response.get("status"))
            if not isinstance(status, str):
                status = "unknown"
            structured_output = response.get("structured_output")
            updated_at = response.get("updated_at")
            processed = structured_output != previous_output or (
                isinstance(updated_at, str) and updated_at != previous_updated
            )
            if processed:
                snapshot = SessionSnapshot(session_id, status, response)
                if status in TERMINAL_STATUSES:
                    self._record_terminal_usage(role, session_id, response)
                return snapshot
            if self._clock() >= deadline:
                raise TimeoutError(
                    f"{role.value} session did not process follow-up message: {session_id}"
                )
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
        "required": ["files_changed", "criteria_addressed", "commands_run", "committed_diff"],
        "properties": {
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "criteria_addressed": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "committed_diff": {"type": "string"},
        },
    },
    SessionRole.REVIEWER: {
        "type": "object",
        "required": [
            "tests",
            "red_baseline",
            "green_result",
            "diff_reviewed",
            "findings",
            "committed_diff",
        ],
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
            "committed_diff": {"type": "string"},
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
        implementer_prompt: str | None,
        reviewer_prompt: str | None,
        *,
        attempt: int = 1,
        candidate: Candidate | None = None,
        head_sha_resolver: Callable[[], str | None] | None = None,
        prompt_factory: (
            Callable[[Mapping[str, object]], tuple[str, str, Callable[[str], str]]] | None
        ) = None,
    ) -> OrchestrationResult:
        """Run the review loop after a planner and concurrent role join."""
        planner = self.run_planner(candidate_id, planner_prompt, attempt=attempt)

        def concurrent_roles(role_attempt: int) -> OrchestrationResult:
            with ThreadPoolExecutor(max_workers=2) as executor:
                implementer_future = executor.submit(
                    self.run_implementer,
                    candidate_id,
                    resolved_implementer_prompt,
                    attempt=role_attempt,
                )
                reviewer_future = executor.submit(
                    self.run_reviewer,
                    candidate_id,
                    resolved_reviewer_prompt,
                    attempt=role_attempt,
                )
                try:
                    implementer = implementer_future.result()
                    reviewer = reviewer_future.result()
                except Exception:
                    implementer_future.cancel()
                    reviewer_future.cancel()
                    raise
            return OrchestrationResult(planner, implementer, reviewer)

        def output(run: RoleRun) -> Mapping[str, object]:
            structured = run.snapshot.payload.get("structured_output")
            return structured if isinstance(structured, Mapping) else {}

        planner_output = output(planner)
        if prompt_factory is not None:
            try:
                validate_planner_output(planner_output)
            except ValueError as exc:
                raise PlannerOutputError(str(exc)) from exc
            implementer_prompt, reviewer_prompt, phase_b_prompt = prompt_factory(planner_output)
        elif self._client.config.mode is Mode.LIVE:
            raise PlannerOutputError("missing planner prompt factory")
        else:
            if implementer_prompt is None or reviewer_prompt is None:
                raise ValueError("implementer and reviewer prompts are required without a factory")
            phase_b_prompt = None
        if implementer_prompt is None or reviewer_prompt is None:
            raise ValueError("implementer and reviewer prompts are required")
        resolved_implementer_prompt = implementer_prompt
        resolved_reviewer_prompt = reviewer_prompt

        current = (
            simulation_result(concurrent_roles(attempt), candidate)
            if self._client.config.mode is Mode.SIMULATE and candidate is not None
            else concurrent_roles(attempt)
        )

        def iteration_from(result: OrchestrationResult) -> ReviewIteration:
            nonlocal candidate
            if candidate is not None and head_sha_resolver is not None:
                resolved_head_sha = head_sha_resolver()
                if resolved_head_sha is not None:
                    candidate = candidate.model_copy(update={"head_sha": resolved_head_sha})
            iteration = review_iteration_from_payload(
                output(result.planner),
                output(result.reviewer),
                output(result.implementer),
            )
            implementer_payload = output(result.implementer)
            reviewer_payload = output(result.reviewer)
            implementer_diff = implementer_payload.get("committed_diff")
            reviewer_diff = reviewer_payload.get("committed_diff")
            findings = list(iteration.findings)
            implementer_inspection: DiffInspection | None = None
            if isinstance(implementer_diff, str):
                implementer_inspection = self.inspect_implementer_diff(implementer_diff)
                if not implementer_inspection.accepted:
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "implementer diff violates production-only policy",
                            ReasonCode.IMPLEMENTER_TEST_EDIT,
                        )
                    )
                raw_files_changed = implementer_payload.get("files_changed")
                files_changed = (
                    {path for path in raw_files_changed if isinstance(path, str)}
                    if isinstance(raw_files_changed, Sequence)
                    and not isinstance(raw_files_changed, str)
                    else set()
                )
                if files_changed and (
                    not implementer_inspection.changed_paths
                    or not files_changed <= set(implementer_inspection.changed_paths)
                ):
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "implementer files_changed does not match committed_diff",
                            ReasonCode.DISAGREEMENT_UNRESOLVED,
                        )
                    )
                raw_criteria = implementer_payload.get("criteria_addressed")
                raw_commands = implementer_payload.get("commands_run")
                if not implementer_diff.strip() and (
                    (
                        isinstance(raw_criteria, Sequence)
                        and not isinstance(raw_criteria, str)
                        and bool(raw_criteria)
                    )
                    or (
                        isinstance(raw_commands, Sequence)
                        and not isinstance(raw_commands, str)
                        and bool(raw_commands)
                    )
                ):
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "implementer claims work without a committed diff",
                            ReasonCode.DISAGREEMENT_UNRESOLVED,
                        )
                    )
            reviewer_inspection: DiffInspection | None = None
            if candidate is not None and isinstance(reviewer_diff, str):
                reviewer_inspection = self.inspect_reviewer_diff(
                    reviewer_diff,
                    candidate,
                    lifted_markers=tuple(candidate.lifted_markers),
                )
                if not reviewer_inspection.accepted:
                    findings.append(
                        ReviewFinding(
                            FindingSeverity.BLOCKING,
                            None,
                            "reviewer diff violates reviewer ownership policy",
                        )
                    )
            diff_reviewed = (
                iteration.diff_reviewed
                and isinstance(implementer_diff, str)
                and isinstance(reviewer_diff, str)
            )
            raw_diff_reviewed = reviewer_payload.get("diff_reviewed")
            if diff_reviewed and isinstance(raw_diff_reviewed, Mapping):
                files_read = raw_diff_reviewed.get("files_read")
                read_paths = (
                    {path for path in files_read if isinstance(path, str)}
                    if isinstance(files_read, Sequence) and not isinstance(files_read, str)
                    else set()
                )
                changed_paths = (
                    set(implementer_inspection.changed_paths)
                    if implementer_inspection is not None
                    else set()
                )
                base_sha = raw_diff_reviewed.get("base_sha")
                head_sha = raw_diff_reviewed.get("head_sha")
                expected_base = candidate.base_sha if candidate is not None else None
                expected_head = candidate.head_sha if candidate is not None else None
                diff_reviewed = (
                    isinstance(base_sha, str)
                    and isinstance(head_sha, str)
                    and expected_base is not None
                    and base_sha == expected_base
                    and expected_head is not None
                    and head_sha == expected_head
                    and changed_paths <= read_paths
                )
            else:
                diff_reviewed = False
            if (
                candidate is not None
                and candidate.base_sha is not None
                and candidate.head_sha is not None
                and candidate.base_sha == candidate.head_sha
            ):
                findings.append(
                    ReviewFinding(
                        FindingSeverity.BLOCKING,
                        None,
                        "candidate branch has no commits beyond base",
                        ReasonCode.DISAGREEMENT_UNRESOLVED,
                    )
                )
            if findings != list(iteration.findings) or diff_reviewed != iteration.diff_reviewed:
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
                    diff_reviewed=diff_reviewed,
                    red_result=iteration.red_result,
                )
            return iteration

        phase_b_attempted: set[str] = set()

        def phase_b(result: OrchestrationResult) -> OrchestrationResult:
            reviewer_session_id = result.reviewer.attempt.session_id
            if (
                reviewer_session_id in phase_b_attempted
                or self._client.config.mode is Mode.SIMULATE
            ):
                return result
            if output(result.reviewer).get("diff_reviewed") not in (None, False):
                return result
            raw_diff = output(result.implementer).get("committed_diff")
            committed_diff = raw_diff if isinstance(raw_diff, str) else ""
            if phase_b_prompt is None:
                raise PlannerOutputError("missing reviewer phase-B prompt factory")
            self._client.send_message(
                reviewer_session_id,
                phase_b_prompt(committed_diff),
            )
            updated = self._client.poll_session_after_message(
                SessionRole.REVIEWER,
                reviewer_session_id,
                result.reviewer.snapshot,
            )
            phase_b_attempted.add(reviewer_session_id)
            return replace(result, reviewer=replace(result.reviewer, snapshot=updated))

        current = phase_b(current)

        def rerun(role_attempt: int) -> ReviewIteration:
            nonlocal current
            if current.reviewer.attempt.session_id in phase_b_attempted and not output(
                current.reviewer
            ).get("diff_reviewed"):
                return iteration_from(current)
            next_attempt = max(
                role_attempt + 1,
                current.implementer.attempt.attempt + 1,
                current.reviewer.attempt.attempt + 1,
            )
            current = (
                simulation_result(concurrent_roles(next_attempt), candidate)
                if self._client.config.mode is Mode.SIMULATE and candidate is not None
                else concurrent_roles(next_attempt)
            )
            current = phase_b(current)
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
    "SessionMessageError",
    "SessionRole",
    "SessionSnapshot",
    "SessionCeilingError",
    "RoleCollisionError",
    "PlannerOutputError",
    "event_with_attempt",
    "event_with_ceiling",
    "resolve_retry_decision",
]
