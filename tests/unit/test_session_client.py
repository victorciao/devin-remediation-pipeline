"""§9/§12 session management: retry tri-state, diff roles, ceilings and role separation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import SecretStr

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.red_baseline import classify_implementer_diff, inspect_reviewer_diff
from pipeline.schemas import EventRecord, Lane, ReasonCode, RetryDecision
from pipeline.session_client import (
    PHASE_B_REVIEWER_OUTPUT_SCHEMA,
    ROLE_OUTPUT_SCHEMAS,
    RoleCollisionError,
    RoleLimits,
    SessionCeilingError,
    SessionClient,
    SessionDedupeError,
    SessionRole,
    event_with_attempt,
    event_with_ceiling,
    resolve_retry_decision,
)
from tests.factories import lane2_candidate

ASSERTION_DIFF = """\
--- a/tests/unit_tests/db_engine_specs/test_base.py
+++ b/tests/unit_tests/db_engine_specs/test_base.py
@@
-    assert normalize_indexes(indexes) == []
+    assert normalize_indexes(indexes) == [{"column_names": ["a"]}]
"""

SKIP_MARKER_DIFF = """\
--- a/tests/integration_tests/sqllab_tests.py
+++ b/tests/integration_tests/sqllab_tests.py
@@
-    @pytest.mark.skip("Flaky")
     def test_run_sync_query(self):
"""

# The reviewer's own-node marker removal, with the node named inside the changed hunk.
OWN_NODE_SKIP_MARKER_DIFF = """\
--- a/tests/integration_tests/sqllab_tests.py
+++ b/tests/integration_tests/sqllab_tests.py
@@
-    @pytest.mark.skip("Flaky")
-    def test_run_sync_query(self):
+    def test_run_sync_query(self):
"""

PRODUCTION_DIFF = """\
--- a/superset/db_engine_specs/base.py
+++ b/superset/db_engine_specs/base.py
@@
-        return None
+        return indexes
"""

NODEID = "tests/integration_tests/sqllab_tests.py::TestSqlLab::test_run_sync_query"
CLASS_NODEID = "tests/integration_tests/sqllab_tests.py::TestSqlLab"


def live_config(**overrides: Any) -> PipelineConfig:  # noqa: ANN401
    return PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-github-token"),
        devin_api_key=SecretStr("placeholder-devin-key"),
        **overrides,
    )


class FakeTransport:
    """A local Devin API stand-in: no network, fully scripted responses."""

    def __init__(
        self,
        *,
        create_responses: list[Mapping[str, object]] | None = None,
        get_responses: list[Mapping[str, object]] | None = None,
    ) -> None:
        self.create_responses = create_responses or []
        self.get_responses = get_responses or []
        self.posts: list[tuple[str, Mapping[str, object]]] = []
        self.gets: list[str] = []

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        self.posts.append((path, payload))
        if not self.create_responses:
            return {"session_id": f"s-{len(self.posts)}", "is_new_session": True}
        return self.create_responses.pop(0)

    def get(self, path: str) -> Mapping[str, object]:
        self.gets.append(path)
        if not self.get_responses:
            return {"status_enum": "finished", "acu_used": 1.0}
        return self.get_responses.pop(0)


def terminal_response(role: SessionRole, *, acu_used: float) -> Mapping[str, object]:
    """A finished snapshot carrying the role's required output keys and its cost."""
    required = ROLE_OUTPUT_SCHEMAS[role]["required"]
    assert isinstance(required, list)
    return {
        "status_enum": "finished",
        "acu_used": acu_used,
        "structured_output": {str(key): [] for key in required},
    }


# -- §12.2 retry tri-state ---------------------------------------------------------------


def test_retry_asserts_new_session() -> None:
    """§17 — `false` is fatal; `null` falls back to the session-id comparison."""
    assert resolve_retry_decision(1, "s-1", None, None) is RetryDecision.PROCEED
    assert resolve_retry_decision(2, "s-2", "s-1", True) is RetryDecision.PROCEED
    assert resolve_retry_decision(2, "s-2", "s-1", False) is RetryDecision.FATAL_DEDUPE_HIT
    assert resolve_retry_decision(2, "s-1", "s-1", None) is RetryDecision.FATAL_DEDUPE_HIT
    assert resolve_retry_decision(2, "s-2", "s-1", None) is RetryDecision.PROCEED_ID_DIFFERS


def test_missing_field_never_aborts_a_legitimate_first_attempt() -> None:
    assert resolve_retry_decision(1, "s-1", None, False) is RetryDecision.PROCEED


def test_retry_evidence_is_recorded_on_the_layer_one_event() -> None:
    """§12 — the event carries the attempt ordinal, the raw tri-state and the decision."""
    client = SessionClient(PipelineConfig())
    attempt = client.create_session(SessionRole.PLANNER, "cand-1", "prompt")
    event = EventRecord(run_id="run-1", lane=Lane.SKIPPED_TESTS, candidate_id="cand-1")

    recorded = event_with_attempt(event, attempt)

    assert recorded.attempt == 1
    assert recorded.is_new_session_raw is True
    assert recorded.retry_decision is RetryDecision.PROCEED


def test_fatal_dedupe_hit_aborts_the_retry() -> None:
    """§12.2 — a retry that returns an existing session must not silently proceed."""
    transport = FakeTransport(
        create_responses=[
            {"session_id": "s-1", "is_new_session": True},
            {"session_id": "s-1", "is_new_session": False},
        ]
    )
    client = SessionClient(live_config(), transport=transport)
    client.create_session(SessionRole.IMPLEMENTER, "cand-1", "prompt", attempt=1)

    with pytest.raises(SessionDedupeError):
        client.create_session(SessionRole.IMPLEMENTER, "cand-1", "prompt", attempt=2)


def test_session_creation_is_idempotent_and_candidate_tagged() -> None:
    """§12.2 — creation is idempotent and carries the candidate/role/attempt tags."""
    transport = FakeTransport()
    client = SessionClient(live_config(), transport=transport)

    client.create_session(SessionRole.REVIEWER, "cand-9", "prompt", attempt=2)

    _, payload = transport.posts[0]
    assert payload["idempotent"] is True
    assert payload["tags"] == ["devin-remediation", "cand-9", "reviewer", "attempt:2"]
    assert "attempt:2" in str(payload["prompt"])


# -- §9.3 role-aware diff classifier -----------------------------------------------------


def test_implementer_assertion_hunk_rejected() -> None:
    inspection = classify_implementer_diff(ASSERTION_DIFF)

    assert inspection.accepted is False
    assert inspection.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_implementer_skip_marker_hunk_rejected() -> None:
    """§17 — the implementer has no LANE 2 carve-out for skip markers."""
    inspection = classify_implementer_diff(SKIP_MARKER_DIFF)

    assert inspection.accepted is False
    assert inspection.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_implementer_production_hunk_allowed() -> None:
    inspection = classify_implementer_diff(PRODUCTION_DIFF)

    assert inspection.accepted is True
    assert inspection.reason is None


def test_reviewer_skip_marker_diff_accepted() -> None:
    """§9.2 — the marker is single-owner and that owner is the reviewer."""
    candidate = lane2_candidate(nodeid=NODEID)

    inspection = inspect_reviewer_diff(OWN_NODE_SKIP_MARKER_DIFF, candidate)

    assert inspection.accepted is True
    assert inspection.reason is None


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan-vs-code: §9.3 line 517 accepts the reviewer's skip-marker-only diff, and §9.2 "
        "scopes ownership by marker position relative to the node. `inspect_reviewer_diff` "
        "reads only added/removed lines, so a marker removal whose node appears solely on a "
        "context line is rejected as an implementer_test_edit."
    ),
)
def test_reviewer_marker_only_removal_above_its_own_node_is_accepted() -> None:
    """§9.2/§9.3 — removing only the marker line directly above the candidate's own node."""
    candidate = lane2_candidate(nodeid=NODEID)

    inspection = inspect_reviewer_diff(SKIP_MARKER_DIFF, candidate)

    assert inspection.accepted is True


def test_reviewer_may_not_touch_production_paths() -> None:
    """§9.2 — the reviewer owns test paths; production code belongs to the implementer."""
    candidate = lane2_candidate(nodeid=NODEID)

    inspection = inspect_reviewer_diff(PRODUCTION_DIFF, candidate)

    assert inspection.accepted is False
    assert inspection.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_nested_child_commits_only_own_marker() -> None:
    """§17 — the committed diff of a nested candidate carries no marker outside its own node."""
    parent_lift = """\
--- a/tests/integration_tests/sqllab_tests.py
+++ b/tests/integration_tests/sqllab_tests.py
@@
-@pytest.mark.skip("Broken")
 class TestSqlLab(base_tests.SupersetTestCase):
"""
    candidate = lane2_candidate(nodeid=NODEID, enclosing_skip_nodeid=CLASS_NODEID)

    inspection = inspect_reviewer_diff(
        parent_lift,
        candidate,
        lifted_markers=[NODEID, CLASS_NODEID],
    )

    assert inspection.accepted is False
    assert inspection.reason is ReasonCode.IMPLEMENTER_TEST_EDIT


def test_nested_candidate_must_lift_every_ancestor_first() -> None:
    """§9.2 — classification is refused until the enumerated ancestors are lifted."""
    candidate = lane2_candidate(nodeid=NODEID, enclosing_skip_nodeid=CLASS_NODEID)

    with pytest.raises(ValueError, match="enclosing marker"):
        inspect_reviewer_diff(OWN_NODE_SKIP_MARKER_DIFF, candidate, lifted_markers=[NODEID])


# -- §12.1 structured outputs ------------------------------------------------------------


def node(role: SessionRole, *path: str) -> Mapping[str, Any]:
    """Walk a role's JSON schema, which the client types only as opaque mapping values."""
    current: Any = ROLE_OUTPUT_SCHEMAS[role]
    for key in path:
        current = current[key]
    return cast(Mapping[str, Any], current)


def test_role_output_schemas_require_the_planner_criterion_contract() -> None:
    """§12.1 — each planner criterion carries an expected failure and a verify command."""
    criterion = node(SessionRole.PLANNER, "properties", "criteria", "items")

    assert criterion["required"] == ["id", "statement", "expected_failure", "verify_command"]
    assert criterion["properties"]["expected_failure"]["required"] == [
        "nodeid",
        "exception_type",
        "message_pattern",
    ]


def test_reviewer_output_schema_binds_every_test_to_a_criterion() -> None:
    """§12.1 — a reviewer test without a `criterion_id` cannot be reported at all."""
    reviewer = ROLE_OUTPUT_SCHEMAS[SessionRole.REVIEWER]

    assert reviewer["required"] == [
        "tests",
        "red_baseline",
        "green_result",
        "findings",
        "committed_diff",
    ]
    assert node(SessionRole.REVIEWER, "properties", "tests", "items")["required"] == [
        "path",
        "nodeid",
        "criterion_id",
    ]


def test_the_phase_a_reviewer_is_never_asked_for_a_diff_it_cannot_have_read() -> None:
    """§9.3 — phase A runs concurrently with the implementer, so no diff exists yet.

    Requiring `diff_reviewed` in phase A forces the reviewer to invent a commit range,
    which is precisely the fabricated evidence the phase split exists to prevent.
    """
    reviewer = ROLE_OUTPUT_SCHEMAS[SessionRole.REVIEWER]

    assert "diff_reviewed" not in cast(list[str], reviewer["required"])
    assert "diff_reviewed" not in node(SessionRole.REVIEWER, "properties")


def test_phase_b_reviewer_schema_requires_the_reviewed_diff_identity() -> None:
    """§12.1 — `diff_reviewed` records which commit range the reviewer actually read."""
    schema = cast(Mapping[str, Any], PHASE_B_REVIEWER_OUTPUT_SCHEMA)
    reviewed = cast(Mapping[str, Any], schema["properties"]["diff_reviewed"])

    assert schema["required"] == ["diff_reviewed", "findings"]
    assert reviewed["required"] == ["base_sha", "head_sha", "files_read"]


def test_implementer_output_schema_has_no_test_surface() -> None:
    """§9.3 — the implementer reports production changes only."""
    implementer = ROLE_OUTPUT_SCHEMAS[SessionRole.IMPLEMENTER]

    assert implementer["required"] == [
        "files_changed",
        "criteria_addressed",
        "commands_run",
        "committed_diff",
    ]
    assert "tests" not in node(SessionRole.IMPLEMENTER, "properties")


# -- §14 structural invariants -----------------------------------------------------------


def test_role_collision_raises_config_error() -> None:
    """§14/§17 — reviewer == implementer is a configuration error, build-time and runtime."""
    transport = FakeTransport(
        create_responses=[
            {"session_id": "s-shared", "is_new_session": True},
            {"session_id": "s-shared", "is_new_session": True},
        ]
    )
    client = SessionClient(live_config(), transport=transport)
    client.create_session(SessionRole.IMPLEMENTER, "cand-1", "prompt")

    with pytest.raises(RoleCollisionError) as excinfo:
        client.create_session(SessionRole.REVIEWER, "cand-1", "prompt")

    assert isinstance(excinfo.value, ConfigError)
    assert excinfo.value.reason is ReasonCode.ROLE_COLLISION


def test_session_ceiling_refuses_the_creation_it_cannot_afford() -> None:
    """§12.2/§13 — the ceiling is refused loudly at creation, never degraded silently.

    The run-level handler turns this into one `deferred`/`session_ceiling` candidate; the
    client's job is only to make the exhausted budget unmissable.
    """
    client = SessionClient(PipelineConfig(), max_sessions=2)

    client.create_session(SessionRole.PLANNER, "cand-1", "prompt")
    client.create_session(SessionRole.IMPLEMENTER, "cand-1", "prompt")

    with pytest.raises(SessionCeilingError) as excinfo:
        client.create_session(SessionRole.REVIEWER, "cand-1", "prompt")

    assert excinfo.value.reason is ReasonCode.SESSION_CEILING


def test_session_ceiling_abort_is_recorded_as_a_terminal_event() -> None:
    event = EventRecord(run_id="run-1", lane=Lane.CODEQL, candidate_id="cand-1")

    recorded = event_with_ceiling(event, SessionCeilingError("ceiling"))

    assert recorded.reason is ReasonCode.SESSION_CEILING
    assert recorded.terminal_outcome is not None


def test_the_acu_ceiling_defers_the_candidate_with_its_own_reason() -> None:
    """§12.2/§13 — the per-run ACU ceiling is enforced on poll and carries `session_ceiling`.

    The reason code is what makes the deferral accountable: the run-level handler appends the
    in-flight candidate as `deferred`/`session_ceiling` and still reaches publication, so an
    exhausted budget costs one candidate rather than the run's whole report.
    """
    transport = FakeTransport(
        get_responses=[terminal_response(SessionRole.PLANNER, acu_used=40.0)],
    )
    client = SessionClient(live_config(), transport=transport, max_total_acu=10.0)
    attempt = client.create_session(SessionRole.PLANNER, "cand-1", "prompt")

    with pytest.raises(SessionCeilingError) as excinfo:
        client.poll_session(SessionRole.PLANNER, attempt.session_id)

    assert excinfo.value.reason is ReasonCode.SESSION_CEILING


def test_per_session_acu_limit_is_enforced() -> None:
    """§12.2/§13 — one over-budget session defers its candidate with `session_ceiling`."""
    transport = FakeTransport(
        get_responses=[terminal_response(SessionRole.IMPLEMENTER, acu_used=90.0)],
    )
    client = SessionClient(
        live_config(),
        transport=transport,
        role_limits={SessionRole.IMPLEMENTER: RoleLimits(max_acu_limit=20.0)},
    )
    attempt = client.create_session(SessionRole.IMPLEMENTER, "cand-1", "prompt")

    with pytest.raises(SessionCeilingError) as excinfo:
        client.poll_session(SessionRole.IMPLEMENTER, attempt.session_id)

    assert excinfo.value.reason is ReasonCode.SESSION_CEILING


def test_polling_times_out_instead_of_hanging() -> None:
    """§12.2 — a non-terminal status must respect the role session timeout."""
    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    transport = FakeTransport(
        get_responses=[{"status_enum": "working"} for _ in range(5)],
    )
    client = SessionClient(
        live_config(),
        transport=transport,
        role_limits={SessionRole.REVIEWER: RoleLimits(session_timeout_s=5.0)},
        clock=lambda: next(ticks),
        sleeper=lambda _seconds: None,
    )
    attempt = client.create_session(SessionRole.REVIEWER, "cand-1", "prompt")

    with pytest.raises(TimeoutError):
        client.poll_session(SessionRole.REVIEWER, attempt.session_id)


def test_live_orchestration_requires_a_transport() -> None:
    """No implicit network client: LIVE without an injected transport is a config error."""
    with pytest.raises(ConfigError):
        SessionClient(live_config())


def test_simulate_never_touches_the_transport() -> None:
    """§10 — SIMULATE runs the role lifecycle without a single remote call."""
    transport = FakeTransport()
    client = SessionClient(PipelineConfig(), transport=transport)

    attempt = client.create_session(SessionRole.PLANNER, "cand-1", "prompt")
    snapshot = client.poll_session(SessionRole.PLANNER, attempt.session_id)

    assert transport.posts == []
    assert transport.gets == []
    assert attempt.session_id.startswith("simulate-")
    assert snapshot.status_enum == "finished"
