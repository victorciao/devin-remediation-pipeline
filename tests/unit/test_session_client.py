"""§9/§12 session management: retry tri-state, diff roles, ceilings and role separation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline.config import ConfigError, Mode, PipelineConfig
from pipeline.schemas import EventRecord, Lane, ReasonCode, RetryDecision
from pipeline.session_client import (
    SessionCeilingError,
    SessionClient,
    event_with_ceiling,
    resolve_retry_decision,
)

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


# -- §14 structural invariants -----------------------------------------------------------


def test_session_ceiling_abort_is_recorded_as_a_terminal_event() -> None:
    event = EventRecord(run_id="run-1", lane=Lane.CODEQL, candidate_id="cand-1")

    recorded = event_with_ceiling(event, SessionCeilingError("ceiling"))

    assert recorded.reason is ReasonCode.SESSION_CEILING
    assert recorded.terminal_outcome is not None


def test_live_orchestration_requires_a_transport() -> None:
    """No implicit network client: LIVE without an injected transport is a config error."""
    with pytest.raises(ConfigError):
        SessionClient(live_config())
