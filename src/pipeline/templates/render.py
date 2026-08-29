"""Pure rendering and validation for Superset issue and pull-request artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pipeline.config import SECURITY_ISSUE_MODE
from pipeline.schemas import Candidate, Lane


def candidate_marker(candidate_id: str) -> str:
    """Return the stable marker used to resume one candidate's artifacts."""
    return f"<!-- devin-remediation-id: {candidate_id} -->"


def _section_body(template: str, heading: str) -> str:
    lines = template.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == heading:
            next_heading = len(lines)
            for next_index in range(index + 1, len(lines)):
                if lines[next_index].startswith("### "):
                    next_heading = next_index
                    break
            return "\n".join(lines[index + 1 : next_heading]).strip()
    raise ValueError(f"template lacks required heading: {heading}")


def _planner_text(planner_output: Mapping[str, object]) -> str:
    criteria = planner_output.get("criteria")
    if not isinstance(criteria, list):
        return "No additional implementation criteria supplied."
    rendered: list[str] = []
    for item in criteria:
        if not isinstance(item, Mapping):
            continue
        criterion_id = item.get("id")
        statement = item.get("statement")
        if isinstance(criterion_id, str) and isinstance(statement, str):
            rendered.append(f"- **{criterion_id}**: {statement}")
    return "\n".join(rendered) or "No additional implementation criteria supplied."


def _reviewer_text(reviewer_output: Mapping[str, object]) -> str:
    tests = reviewer_output.get("tests")
    commands = reviewer_output.get("commands_run")
    lines: list[str] = []
    if isinstance(tests, list):
        for item in tests:
            if not isinstance(item, Mapping):
                continue
            nodeid = item.get("nodeid")
            criterion_id = item.get("criterion_id")
            if isinstance(nodeid, str) and isinstance(criterion_id, str):
                lines.append(f"- `{nodeid}` (criterion `{criterion_id}`)")
    if isinstance(commands, list):
        lines.extend(f"- `{item}`" for item in commands if isinstance(item, str))
    return "\n".join(lines) or "No reviewer test details supplied."


def render_pr_body(
    template: str,
    candidate: Candidate,
    planner_output: Mapping[str, object],
    reviewer_output: Mapping[str, object],
    *,
    automation_metadata: Mapping[str, object] | None = None,
    issue_number: int | None = None,
) -> str:
    """Render a PR body while preserving the vendored Superset checkbox block."""
    summary = (
        "Technical remediation for engineers and AI reviewers; candidate surfaced by "
        f"the {candidate.lane.value} lane."
    )
    if issue_number is not None:
        summary += f"\n\nCloses #{issue_number}"
    prefix = template.split("### SUMMARY", 1)[0].rstrip()
    additional = _section_body(template, "### ADDITIONAL INFORMATION")
    sections = [
        candidate_marker(candidate.candidate_id),
        prefix,
        "### SUMMARY",
        summary,
        "",
        "### IMPLEMENTATION PLAN",
        _planner_text(planner_output),
        "",
        "### TESTS",
        _reviewer_text(reviewer_output),
        "",
        "### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF",
        "n/a",
        "",
        "### TESTING INSTRUCTIONS",
        _reviewer_text(reviewer_output),
        "",
        "### ADDITIONAL INFORMATION",
        additional,
    ]
    if automation_metadata is not None:
        sections.extend(
            [
                "",
                "### AUTOMATION METADATA",
                *(f"- **{key}**: {value}" for key, value in automation_metadata.items()),
            ]
        )
    return "\n".join(sections).rstrip() + "\n"


def render_issue_title(candidate: Candidate, generated_title: str) -> str:
    """Render the lane-specific issue title without propagating assignees."""
    if candidate.lane is Lane.DEPRECATIONS:
        return f"[SIP] {generated_title}"
    return generated_title


def render_issue_body(
    template: str,
    candidate: Candidate,
    *,
    generated_summary: str,
    verification: str = "Pending reviewer verification.",
    references: str | None = None,
) -> str:
    """Render a manager-facing issue body in the selected Superset shape."""
    marker = candidate_marker(candidate.candidate_id)
    if candidate.lane is Lane.CODEQL:
        if SECURITY_ISSUE_MODE != "generic_tracking":
            raise ValueError("unsupported security issue mode")
        values = {
            "### SUMMARY (no exploit detail)": generated_summary,
            "### SCOPE (files or modules only)": candidate.file_path or "<module>",
            "### REMEDIATION STATUS": "Remediation is being tracked by the pipeline.",
            "### VERIFICATION": verification,
            "### REFERENCES (rule ID only)": candidate.rule_id or "<rule>",
        }
        return (
            "\n".join(
                [marker, *sum(([heading, value, ""] for heading, value in values.items()), [])]
            ).rstrip()
            + "\n"
        )

    if candidate.lane is Lane.DEPRECATIONS:
        body = template
        if body.startswith("---"):
            parts = body.split("---", 2)
            body = parts[2].lstrip("\n") if len(parts) == 3 else body
        body = body.replace(
            "Description of the problem to be solved.",
            f"For EM/PM tracking: {generated_summary}",
        )
        return f"{marker}\n\n{body.rstrip()}\n"

    headings = [
        "### Bug description",
        "### Screenshots/recordings",
        "### Environment",
        "### Additional context",
        "### Checklist",
    ]
    content = [
        f"For EM/PM tracking: {generated_summary}",
        "n/a",
        "Superset target revision is recorded on the candidate.",
        verification,
        "- [ ] Pipeline-generated remediation",
    ]
    pairs = ([heading, value, ""] for heading, value in zip(headings, content, strict=True))
    return "\n".join([marker, *sum(pairs, [])]).rstrip() + "\n"


def render_degraded_comment_body(
    template: str,
    candidate: Candidate,
    *,
    generated_summary: str,
    verification: str = "Pending reviewer verification.",
) -> str:
    """Render the degraded issue artifact as a validated PR comment body."""
    body = render_issue_body(
        template,
        candidate,
        generated_summary=generated_summary,
        verification=verification,
    )
    validate_issue_body(body, candidate)
    return body


def validate_pr_body(body: str) -> None:
    """Validate required PR headings, order, and the absence of CHECKLIST."""
    headings = [
        "### SUMMARY",
        "### IMPLEMENTATION PLAN",
        "### TESTS",
        "### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF",
        "### TESTING INSTRUCTIONS",
        "### ADDITIONAL INFORMATION",
    ]
    positions = [body.index(heading) for heading in headings]
    if positions != sorted(positions):
        raise ValueError("PR headings are out of order")
    if "### CHECKLIST" in body:
        raise ValueError("PR body must not add a CHECKLIST heading")
    if "- [ ] Has associated issue:" not in body:
        raise ValueError("PR body lost the Superset checkbox block")
    if "### AUTOMATION METADATA" in body and body.index("### AUTOMATION METADATA") < positions[-1]:
        raise ValueError("automation metadata must be last")


def validate_issue_body(body: str, candidate: Candidate) -> None:
    """Validate the lane-specific issue body used by issues and comments."""
    if candidate_marker(candidate.candidate_id) not in body:
        raise ValueError("issue body lacks candidate marker")
    headings: tuple[str, ...]
    if candidate.lane is Lane.CODEQL:
        headings = (
            "### SUMMARY (no exploit detail)",
            "### SCOPE (files or modules only)",
            "### REMEDIATION STATUS",
            "### VERIFICATION",
            "### REFERENCES (rule ID only)",
        )
    elif candidate.lane is Lane.DEPRECATIONS:
        headings = ("### Motivation", "### Proposed Change")
    else:
        headings = (
            "### Bug description",
            "### Screenshots/recordings",
            "### Environment",
            "### Additional context",
            "### Checklist",
        )
    validate_template_sections(body, headings)


def validate_template_sections(body: str, required_headings: tuple[str, ...]) -> None:
    """Validate heading presence and order for a rendered artifact."""
    positions = [body.index(heading) for heading in required_headings]
    if positions != sorted(positions):
        raise ValueError("artifact headings are out of order")


def validate_pr_title(title: str, regex_text: str) -> bool:
    """Return whether a title satisfies the vendored Superset title regex."""
    return re.fullmatch(regex_text.strip(), title) is not None


def templates_match(vendored: str, live: str) -> bool:
    """Compare a vendored template or regex byte-for-byte with its live source."""
    return vendored == live


def compare_template_files(vendored_path: Path, live_path: Path) -> bool:
    """Compare two checked-in template files without contacting a remote service."""
    return vendored_path.read_bytes() == live_path.read_bytes()


__all__ = [
    "candidate_marker",
    "compare_template_files",
    "render_degraded_comment_body",
    "render_issue_body",
    "render_issue_title",
    "render_pr_body",
    "templates_match",
    "validate_pr_body",
    "validate_issue_body",
    "validate_pr_title",
    "validate_template_sections",
]
