"""Pure fix-prompt rendering for the single per-candidate remediation session."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pipeline.schemas import Candidate, Lane

FIX_OUTPUT_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "required": [
        "files_changed",
        "test_nodeid",
        "test_paths",
        "verify_command",
        "head_sha",
        "suite_scope",
        "fix_summary",
        "testing_notes",
        "criterion_notes",
        "feasible",
        "infeasible_reason",
    ],
    "properties": {
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "test_nodeid": {"type": ["string", "null"]},
        "test_paths": {"type": "array", "items": {"type": "string"}},
        "verify_command": {"type": "string"},
        "head_sha": {"type": "string"},
        "suite_scope": {"type": "array", "items": {"type": "string"}},
        "fix_summary": {"type": "string"},
        "testing_notes": {"type": "string"},
        "criterion_notes": {"type": "string"},
        "feasible": {"type": "boolean"},
        "infeasible_reason": {"type": ["string", "null"]},
    },
}


def _locator(candidate: Candidate) -> str:
    """Describe the lane locator the session must act on."""
    if candidate.lane is Lane.CODEQL:
        return (
            f"CodeQL rule {candidate.rule_id or '<unknown>'} in "
            f"{candidate.file_path or '<unknown>'} at line {candidate.line or '<unknown>'} "
            f"(symbol {candidate.normalized_symbol or '<unknown>'}, "
            f"region {candidate.position_digest or '<unknown>'})"
        )
    if candidate.lane is Lane.SKIPPED_TESTS:
        return (
            f"unconditionally skipped test {candidate.nodeid or '<unknown>'} "
            f"(skip reason: {candidate.skip_reason or '<none recorded>'}, "
            f"enclosed tests: "
            f"{candidate.enclosed_tests if candidate.enclosed_tests is not None else '<unknown>'})"
        )
    return (
        f"EOL deprecated symbol {candidate.module or '<unknown>'}:"
        f"{candidate.qualname or '<unknown>'} "
        f"(deprecated in {candidate.deprecated_in or '<unknown>'}, "
        f"removed in {candidate.removed_in or '<unset>'})"
    )


def _objective(candidate: Candidate) -> str:
    """Describe the lane's fix objective."""
    if candidate.lane is Lane.CODEQL:
        return "Fix the alerted defect so the alert no longer holds at the candidate head."
    if candidate.lane is Lane.SKIPPED_TESTS:
        return (
            "Fix the underlying defect and re-enable the skipped test by removing its "
            "unconditional skip marker."
        )
    return "Remove the EOL deprecated symbol and every internal reference to it."


def _test_requirement(candidate: Candidate) -> str:
    """State the regression-test requirement for the candidate's lane."""
    if candidate.lane is Lane.DEPRECATIONS:
        return (
            "No new test is required: the evidence for a deletion is that the existing suite "
            "still passes. Report `test_nodeid: null` and say so in criterion_notes."
        )
    if candidate.lane is Lane.SKIPPED_TESTS:
        return (
            "The re-enabled test is the regression test. Report its collectable nodeid in "
            "`test_nodeid`; do not weaken, delete or re-skip it."
        )
    return (
        "Add a regression test at the narrowest level that can express the fix — a unit test "
        "is preferred to an integration test, and re-enabling an existing test counts. If the "
        "alert class admits no such test, report `test_nodeid: null` and say why in "
        "criterion_notes."
    )


def render_fix_prompt(
    candidate: Candidate,
    *,
    target_repo: str,
    base_sha: str,
    head_branch: str,
    success_criterion: str,
    attempt: int = 1,
    suite_scope: Sequence[str] = (),
) -> str:
    """Render the §9 prompt for the one session that fixes this candidate."""
    scope = list(suite_scope) or list(candidate.suite_scope)
    return (
        f"attempt:{attempt}\n"
        f"You are the single remediation session for candidate {candidate.candidate_id} in "
        f"target repository {target_repo}. Work on branch `{head_branch}`, which already "
        f"exists and is pinned at base SHA {base_sha}.\n\n"
        "Your default environment is REPO A, not the target checkout: clone the target "
        "repository if it is absent, then `git fetch origin` and "
        f"`git checkout {head_branch}`. Never work detached and never touch another branch.\n\n"
        f"LANE: {candidate.lane.value}\n"
        f"LOCATOR: {_locator(candidate)}\n"
        f"OBJECTIVE: {_objective(candidate)}\n\n"
        "SUCCESS CRITERION (verbatim; the orchestrator, not you, evaluates it):\n"
        f"{success_criterion}\n\n"
        "EVIDENCE YOU MUST LEAVE BEHIND: the fix and its test committed on "
        f"`{head_branch}`, the exact verify command you ran, the resulting head SHA, and the "
        "suite scope that covers the change. Nothing you report about your own results is "
        "evidence; the orchestrator re-runs the commands itself.\n"
        f"REGRESSION TEST: {_test_requirement(candidate)}\n"
        f"SUITE SCOPE: {', '.join(scope) if scope else 'the narrowest suite covering the fix'}\n\n"
        "COMMANDS: create a virtualenv if the checkout lacks one, run the verify command and "
        "the suite over the suite scope, and report only commands you actually ran.\n"
        "Commit every change with `git commit -s` so each commit carries the Signed-off-by "
        f"trailer, then push to `{head_branch}` only.\n\n"
        "PROHIBITIONS: do not open or comment on a pull request or issue, do not touch any "
        "other branch, do not edit unrelated tests or code, and do not force-push.\n\n"
        "Answer with the required structured output object:\n"
        + json.dumps(FIX_OUTPUT_SCHEMA, indent=2, sort_keys=True)
        + "\n`feasible: false` with an `infeasible_reason` is a legitimate answer."
    )


__all__ = ["FIX_OUTPUT_SCHEMA", "render_fix_prompt"]
