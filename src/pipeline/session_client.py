"""Injected Devin transport for the one session a dispatched candidate gets."""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.prompts import FIX_OUTPUT_SCHEMA
from pipeline.schemas import (
    Candidate,
    CandidateState,
    EventRecord,
    ReasonCode,
    RetryDecision,
)
from pipeline.simulation_fixtures import simulated_fix_output


class DevinTransport(Protocol):
    """Minimal remote transport required by the session client."""

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Create a remote session."""

    def get(self, path: str) -> Mapping[str, object]:
        """Read a remote session."""


@dataclass(frozen=True)
class SessionLimits:
    """Timeout and per-session ACU limit for one candidate session."""

    session_timeout_s: float = 5400.0
    max_acu_limit: float = 100.0


@dataclass(frozen=True)
class SessionAttempt:
    """Creation evidence, including the raw tri-state retry field."""

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
class FixOutput:
    """The validated §9 structured output of one candidate session."""

    files_changed: tuple[str, ...]
    test_nodeid: str | None
    test_paths: tuple[str, ...]
    verify_command: str
    head_sha: str
    suite_scope: tuple[str, ...]
    fix_summary: str
    testing_notes: str
    criterion_notes: str
    feasible: bool
    infeasible_reason: str | None


@dataclass(frozen=True)
class SessionRun:
    """Completed session result, its creation evidence and its validated output."""

    attempt: SessionAttempt
    snapshot: SessionSnapshot
    output: FixOutput


class SessionCeilingError(RuntimeError):
    """Raised when a run exceeds its configured session or cost ceiling."""

    reason = ReasonCode.SESSION_CEILING


class SessionDedupeError(RuntimeError):
    """Raised when an idempotent retry returns an existing session."""

    reason = ReasonCode.SESSION_FAILED


class SessionBlockedError(RuntimeError):
    """Raised when a session stops without producing the required output."""

    reason = ReasonCode.SESSION_BLOCKED


class SessionOutputError(ValueError):
    """Raised when a terminal session's structured output is unusable."""

    reason = ReasonCode.SESSION_BLOCKED


class SessionInfeasibleError(RuntimeError):
    """Raised when a session answers that the candidate is not feasible."""

    reason = ReasonCode.SESSION_FAILED

    def __init__(self, message: str, *, output: FixOutput) -> None:
        super().__init__(message)
        self.output = output


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
    """Resolve the §9 tri-state new-session check for a retry."""
    if attempt <= 1 or is_new_session_raw is True:
        return RetryDecision.PROCEED
    if is_new_session_raw is False:
        return RetryDecision.FATAL_DEDUPE_HIT
    if previous_session_id == session_id:
        return RetryDecision.FATAL_DEDUPE_HIT
    return RetryDecision.PROCEED_ID_DIFFERS


def event_with_attempt(event: EventRecord, attempt: SessionAttempt) -> EventRecord:
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


def _required_keys() -> tuple[str, ...]:
    required = FIX_OUTPUT_SCHEMA.get("required")
    if not isinstance(required, Sequence) or isinstance(required, str):
        raise SessionOutputError("fix output schema declares no required keys")
    return tuple(key for key in required if isinstance(key, str))


def has_required_output(response: Mapping[str, object]) -> bool:
    """Check that a terminal session returned every required §9 key."""
    structured = response.get("structured_output")
    if not isinstance(structured, Mapping):
        return False
    return all(key in structured for key in _required_keys())


def _string_sequence(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise SessionOutputError(f"fix output field must be a list of strings: {field}")
    return tuple(item for item in value if isinstance(item, str))


def validate_fix_output(structured: Mapping[str, object]) -> FixOutput:
    """Validate one session's structured output against the §9 schema."""
    missing = [key for key in _required_keys() if key not in structured]
    if missing:
        raise SessionOutputError(f"fix output is missing keys: {', '.join(missing)}")
    test_nodeid = structured["test_nodeid"]
    if test_nodeid is not None and not isinstance(test_nodeid, str):
        raise SessionOutputError("fix output field must be a string or null: test_nodeid")
    infeasible_reason = structured["infeasible_reason"]
    if infeasible_reason is not None and not isinstance(infeasible_reason, str):
        raise SessionOutputError("fix output field must be a string or null: infeasible_reason")
    feasible = structured["feasible"]
    if not isinstance(feasible, bool):
        raise SessionOutputError("fix output field must be a boolean: feasible")
    texts: dict[str, str] = {}
    for field in ("verify_command", "head_sha", "fix_summary", "testing_notes", "criterion_notes"):
        value = structured[field]
        if not isinstance(value, str):
            raise SessionOutputError(f"fix output field must be a string: {field}")
        texts[field] = value
    if feasible and not texts["head_sha"].strip():
        raise SessionOutputError("fix output field must be a non-empty string: head_sha")
    return FixOutput(
        files_changed=_string_sequence(structured["files_changed"], "files_changed"),
        test_nodeid=test_nodeid,
        test_paths=_string_sequence(structured["test_paths"], "test_paths"),
        verify_command=texts["verify_command"],
        head_sha=texts["head_sha"],
        suite_scope=_string_sequence(structured["suite_scope"], "suite_scope"),
        fix_summary=texts["fix_summary"],
        testing_notes=texts["testing_notes"],
        criterion_notes=texts["criterion_notes"],
        feasible=feasible,
        infeasible_reason=infeasible_reason,
    )


class SessionClient:
    """Create and poll candidate sessions through one injectable transport seam."""

    def __init__(
        self,
        config: PipelineConfig,
        *,
        transport: DevinTransport | None = None,
        limits: SessionLimits | None = None,
        max_sessions: int | None = None,
        max_total_acu: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if config.mode is Mode.LIVE and transport is None:
            raise ConfigError("live session orchestration requires a transport")
        self._config = config
        self._transport = transport
        self._limits = limits or SessionLimits(session_timeout_s=config.session_timeout_s)
        self._max_sessions = config.max_sessions if max_sessions is None else max_sessions
        self._max_total_acu = config.max_total_acu if max_total_acu is None else max_total_acu
        self._clock = clock
        self._sleeper = sleeper
        self._session_count = 0
        self._total_acu = 0.0
        self._previous: dict[str, str] = {}
        self._accounted_acu: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> PipelineConfig:
        """Return the configuration used for this client."""
        return self._config

    @property
    def sessions_created(self) -> int:
        """Return how many sessions this run has created."""
        with self._lock:
            return self._session_count

    @property
    def limits(self) -> SessionLimits:
        """Return the per-session timeout and ACU limit."""
        return self._limits

    def _reserve(self) -> None:
        with self._lock:
            if self._session_count >= self._max_sessions:
                raise SessionCeilingError("per-run session ceiling exceeded")
            if self._total_acu + self._limits.max_acu_limit > self._max_total_acu:
                raise SessionCeilingError("projected per-run ACU ceiling exceeded")
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
        candidate_id: str,
        prompt: str,
        *,
        attempt: int = 1,
    ) -> SessionAttempt:
        """Create one candidate session with idempotency and retry evidence."""
        if attempt < 1:
            raise ValueError("attempt must be at least one")
        self._reserve()
        payload: dict[str, object] = {
            "prompt": prompt,
            "idempotent": True,
            "tags": ["devin-remediation", candidate_id, f"attempt:{attempt}"],
            "max_acu_limit": self._limits.max_acu_limit,
            "session_timeout_s": self._limits.session_timeout_s,
            "structured_output_schema": FIX_OUTPUT_SCHEMA,
        }
        if self._config.role_session_snapshot_id is not None:
            payload["snapshot_id"] = self._config.role_session_snapshot_id
        try:
            if self._config.mode is Mode.SIMULATE:
                digest = hashlib.sha256(f"{candidate_id}|{attempt}".encode()).hexdigest()[:16]
                response: Mapping[str, object] = {
                    "session_id": f"simulate-{digest}",
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
        raw = self._is_new(response)
        decision = resolve_retry_decision(
            attempt, session_id, self._previous.get(candidate_id), raw
        )
        if decision is RetryDecision.FATAL_DEDUPE_HIT:
            raise SessionDedupeError(f"retry returned an existing session: {session_id}")
        self._previous[candidate_id] = session_id
        return SessionAttempt(candidate_id, attempt, session_id, raw, decision)

    def poll_session(
        self,
        session_id: str,
        *,
        candidate: Candidate | None = None,
    ) -> SessionSnapshot:
        """Poll until the API reports a terminal status with the required output."""
        if self._config.mode is Mode.SIMULATE:
            output = simulated_fix_output(candidate) if candidate is not None else {}
            return SessionSnapshot(
                session_id,
                "finished",
                {"session_id": session_id, "structured_output": output},
            )
        if self._transport is None:
            raise ConfigError("live session orchestration requires a transport")
        deadline = self._clock() + self._limits.session_timeout_s
        while True:
            response = self._transport.get(f"/v1/sessions/{session_id}")
            status = response.get("status_enum", response.get("status"))
            if not isinstance(status, str):
                status = "unknown"
            if status == "expired":
                raise SessionBlockedError(f"session expired: {session_id}")
            if status in {"finished", "blocked"}:
                if has_required_output(response):
                    self._record_terminal_usage(session_id, response)
                    return SessionSnapshot(session_id, status, response)
                raise SessionBlockedError(
                    f"session stopped with status {status} without required output: {session_id}"
                )
            if self._clock() >= deadline:
                raise TimeoutError(f"session timed out: {session_id}")
            self._sleeper(1.0)

    def _record_terminal_usage(self, session_id: str, response: Mapping[str, object]) -> None:
        """Account newly observed terminal-session ACU for a returned snapshot."""
        acu = response.get("acu_used", response.get("acu"))
        observed = float(acu) if isinstance(acu, (int, float)) else 0.0
        with self._lock:
            accounted = self._accounted_acu.get(session_id, 0.0)
            delta = max(observed - accounted, 0.0)
            self._accounted_acu[session_id] = max(accounted, observed)
            self._total_acu += delta
            if observed > self._limits.max_acu_limit:
                raise SessionCeilingError(f"session exceeded max_acu_limit: {session_id}")
            if self._total_acu > self._max_total_acu:
                raise SessionCeilingError("per-run ACU ceiling exceeded")


class RuntimeOrchestrator:
    """Run exactly one session per dispatched candidate."""

    def __init__(self, client: SessionClient) -> None:
        self._client = client

    @property
    def client(self) -> SessionClient:
        """Return the wrapped session client."""
        return self._client

    def run_candidate(
        self,
        candidate: Candidate,
        prompt: str,
        *,
        attempt: int = 1,
        session_created: Callable[[SessionAttempt], None] | None = None,
    ) -> SessionRun:
        """Create, poll and validate the one session that fixes this candidate."""
        evidence = self._client.create_session(
            candidate.candidate_id,
            prompt,
            attempt=attempt,
        )
        if session_created is not None:
            session_created(evidence)
        snapshot = self._client.poll_session(evidence.session_id, candidate=candidate)
        structured = snapshot.payload.get("structured_output")
        if not isinstance(structured, Mapping):
            raise SessionOutputError(
                f"session returned no structured output: {evidence.session_id}"
            )
        output = validate_fix_output(structured)
        if not output.feasible:
            raise SessionInfeasibleError(
                output.infeasible_reason or "session reported the candidate as infeasible",
                output=output,
            )
        return SessionRun(evidence, snapshot, output)


__all__ = [
    "DevinTransport",
    "FixOutput",
    "KNOWN_STATUSES",
    "RuntimeOrchestrator",
    "SessionAttempt",
    "SessionBlockedError",
    "SessionCeilingError",
    "SessionClient",
    "SessionDedupeError",
    "SessionInfeasibleError",
    "SessionLimits",
    "SessionOutputError",
    "SessionRun",
    "SessionSnapshot",
    "TERMINAL_STATUSES",
    "event_with_attempt",
    "event_with_ceiling",
    "has_required_output",
    "resolve_retry_decision",
    "validate_fix_output",
]
