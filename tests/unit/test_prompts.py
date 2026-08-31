"""§9 role prompts: the planner's criteria are the shared oracle the other roles must receive.

The three role prompts used to be one-line stubs, so nothing the planner wrote reached the
implementer or the reviewer. In the LIVE dry run the reviewer invented its own criterion ids
and its own test nodeids and every candidate escalated with unmapped criteria.
"""

from __future__ import annotations

from typing import Any

TARGET_REPO = "victorciao/superset"
BASE_SHA = "a" * 40
HEAD_BRANCH = "devin/codeql-0"
HEAD_SHA = "b" * 40
NODEID = "tests/unit_tests/db_engine_specs/test_base.py::test_normalize_indexes"


def planner_output(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401
    """A complete planner payload: two criteria, each with a full expected failure."""
    payload: dict[str, Any] = {
        "criteria": [
            {
                "id": "AC-1",
                "statement": "normalize_indexes returns the parsed indexes",
                "verify_command": "pytest -q tests/unit_tests/db_engine_specs/test_base.py",
                "expected_failure": {
                    "nodeid": NODEID,
                    "exception_type": "AssertionError",
                    "message_pattern": "assert None == [{'column_names': ['a']}]",
                },
            },
            {
                "id": "AC-2",
                "statement": "the range helper rejects an unbounded range",
                "verify_command": "pytest -q tests/unit_tests/test_range.py",
                "expected_failure": {
                    "nodeid": "tests/unit_tests/test_range.py::test_range_is_bounded",
                    "exception_type": "ValueError",
                    "message_pattern": "range is unbounded",
                },
            },
        ],
        "files_in_scope": ["superset/db_engine_specs/base.py"],
        "out_of_scope": ["tests/"],
    }
    payload.update(overrides)
    return payload
