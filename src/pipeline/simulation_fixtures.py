"""Deterministic role-output fixtures for credential-free simulation."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from typing import TYPE_CHECKING

from pipeline.schemas import Candidate, ExpectedFailure, ItemOutcome

if TYPE_CHECKING:
    from pipeline.session_client import OrchestrationResult


def simulation_result(
    result: OrchestrationResult,
    candidate: Candidate,
) -> OrchestrationResult:
    """Replace remote role output with realistic classifier fixture data."""
    from pipeline.session_client import OrchestrationResult

    expected = candidate.expected_failure or ExpectedFailure(
        nodeid=candidate.nodeid or candidate.stable_locator,
        exception_type="AssertionError",
        message_pattern="simulated failure",
    )
    expected_payload = expected.model_dump(mode="json")
    kind = hashlib.sha256(candidate.candidate_id.encode()).digest()[0] % 3
    if kind == 0:
        observed = {
            "nodeid": expected.nodeid,
            "outcome": ItemOutcome.FAILED,
            "exception_type": expected.exception_type,
            "message": "simulated failure",
            "assert_location": expected.assert_location,
        }
    elif kind == 1:
        observed = {
            "nodeid": expected.nodeid,
            "outcome": ItemOutcome.FAILED,
            "exception_type": "TypeError",
            "message": "different simulated failure",
        }
    else:
        observed = {
            "nodeid": expected.nodeid,
            "outcome": ItemOutcome.PASSED,
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
        "files_changed": ["src/simulated_remediation.py"],
        "criteria_addressed": ["AC-1"],
        "commands_run": ["pytest fixtures/simulated_test.py"],
        "committed_diff": (
            "diff --git a/src/simulated_remediation.py b/src/simulated_remediation.py\n"
            "--- a/src/simulated_remediation.py\n"
            "+++ b/src/simulated_remediation.py\n"
            "@@ -1 +1 @@\n"
            "-old\n"
            "+new\n"
        ),
    }
    reviewer_payload = {
        "tests": [
            {
                "path": "fixtures/simulated_test.py",
                "nodeid": expected.nodeid,
                "criterion_id": "AC-1",
            }
        ],
        "red_baseline": {"observed": observed},
        "green_result": {"passed": True},
        "diff_reviewed": {
            "base_sha": candidate.base_sha or "0000000",
            "head_sha": candidate.head_sha or "1111111",
            "files_read": ["src/simulated_remediation.py"],
        },
        "committed_diff": (
            "diff --git a/tests/test_simulated.py b/tests/test_simulated.py\n"
            "--- a/tests/test_simulated.py\n"
            "+++ b/tests/test_simulated.py\n"
            "@@ -1 +1 @@\n"
            "-def test_old(): pass\n"
            "+def test_new(): pass\n"
        ),
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


__all__ = ["simulation_result"]
