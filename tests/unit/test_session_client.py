"""§9/§12 session management: retry tri-state, diff roles, criterion mapping, review loop."""

from __future__ import annotations

import pytest

from pipeline.config import ConfigError, PipelineConfig
from pipeline.schemas import (
    NEEDS_HUMAN_REVIEW_LABEL,
    CandidateState,
    ReasonCode,
    RetryDecision,
    Tier,
)
from tests import _api
from tests.factories import codeql_candidate, lane2_candidate
from tests.fakes import ReviewFinding

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

PRODUCTION_DIFF = """\
--- a/superset/db_engine_specs/base.py
+++ b/superset/db_engine_specs/base.py
@@
-        return None
+        return indexes
"""


# -- §12.2 retry tri-state ---------------------------------------------------------------


def test_retry_asserts_new_session() -> None:
    """§17 — `false` is fatal; `null` falls back to the session-id comparison."""
    resolve = _api.session_client().resolve_retry

    assert (
        resolve(attempt=1, is_new_session=None, session_id="s-1", previous_session_id=None)
        == RetryDecision.PROCEED
    )
    assert (
        resolve(attempt=2, is_new_session=True, session_id="s-2", previous_session_id="s-1")
        == RetryDecision.PROCEED
    )
    assert (
        resolve(attempt=2, is_new_session=False, session_id="s-2", previous_session_id="s-1")
        == RetryDecision.FATAL_DEDUPE_HIT
    )
    assert (
        resolve(attempt=2, is_new_session=None, session_id="s-1", previous_session_id="s-1")
        == RetryDecision.FATAL_DEDUPE_HIT
    )
    assert (
        resolve(attempt=2, is_new_session=None, session_id="s-2", previous_session_id="s-1")
        == RetryDecision.PROCEED_ID_DIFFERS
    )


def test_missing_field_never_aborts_a_legitimate_first_attempt() -> None:
    assert (
        _api.session_client().resolve_retry(
            attempt=1, is_new_session=False, session_id="s-1", previous_session_id=None
        )
        == RetryDecision.PROCEED
    )


# -- §9.3 role-aware diff classifier -----------------------------------------------------


def test_implementer_assertion_hunk_rejected() -> None:
    classification = _api.session_client().classify_diff(ASSERTION_DIFF, role="implementer")

    assert classification.allowed is False
    assert classification.reason == ReasonCode.IMPLEMENTER_TEST_EDIT


def test_implementer_skip_marker_hunk_rejected() -> None:
    """§17 — the implementer has no LANE 2 carve-out for skip markers."""
    classification = _api.session_client().classify_diff(SKIP_MARKER_DIFF, role="implementer")

    assert classification.allowed is False
    assert classification.reason == ReasonCode.IMPLEMENTER_TEST_EDIT


def test_implementer_production_hunk_allowed() -> None:
    classification = _api.session_client().classify_diff(PRODUCTION_DIFF, role="implementer")

    assert classification.allowed is True
    assert classification.reason is None


def test_reviewer_skip_marker_diff_accepted() -> None:
    """§9.2 — the marker is single-owner and that owner is the reviewer."""
    classification = _api.session_client().classify_diff(SKIP_MARKER_DIFF, role="reviewer")

    assert classification.allowed is True


def test_nested_child_commits_only_own_marker() -> None:
    """§17 — the committed diff of a nested candidate carries no marker outside its own node."""
    parent_lift = """\
--- a/tests/integration_tests/charts/data/api_tests.py
+++ b/tests/integration_tests/charts/data/api_tests.py
@@
-@pytest.mark.skip("Broken")
 class TestPostChartDataApi(base_tests.SupersetTestCase):
@@
-    @pytest.mark.skip("Flaky")
     def test_chart_data_get(self):
"""
    classification = _api.session_client().classify_diff(parent_lift, role="reviewer")

    assert classification.allowed is False
    assert classification.reason == ReasonCode.BLOCKED_BY_ENCLOSING_SKIP


# -- §12.1 criterion mapping -------------------------------------------------------------


def test_reviewer_test_without_mapped_criterion_is_rejected() -> None:
    """§17/§12.1 — a reviewer test whose `criterion_id` is absent escalates."""
    planner = {"criteria": [{"id": "AC-1", "statement": "normalize_indexes returns indexes"}]}
    reviewer = {
        "tests": [
            {
                "path": "tests/unit_tests/x.py",
                "nodeid": "tests/unit_tests/x.py::a",
                "criterion_id": "AC-1",
            },
            {
                "path": "tests/unit_tests/x.py",
                "nodeid": "tests/unit_tests/x.py::b",
                "criterion_id": "AC-9",
            },
        ]
    }

    mapping = _api.session_client().validate_criterion_mapping(planner, reviewer)

    assert mapping.ok is False
    assert list(mapping.unmapped_nodeids) == ["tests/unit_tests/x.py::b"]


def test_fully_mapped_reviewer_tests_are_accepted() -> None:
    planner = {"criteria": [{"id": "AC-1"}]}
    reviewer = {"tests": [{"nodeid": "tests/unit_tests/x.py::a", "criterion_id": "AC-1"}]}

    mapping = _api.session_client().validate_criterion_mapping(planner, reviewer)

    assert mapping.ok is True
    assert list(mapping.unmapped_nodeids) == []


# -- §9 review loop ----------------------------------------------------------------------


def unresolved_major() -> list[ReviewFinding]:
    return [ReviewFinding(severity="major", note="still red", resolved=False)]


@pytest.mark.parametrize("cap", [3, 5])
def test_non_converging_loop_escalates_at_exactly_the_iteration_cap(cap: int) -> None:
    """§17 — escalation happens at exactly `iteration_cap`, parameterized over {3, 5}."""
    config = PipelineConfig(iteration_cap=cap)
    candidate = codeql_candidate(tier=Tier.HIGH, score=128.0, gate_passed=True)

    outcome = _api.session_client().run_review_loop(
        candidate,
        config,
        rounds=[unresolved_major() for _ in range(cap + 3)],
        ci_green=True,
        test_added=True,
    )

    assert outcome.converged is False
    assert outcome.iterations == cap
    assert outcome.reason == ReasonCode.DISAGREEMENT_UNRESOLVED
    assert outcome.auto_merge_eligible is False
    assert NEEDS_HUMAN_REVIEW_LABEL in outcome.labels


def test_shipped_default_iteration_cap_is_five(simulate_config: PipelineConfig) -> None:
    assert simulate_config.iteration_cap == 5

    outcome = _api.session_client().run_review_loop(
        codeql_candidate(tier=Tier.HIGH, score=128.0, gate_passed=True),
        simulate_config,
        rounds=[unresolved_major() for _ in range(8)],
        ci_green=True,
        test_added=True,
    )

    assert outcome.iterations == 5


def test_still_red_join_never_auto_merges(simulate_config: PipelineConfig) -> None:
    """§9 step 5 — a still-red join goes straight to a human, with no adjudicating session."""
    outcome = _api.session_client().run_review_loop(
        lane2_candidate(tier=Tier.HIGH, score=120.0, gate_passed=True),
        simulate_config,
        rounds=[unresolved_major(), unresolved_major()],
        ci_green=False,
        test_added=True,
    )

    assert outcome.auto_merge_eligible is False
    assert NEEDS_HUMAN_REVIEW_LABEL in outcome.labels


def test_loop_converges_once_findings_are_resolved(simulate_config: PipelineConfig) -> None:
    outcome = _api.session_client().run_review_loop(
        codeql_candidate(tier=Tier.HIGH, score=128.0, gate_passed=True),
        simulate_config,
        rounds=[
            [ReviewFinding(severity="major", resolved=False)],
            [ReviewFinding(severity="nit", resolved=False)],
        ],
        ci_green=True,
        test_added=True,
    )

    assert outcome.converged is True
    assert outcome.iterations == 2
    assert outcome.state == CandidateState.CONVERGED
    assert outcome.reason is None


# -- §14 structural invariants -----------------------------------------------------------


def test_role_collision_raises_config_error() -> None:
    """§14/§17 — reviewer == implementer is a configuration error, build-time and runtime."""
    session = _api.session_client()

    session.assert_distinct_roles(
        planner_session_id="s-plan",
        implementer_session_id="s-impl",
        reviewer_session_id="s-rev",
    )

    with pytest.raises(ConfigError) as excinfo:
        session.assert_distinct_roles(
            planner_session_id="s-plan",
            implementer_session_id="s-same",
            reviewer_session_id="s-same",
        )

    assert ReasonCode.ROLE_COLLISION.value in str(excinfo.value)


def test_session_ceiling_aborts_run() -> None:
    """§12.2 — exceeding the per-run session ceiling aborts rather than degrading silently."""
    session = _api.session_client()

    session.enforce_session_ceiling(used=2, requested=3, ceiling=6)

    with pytest.raises(session.SessionCeilingExceeded) as excinfo:
        session.enforce_session_ceiling(used=5, requested=3, ceiling=6)

    assert ReasonCode.SESSION_CEILING_EXCEEDED.value in str(excinfo.value)
