"""Pure role prompt renderers for target-repository remediation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pipeline.schemas import Candidate, Lane

if TYPE_CHECKING:
    from pipeline.review_loop import ReviewIteration


def _context(candidate: Candidate) -> str:
    if candidate.lane is Lane.CODEQL:
        return (
            f"CodeQL rule: {candidate.rule_id or '<unknown>'}; alert number: "
            f"{candidate.alert_number or '<unknown>'}; file: {candidate.file_path or '<unknown>'}; "
            f"line: {candidate.line or '<unknown>'}; symbol: "
            f"{candidate.normalized_symbol or '<unknown>'}."
        )
    if candidate.lane is Lane.SKIPPED_TESTS:
        return (
            f"Skipped test nodeid: {candidate.nodeid or '<unknown>'}; skip reason: "
            f"{candidate.skip_reason or '<unknown>'}; enclosed tests: "
            f"{candidate.enclosed_tests if candidate.enclosed_tests is not None else '<unknown>'}."
        )
    return (
        f"Deprecated symbol: {candidate.qualname or '<unknown>'}; deprecated in: "
        f"{candidate.deprecated_in or '<unknown>'}; removed in: "
        f"{candidate.removed_in or '<unknown>'}; "
        "this site is EOL-passed under the configured major-version lag."
    )


def _preamble(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
    role: str,
) -> str:
    return (
        f"You are the {role} for candidate {candidate.candidate_id} in target repository "
        f"{target_repo}. The base SHA is {base_sha}; the head branch is {head_branch}. "
        "Your default environment is REPO A, not the target checkout. Do not assume the target "
        "checkout is present: clone the target repository and check out the base SHA if needed. "
        f"Defect context: {_context(candidate)}\n"
    )


def _branch_contract(head_branch: str) -> str:
    """Describe the shared branch handoff both coding roles must honor."""
    return (
        f"Run `git fetch origin {head_branch}` and `git checkout {head_branch}` for the existing "
        "pinned branch; do not create a branch or work detached. The implementer and reviewer "
        "are working concurrently on "
        "this same branch. Commit only your own permitted paths with `git commit -s`, then "
        f"pull --rebase origin `{head_branch}` and push origin `{head_branch}`. If the push is "
        "rejected, repeat pull-rebase-push. Never force-push. Report the resulting head_sha."
    )


def render_planner_prompt(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
) -> str:
    """Render the planner's acceptance-criteria prompt."""
    return (
        _preamble(
            candidate,
            target_repo=target_repo,
            base_sha=base_sha,
            head_branch=head_branch,
            role="PLANNER",
        )
        + "Write explicit acceptance criteria with expected_failure and verify_command fields, "
        "plus files_in_scope and out_of_scope. Planner commits nothing. Do not open a PR or issue."
    )


def _planner_text(planner_output: Mapping[str, object]) -> str:
    return json.dumps(dict(planner_output), indent=2, sort_keys=True)


def _findings_text(previous_iteration: ReviewIteration | None) -> str:
    """Render prior review evidence so retries correct the failed attempt."""
    if previous_iteration is None:
        return ""
    findings = [
        {
            "severity": finding.severity.value,
            "criterion_id": finding.criterion_id,
            "file": finding.file,
            "line": finding.line,
            "note": finding.note,
        }
        for finding in previous_iteration.findings
    ]
    payload = {
        "findings": findings,
        "failing_test": previous_iteration.failing_test,
        "pre_fix_signature": previous_iteration.pre_fix_signature,
        "prior_head_sha": previous_iteration.prior_head_sha,
    }
    return "\n\nPREVIOUS ITERATION FAILURE (correct this, do not re-roll it):\n" + json.dumps(
        payload, indent=2, sort_keys=True
    )


def validate_planner_output(planner_output: Mapping[str, object]) -> None:
    """Reject planner output that cannot act as a shared test oracle."""
    criteria = planner_output.get("criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("missing planner output: criteria")
    for index, criterion in enumerate(criteria):
        if not isinstance(criterion, Mapping):
            raise ValueError(f"missing planner output: criteria[{index}]")
        for key in ("id", "statement", "verify_command", "expected_failure"):
            if key not in criterion:
                raise ValueError(f"missing planner output: criteria[{index}].{key}")
        for key in ("id", "statement", "verify_command"):
            if not isinstance(criterion[key], str) or not criterion[key].strip():
                raise ValueError(f"missing planner output: criteria[{index}].{key}")
        expected = criterion["expected_failure"]
        if not isinstance(expected, Mapping):
            raise ValueError(f"missing planner output: criteria[{index}].expected_failure")
        for key in ("nodeid", "exception_type", "message_pattern"):
            if key not in expected:
                raise ValueError(
                    f"missing planner output: criteria[{index}].expected_failure.{key}"
                )
            if not isinstance(expected[key], str) or not expected[key].strip():
                raise ValueError(
                    f"missing planner output: criteria[{index}].expected_failure.{key}"
                )
        if "assert_location" in expected and not isinstance(expected["assert_location"], str):
            raise ValueError(
                f"missing planner output: criteria[{index}].expected_failure.assert_location"
            )
    for key in ("files_in_scope", "out_of_scope"):
        values = planner_output.get(key)
        if not isinstance(values, list) or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ValueError(f"missing planner output: {key}")


def _changed_files(diff: str) -> list[str]:
    """Extract changed paths for the large-diff handoff without reading the tree."""
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[6:]
        if path != "/dev/null" and path not in paths:
            paths.append(path)
    return paths


def render_implementer_prompt(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
    planner_output: Mapping[str, object],
    previous_iteration: ReviewIteration | None = None,
) -> str:
    """Render the production-only implementer prompt with planner output."""
    return (
        _preamble(
            candidate,
            target_repo=target_repo,
            base_sha=base_sha,
            head_branch=head_branch,
            role="IMPLEMENTER",
        )
        + _branch_contract(head_branch)
        + "\nImplement the planner specification below. Touch production files only; never edit "
        "tests or skip markers. Run every verify_command (create a venv if the environment "
        "lacks one), report only commands actually run, commit with `git commit -s`, "
        "and do not open a PR or issue.\n\n"
        "PLANNER SPECIFICATION (verbatim):\n"
        + _planner_text(planner_output)
        + _findings_text(previous_iteration)
    )


def render_reviewer_prompt(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
    planner_output: Mapping[str, object],
    previous_iteration: ReviewIteration | None = None,
) -> str:
    """Render the phase-A reviewer prompt with planner output."""
    return (
        _preamble(
            candidate,
            target_repo=target_repo,
            base_sha=base_sha,
            head_branch=head_branch,
            role="REVIEWER",
        )
        + _branch_contract(head_branch)
        + "\nAuthor tests only, at exactly the planner expected_failure nodeids. Run every "
        "verify_command (create a venv if the environment lacks one) and report only executed "
        "commands. Every test maps to a planner criterion; findings carry a criterion id or "
        "null for a genuine off-criterion defect. Do not open a PR or issue.\n\n"
        "PLANNER SPECIFICATION (verbatim):\n"
        + _planner_text(planner_output)
        + _findings_text(previous_iteration)
    )


def render_reviewer_phase_b_prompt(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
    planner_output: Mapping[str, object],
    committed_diff: str,
) -> str:
    """Render the post-join reviewer diff-review prompt."""
    diff = committed_diff
    if len(diff) > 60_000:
        files = _changed_files(diff)
        file_list = ", ".join(f"`{path}`" for path in files) or "the changed files"
        diff = (
            "[diff omitted because it exceeds 60000 characters]. Changed files: "
            f"{file_list}. Read `git diff {base_sha}..HEAD` on branch `{head_branch}` instead."
        )
    return (
        _preamble(
            candidate,
            target_repo=target_repo,
            base_sha=base_sha,
            head_branch=head_branch,
            role="REVIEWER PHASE B",
        )
        + "Read the implementer's full diff and complete the §9 findings contract. Findings "
        "must use severity blocking|major|minor|nit, nullable criterion_id, file and line "
        "range, triggering path, and proposed fix. Set diff_reviewed true only after actually "
        "reading the diff. Reuse the planner criteria below and do not author unrelated tests.\n\n"
        "PLANNER SPECIFICATION (verbatim):\n"
        + _planner_text(planner_output)
        + "\n\nIMPLEMENTER COMMITTED DIFF:\n"
        + diff
    )


__all__ = [
    "render_implementer_prompt",
    "render_planner_prompt",
    "render_reviewer_phase_b_prompt",
    "render_reviewer_prompt",
    "validate_planner_output",
]
