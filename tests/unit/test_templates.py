"""§8/§14 template rendering: locked section sets, PR title regex, no vulnerability detail."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.config import Mode, PipelineConfig
from pipeline.schemas import Candidate, Lane, Tier
from pipeline.templates.render import (
    ArtifactValidationError,
    candidate_marker,
    compare_template_files,
    render_issue_body,
    render_issue_title,
    render_pr_body,
    render_pr_title,
    templates_match,
    validate_issue_body,
    validate_pr_body,
    validate_pr_title,
    validate_template_sections,
)
from tests.conftest import TARGET_CHECKOUT, TEMPLATES_DIR
from tests.factories import codeql_candidate, lane2_candidate, lane3_candidate

PR_SECTIONS = (
    "### SUMMARY",
    "### IMPLEMENTATION PLAN",
    "### TESTS",
    "### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF",
    "### TESTING INSTRUCTIONS",
    "### ADDITIONAL INFORMATION",
)
SECURITY_SECTIONS = (
    "### SUMMARY (no exploit detail)",
    "### SCOPE (files or modules only)",
    "### REMEDIATION STATUS",
    "### VERIFICATION",
    "### REFERENCES (rule ID only)",
)
BUG_SECTIONS = (
    "### Bug description",
    "### Screenshots/recordings",
    "### Environment",
    "### Additional context",
    "### Checklist",
)
EXPECTED_TITLE_REGEX = (
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+"
)
REGEX_PATH = TEMPLATES_DIR / "superset" / "pr_title_regex.txt"
PR_TEMPLATE_PATH = TEMPLATES_DIR / "superset" / "PULL_REQUEST_TEMPLATE.md"


def pr_template_text() -> str:
    return PR_TEMPLATE_PATH.read_text(encoding="utf-8")


SIGNED_COMMIT = (
    "fix(mcp): bound the generated range\n\n"
    "Signed-off-by: Devin Remediation <devin@example.invalid>\n"
)


def pr_body(
    candidate_id: str = "codeql-1",
    *,
    issue_number: int | None = 101,
    commit_message: str | None = SIGNED_COMMIT,
) -> str:
    """Render a PR body for a head commit made with `git commit --signoff` (§9.2)."""
    return render_pr_body(
        pr_template_text(),
        codeql_candidate(candidate_id=candidate_id, tier=Tier.HIGH, score=128.0),
        issue_number=issue_number,
        commit_message=commit_message,
    )


def issue_template(lane: Lane) -> str:
    """The template text the run passes for one lane.

    §8/§14 — a CodeQL security body is generated from the candidate alone so no upstream
    template can leak vulnerability detail into it, so its template text is empty.
    """
    if lane is Lane.CODEQL:
        return ""
    names = {
        Lane.SKIPPED_TESTS: "issues/bug_report.yml",
        Lane.DEPRECATIONS: "issues/sip.md",
    }
    return (TEMPLATES_DIR / names[lane]).read_text(encoding="utf-8")


def test_a_codeql_issue_refuses_to_consume_an_upstream_template() -> None:
    """§8 — the security body is generated; a supplied template is a leak risk, not input."""
    supplied = (TEMPLATES_DIR / "issues/security_tracking.md").read_text(encoding="utf-8")

    with pytest.raises(ArtifactValidationError, match="does not consume a template"):
        render_issue_body(supplied, codeql_candidate(), generated_summary="s")


# -- PR body -----------------------------------------------------------------------------


def test_pr_body_marks_screenshots_not_applicable_for_backend_fixes() -> None:
    body = pr_body()
    section = body.split("### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF")[1]

    assert "n/a" in section.split("###")[0].lower()


def test_pr_body_keeps_automation_metadata_last() -> None:
    """§8 — pipeline metadata is appended after the vendored sections, never inside them."""
    body = render_pr_body(
        pr_template_text(),
        codeql_candidate(tier=Tier.HIGH, score=128.0),
        automation_metadata={"mode": "simulate", "candidate_id": "codeql-1"},
        issue_number=7,
    )

    validate_pr_body(body)
    assert body.index("### AUTOMATION METADATA") > body.index("### ADDITIONAL INFORMATION")


LOCAL_EVIDENCE = {
    "mode": "live",
    "candidate_id": "codeql-1",
    "ci_evidence_mode": "local",
    "implementer_commands_run": ["pytest tests/unit_tests/mcp_service/test_add_chart.py"],
    "reviewer_pre_fix_failure": "AssertionError: index 1000000 out of range",
    "reviewer_post_fix_result": "1 passed",
    "diff_range": f"{'1' * 40}..{'2' * 40}",
}


def local_evidence_body(**overrides: object) -> str:
    """A LIVE PR body whose CI evidence is self-reported by the automation (§10.1)."""
    return render_pr_body(
        pr_template_text(),
        codeql_candidate(tier=Tier.HIGH, score=128.0),
        automation_metadata={**LOCAL_EVIDENCE, **overrides},
        issue_number=7,
    )


def test_github_ci_evidence_needs_no_self_reported_block() -> None:
    """§10.1 — the evidence block is required only where CI did not observe the result."""
    body = render_pr_body(
        pr_template_text(),
        codeql_candidate(tier=Tier.HIGH, score=128.0),
        automation_metadata={"mode": "live", "ci_evidence_mode": "github"},
        issue_number=7,
    )

    validate_pr_body(body)
    assert "evidence_label" not in body


def test_crosslink_is_rendered_into_the_pr_body() -> None:
    body = pr_body()

    assert "Closes #101" in body
    assert candidate_marker("codeql-1") in body


def test_pr_body_without_an_issue_carries_no_crosslink() -> None:
    assert "Closes #" not in pr_body(issue_number=None)


# -- issue bodies ------------------------------------------------------------------------


def test_issue_template_is_selected_per_lane() -> None:
    """§8 — bug-report for defect lanes, security tracking for CodeQL, SIP for public API."""
    codeql_body = render_issue_body(
        issue_template(Lane.CODEQL), codeql_candidate(), generated_summary="s"
    )
    lane2_body = render_issue_body(
        issue_template(Lane.SKIPPED_TESTS), lane2_candidate(), generated_summary="s"
    )
    lane3_body = render_issue_body(
        issue_template(Lane.DEPRECATIONS),
        lane3_candidate(public_api_surface=True),
        generated_summary="s",
    )

    validate_template_sections(codeql_body, SECURITY_SECTIONS)
    validate_template_sections(lane2_body, BUG_SECTIONS)
    validate_template_sections(lane3_body, ("### Motivation", "### Proposed Change"))


def test_security_issue_never_exposes_vulnerability_detail() -> None:
    """§8/§14 — the security tracking body is detail-free and rule-id only."""
    candidate = codeql_candidate(
        candidate_id="codeql-5",
        rule_id="py/url-redirection",
        file_path="superset/views/redirect.py",
    )

    body = render_issue_body(
        issue_template(Lane.CODEQL),
        candidate,
        generated_summary="A redirect target is not validated.",
    )

    positions = [body.index(section) for section in SECURITY_SECTIONS]
    assert positions == sorted(positions)
    assert "py/url-redirection" in body
    assert "superset/views/redirect.py" in body
    content = "\n".join(line for line in body.lower().splitlines() if not line.startswith("### "))
    for forbidden in ("exploit", "payload", "proof of concept", "curl ", "attacker"):
        assert forbidden not in content
    validate_issue_body(body, candidate)


def test_sip_issue_strips_front_matter_and_assignees() -> None:
    """§8 — the SIP front matter is stripped and `assignees` never propagated."""
    body = render_issue_body(
        issue_template(Lane.DEPRECATIONS),
        lane3_candidate(public_api_surface=True),
        generated_summary="Remove the EOL shim.",
    )

    assert not body.lstrip().startswith("---")
    assert "apache/superset-committers" not in body
    assert "assignees" not in body.lower()


def test_sip_title_is_prefixed() -> None:
    assert render_issue_title(lane3_candidate(public_api_surface=True), "remove the EOL shim") == (
        "[SIP] remove the EOL shim"
    )
    assert render_issue_title(lane2_candidate(), "re-enable the skipped test") == (
        "re-enable the skipped test"
    )


def test_every_issue_body_carries_the_candidate_marker() -> None:
    """§14.1 — the marker is what makes artifact creation resumable."""
    for lane, candidate in (
        (Lane.CODEQL, codeql_candidate(candidate_id="codeql-7")),
        (Lane.SKIPPED_TESTS, lane2_candidate(candidate_id="lane2-7")),
        (Lane.DEPRECATIONS, lane3_candidate(candidate_id="lane3-7")),
    ):
        body = render_issue_body(issue_template(lane), candidate, generated_summary="s")
        assert candidate_marker(candidate.candidate_id) in body
        validate_issue_body(body, candidate)


def test_issue_body_validation_rejects_a_missing_marker() -> None:
    candidate = codeql_candidate(candidate_id="codeql-8")
    body = render_issue_body(issue_template(Lane.CODEQL), candidate, generated_summary="s")

    with pytest.raises(ValueError, match="marker"):
        validate_issue_body(body.replace(candidate_marker("codeql-8"), ""), candidate)


# -- PR title ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        codeql_candidate(tier=Tier.HIGH, score=128.0),
        lane2_candidate(tier=Tier.HIGH, score=120.0),
        lane3_candidate(tier=Tier.HIGH, score=100.0),
    ],
    ids=[Lane.CODEQL.value, Lane.SKIPPED_TESTS.value, Lane.DEPRECATIONS.value],
)
def test_pr_title_matches_pr_lint_regex(candidate: Candidate) -> None:
    """§17 — titles match the regex pinned in `templates/superset/pr_title_regex.txt`."""
    regex_text = REGEX_PATH.read_text(encoding="utf-8")

    assert regex_text.strip() == EXPECTED_TITLE_REGEX

    title = render_pr_title(candidate)
    assert validate_pr_title(title, regex_text), title


def test_pinned_regex_permits_other_and_rejects_revert() -> None:
    regex_text = REGEX_PATH.read_text(encoding="utf-8")

    assert validate_pr_title("other: tidy up the generated body", regex_text)
    assert validate_pr_title("fix(mcp): bound the generated range", regex_text)
    assert not validate_pr_title("revert: bound the generated range", regex_text)
    assert not validate_pr_title("fix:no-space-after-colon", regex_text)


def test_vendored_regex_file_is_the_locked_pattern() -> None:
    raw = REGEX_PATH.read_text(encoding="utf-8").strip()

    assert raw == EXPECTED_TITLE_REGEX
    re.compile(raw)


# -- §14 drift against the target repository's live files ---------------------------------


def test_vendored_pr_template_matches_the_live_target_template() -> None:
    """§14 — the vendored PR template must not drift from the target checkout's copy."""
    live = TARGET_CHECKOUT / ".github" / "PULL_REQUEST_TEMPLATE.md"

    assert live.is_file(), f"target checkout is required at {TARGET_CHECKOUT}"
    assert compare_template_files(PR_TEMPLATE_PATH, live)


def test_vendored_pr_title_regex_matches_the_live_pr_lint_workflow() -> None:
    """§14 — the pinned title regex must match the target's `pr-lint.yml` title-regex."""
    workflow = (TARGET_CHECKOUT / ".github" / "workflows" / "pr-lint.yml").read_text(
        encoding="utf-8"
    )

    match = re.search(r'title-regex:\s*"(?P<pattern>.+)"', workflow)
    assert match is not None, "target pr-lint workflow has no title-regex input"
    live_pattern = match.group("pattern").replace("\\\\", "\\")

    assert templates_match(REGEX_PATH.read_text(encoding="utf-8").strip(), live_pattern)


def test_compare_template_files_detects_drift(tmp_path: Path) -> None:
    drifted = tmp_path / "PULL_REQUEST_TEMPLATE.md"
    drifted.write_text(pr_template_text() + "\n### EXTRA\n", encoding="utf-8")

    assert compare_template_files(PR_TEMPLATE_PATH, drifted) is False


# -- §17 contribution compliance ---------------------------------------------------------


def test_generated_pr_contribution_compliance(simulate_config: PipelineConfig) -> None:
    """§17 — the generated body asserts the sign-off trailer Superset contribution needs."""
    body = pr_body()

    assert simulate_config.mode == Mode.SIMULATE
    assert "\t" not in body

    trailers = [line for line in body.splitlines() if line.startswith("Signed-off-by:")]
    assert trailers == ["Signed-off-by: Devin Remediation <devin@example.invalid>"]


def test_signoff_trailer_is_copied_from_the_head_commit() -> None:
    """§9.2 — every commit is made with `--signoff`, so the trailer is the commit's own."""
    commit = "fix(mcp): bound the range\n\nSigned-off-by: A Human <human@example.invalid>\n"
    body = pr_body(commit_message=commit)

    assert "Signed-off-by: A Human <human@example.invalid>" in body
    assert "Signed-off-by: Devin Remediation" not in body
