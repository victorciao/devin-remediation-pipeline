"""§9 role prompts: the planner's criteria are the shared oracle the other roles must receive.

The three role prompts used to be one-line stubs, so nothing the planner wrote reached the
implementer or the reviewer. In the LIVE dry run the reviewer invented its own criterion ids
and its own test nodeids and every candidate escalated with unmapped criteria.
"""

from __future__ import annotations

from typing import Any

import pytest

from pipeline.prompts import (
    render_implementer_prompt,
    render_planner_prompt,
    render_reviewer_phase_b_prompt,
    render_reviewer_prompt,
    validate_planner_output,
)
from pipeline.schemas import Candidate
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate

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


def role_prompts(candidate: Candidate, **overrides: Any) -> list[str]:  # noqa: ANN401
    """Render every prompt that must carry the planner specification."""
    output = planner_output(**overrides)
    return [
        render_implementer_prompt(
            candidate,
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
            planner_output=output,
        ),
        render_reviewer_prompt(
            candidate,
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
            planner_output=output,
        ),
        render_reviewer_phase_b_prompt(
            candidate,
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
            planner_output=output,
            committed_diff="--- a/superset/db_engine_specs/base.py\n+++ b/x\n",
            head_sha=HEAD_SHA,
        ),
    ]


# -- planner criteria reaching the other roles -------------------------------------------


def test_every_planner_criterion_reaches_the_implementer_and_the_reviewer() -> None:
    """§9 — the criteria, their verify commands and expected failures are the shared oracle."""
    for prompt in role_prompts(codeql_candidate()):
        for criterion in planner_output()["criteria"]:
            assert criterion["id"] in prompt
            assert criterion["statement"] in prompt
            assert criterion["verify_command"] in prompt
            expected = criterion["expected_failure"]
            assert expected["nodeid"] in prompt
            assert expected["exception_type"] in prompt
            assert expected["message_pattern"] in prompt


def test_the_scope_lists_reach_the_implementer_and_the_reviewer() -> None:
    """§9.2 — the implementer may not stray outside `files_in_scope`."""
    for prompt in role_prompts(codeql_candidate()):
        assert "superset/db_engine_specs/base.py" in prompt
        assert "files_in_scope" in prompt
        assert "out_of_scope" in prompt


def test_every_prompt_names_the_target_repo_base_sha_and_head_branch() -> None:
    """§9 — a role session starts in REPO A, so the target checkout must be named."""
    candidate = codeql_candidate()
    prompts = [
        render_planner_prompt(
            candidate,
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
        ),
        *role_prompts(candidate),
    ]

    for prompt in prompts:
        assert TARGET_REPO in prompt
        assert BASE_SHA in prompt
        assert HEAD_BRANCH in prompt
        assert "Do not assume the target checkout is present" in prompt


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (codeql_candidate(), "py/overly-large-range"),
        (lane2_candidate(), "broken since the api/v1 migration"),
        (lane3_candidate(), "BaseEngineSpec.normalize_indexes"),
    ],
)
def test_each_lane_contributes_its_own_defect_context(candidate: Candidate, expected: str) -> None:
    """§9 — a role cannot locate the defect from the candidate id alone."""
    prompt = render_planner_prompt(
        candidate,
        target_repo=TARGET_REPO,
        base_sha=BASE_SHA,
        head_branch=HEAD_BRANCH,
    )

    assert expected in prompt
    for role_prompt in role_prompts(candidate):
        assert expected in role_prompt


def test_the_phase_b_prompt_carries_the_implementer_diff() -> None:
    """§9.3 — the reviewer reviews the diff it is handed, not a diff it re-derives."""
    prompt = render_reviewer_phase_b_prompt(
        codeql_candidate(),
        target_repo=TARGET_REPO,
        base_sha=BASE_SHA,
        head_branch=HEAD_BRANCH,
        planner_output=planner_output(),
        committed_diff="-        return None\n+        return indexes\n",
        head_sha=HEAD_SHA,
    )

    assert "+        return indexes" in prompt
    assert "a boolean is rejected" in prompt


def test_the_phase_b_prompt_dictates_the_head_sha_and_the_paths_to_report() -> None:
    """§14.1 (l.918-922) / §17 — phase B is asked about one resolved revision, not "the branch".

    The response object the reviewer is told to fill carries the resolved head SHA and every
    changed path, so `validated_diff_review` can reject an answer about a different revision or
    an answer that read only part of the diff. A prompt that named neither would make the
    validator's rejection unanswerable.
    """
    prompt = render_reviewer_phase_b_prompt(
        codeql_candidate(),
        target_repo=TARGET_REPO,
        base_sha=BASE_SHA,
        head_branch=HEAD_BRANCH,
        planner_output=planner_output(),
        committed_diff=(
            "--- a/superset/db_engine_specs/base.py\n"
            "+++ b/superset/db_engine_specs/base.py\n"
            "--- a/tests/unit_tests/db_engine_specs/test_base.py\n"
            "+++ b/tests/unit_tests/db_engine_specs/test_base.py\n"
        ),
        head_sha=HEAD_SHA,
    )

    assert HEAD_SHA in prompt
    assert '"head_sha": "' + HEAD_SHA + '"' in prompt
    assert '"superset/db_engine_specs/base.py"' in prompt
    assert '"tests/unit_tests/db_engine_specs/test_base.py"' in prompt
    assert "Every changed path must appear in files_read" in prompt


def test_an_oversized_phase_b_diff_is_replaced_by_a_read_instruction() -> None:
    """§9.3 — an unsendable diff becomes the changed-file list plus a `git diff` command."""
    huge = "--- a/superset/a.py\n+++ b/superset/a.py\n" + "+x\n" * 40_000

    prompt = render_reviewer_phase_b_prompt(
        codeql_candidate(),
        target_repo=TARGET_REPO,
        base_sha=BASE_SHA,
        head_branch=HEAD_BRANCH,
        planner_output=planner_output(),
        committed_diff=huge,
        head_sha=HEAD_SHA,
    )

    assert "diff omitted" in prompt
    assert "`superset/a.py`" in prompt
    assert f"git diff {BASE_SHA}..HEAD" in prompt
    assert len(prompt) < len(huge)


# -- validate_planner_output ------------------------------------------------------------


def test_a_complete_planner_output_is_accepted() -> None:
    validate_planner_output(planner_output())


@pytest.mark.parametrize(
    "field",
    ["id", "statement", "verify_command"],
)
@pytest.mark.parametrize("blank", [True, False])
def test_a_missing_or_blank_criterion_field_is_rejected(field: str, blank: bool) -> None:
    """§9 — an unmappable criterion defers the candidate instead of dispatching a review."""
    output = planner_output()
    if blank:
        output["criteria"][0][field] = "   "
    else:
        del output["criteria"][0][field]

    with pytest.raises(ValueError, match=f"criteria\\[0\\].{field}"):
        validate_planner_output(output)


@pytest.mark.parametrize("field", ["nodeid", "exception_type", "message_pattern"])
@pytest.mark.parametrize("blank", [True, False])
def test_a_missing_or_blank_expected_failure_field_is_rejected(field: str, blank: bool) -> None:
    """§9.1 — without a nodeid, exception type and message the red baseline has no oracle."""
    output = planner_output()
    if blank:
        output["criteria"][0]["expected_failure"][field] = ""
    else:
        del output["criteria"][0]["expected_failure"][field]

    with pytest.raises(ValueError, match=f"expected_failure.{field}"):
        validate_planner_output(output)


@pytest.mark.parametrize(
    "output",
    [
        planner_output(criteria=[]),
        planner_output(criteria="AC-1"),
        planner_output(criteria=["AC-1"]),
    ],
)
def test_planner_output_without_usable_criteria_is_rejected(output: dict[str, Any]) -> None:
    """§9 — no criteria at all is the failure mode the dry run hit hardest."""
    with pytest.raises(ValueError, match="criteria"):
        validate_planner_output(output)


@pytest.mark.parametrize("field", ["files_in_scope", "out_of_scope"])
@pytest.mark.parametrize("value", [None, "superset/", [""], [1]])
def test_a_missing_or_malformed_scope_list_is_rejected(field: str, value: object) -> None:
    """§9.2 — the scope lists bound the implementer's edits, so they may not be empty strings."""
    output = planner_output()
    if value is None:
        del output[field]
    else:
        output[field] = value

    with pytest.raises(ValueError, match=field):
        validate_planner_output(output)


def test_the_rejection_names_the_field_so_the_deferral_reason_is_readable() -> None:
    """§14.1 — `reason_detail` carries this message onto the durable row."""
    output = planner_output()
    del output["criteria"][1]["verify_command"]

    with pytest.raises(ValueError) as excinfo:
        validate_planner_output(output)

    assert str(excinfo.value) == "missing planner output: criteria[1].verify_command"
