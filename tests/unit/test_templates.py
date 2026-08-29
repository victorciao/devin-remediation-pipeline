"""§8/§14 template rendering: locked section sets, PR title regex, no vulnerability detail."""

from __future__ import annotations

import re

import pytest

from pipeline.config import Mode, PipelineConfig
from pipeline.schemas import Candidate, Lane, Tier
from tests import _api
from tests.conftest import TEMPLATES_DIR
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
EXPECTED_TITLE_REGEX = (
    r"^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+"
)


def pr_body(candidate_id: str = "codeql-1") -> str:
    return _api.render().render_pr_body(
        codeql_candidate(candidate_id=candidate_id, tier=Tier.HIGH, score=128.0),
        implementation_plan="Bound the range to the collection length.",
        tests="tests/unit_tests/mcp_service/test_add_chart.py::test_range_is_bounded",
        issue_number=101,
    )


def test_pr_body_carries_the_locked_sections_in_order() -> None:
    """§8 — four template sections plus the two insertion points after `### SUMMARY`."""
    body = pr_body()

    positions = [body.index(section) for section in PR_SECTIONS]
    assert positions == sorted(positions)
    assert "- [ ]" in body
    assert "### CHECKLIST" not in body


def test_pr_body_marks_screenshots_not_applicable_for_backend_fixes() -> None:
    body = pr_body()
    section = body.split("### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF")[1]

    assert "n/a" in section.split("###")[0].lower()


def test_validate_pr_body_rejects_missing_and_reordered_sections() -> None:
    render = _api.render()
    body = pr_body()

    assert render.validate_pr_body(body).valid is True

    missing = body.replace("### TESTING INSTRUCTIONS", "### NOT A SECTION")
    result = render.validate_pr_body(missing)
    assert result.valid is False
    assert "### TESTING INSTRUCTIONS" in result.missing_sections

    swapped = "\n".join(
        [
            "### TESTS",
            "t",
            "### SUMMARY",
            "s",
            "### IMPLEMENTATION PLAN",
            "p",
            "### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF",
            "n/a",
            "### TESTING INSTRUCTIONS",
            "i",
            "### ADDITIONAL INFORMATION",
            "a",
        ]
    )
    reordered = render.validate_pr_body(swapped)
    assert reordered.valid is False
    assert list(reordered.out_of_order_sections) != []


def test_crosslink_is_rendered_into_the_pr_body() -> None:
    body = pr_body()

    assert "Closes #101" in body
    assert _api.dedupe().marker("codeql-1") in body


def test_issue_template_is_selected_per_lane() -> None:
    """§8 — bug-report for defect lanes, security tracking for CodeQL, SIP for public API."""
    render = _api.render()

    assert render.select_issue_template(codeql_candidate()) == "security_tracking"
    assert render.select_issue_template(lane2_candidate()) == "bug_report"
    assert render.select_issue_template(lane3_candidate(public_api=True)) == "sip"


def test_security_issue_never_exposes_vulnerability_detail() -> None:
    """§8/§14 — the security tracking body is detail-free and rule-id only."""
    render = _api.render()
    candidate = codeql_candidate(
        candidate_id="codeql-5",
        rule_id="py/url-redirection",
        file_path="superset/views/redirect.py",
    )

    body = render.render_issue_body(candidate, pr_url="https://github.test/pull/900")

    positions = [body.index(section) for section in SECURITY_SECTIONS]
    assert positions == sorted(positions)
    assert "py/url-redirection" in body
    assert "superset/views/redirect.py" in body
    for forbidden in ("exploit", "payload", "proof of concept", "curl ", "attacker"):
        assert forbidden not in body.lower()
    assert render.validate_issue_body(body, "security_tracking").valid is True


def test_sip_issue_strips_front_matter_and_assignees() -> None:
    """§8 — the SIP front matter is stripped and `assignees` never propagated."""
    body = _api.render().render_issue_body(lane3_candidate(public_api=True))

    assert not body.lstrip().startswith("---")
    assert "apache/superset-committers" not in body
    assert "assignees" not in body.lower()


def test_sip_title_is_prefixed() -> None:
    title = _api.render().render_pr_title(lane3_candidate(public_api=True))

    assert title.startswith(("feat", "fix", "chore", "refactor", "other"))


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
    render = _api.render()
    pattern = render.load_pr_title_regex(TEMPLATES_DIR / "superset" / "pr_title_regex.txt")

    assert pattern.pattern.strip() == EXPECTED_TITLE_REGEX

    title = render.render_pr_title(candidate)
    assert pattern.match(title), title


def test_pinned_regex_permits_other_and_rejects_revert() -> None:
    pattern = _api.render().load_pr_title_regex(TEMPLATES_DIR / "superset" / "pr_title_regex.txt")

    assert pattern.match("other: tidy up the generated body")
    assert pattern.match("fix(mcp): bound the generated range")
    assert not pattern.match("revert: bound the generated range")
    assert not pattern.match("fix:no-space-after-colon")


def test_vendored_regex_file_is_the_locked_pattern() -> None:
    raw = (TEMPLATES_DIR / "superset" / "pr_title_regex.txt").read_text(encoding="utf-8").strip()

    assert raw == EXPECTED_TITLE_REGEX
    re.compile(raw)


def test_generated_pr_contribution_compliance(simulate_config: PipelineConfig) -> None:
    """§17 — the generated body asserts the sign-off trailer Superset contribution needs."""
    body = pr_body()

    assert "Signed-off-by:" in body
    assert simulate_config.mode == Mode.SIMULATE
    assert "\t" not in body
    assert all(len(line) <= 120 for line in body.splitlines())
