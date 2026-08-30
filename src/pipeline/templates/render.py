"""Pure rendering and validation for Superset issue and pull-request artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pipeline.config import SECURITY_ISSUE_MODE
from pipeline.schemas import Candidate, Lane


class ArtifactValidationError(ValueError):
    """Raised when a rendered artifact violates its vendored contract."""


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
    raise ArtifactValidationError(f"template lacks required heading: {heading}")


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


def _commit_signoff(commit_message: str | None) -> str | None:
    if commit_message is None:
        return None
    for line in reversed(commit_message.splitlines()):
        candidate = line.strip()
        if re.fullmatch(r"Signed-off-by:\s+[^<\n]+<[^<>\n]+>", candidate):
            return candidate
    return None


def _metadata_value(value: object) -> str:
    """Render evidence values without changing command text."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return str(value)


def render_pr_body(
    template: str,
    candidate: Candidate,
    planner_output: Mapping[str, object],
    reviewer_output: Mapping[str, object],
    *,
    automation_metadata: Mapping[str, object] | None = None,
    issue_number: int | None = None,
    commit_message: str | None = None,
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
    signoff = _commit_signoff(commit_message)
    if signoff is not None:
        sections.extend(["", signoff])
    if automation_metadata is not None:
        metadata = dict(automation_metadata)
        sections.extend(
            [
                "",
                "### AUTOMATION METADATA",
                *(f"- **{key}**: {_metadata_value(value)}" for key, value in metadata.items()),
            ]
        )
        if metadata.get("ci_evidence_mode") == "local":
            sections.extend(
                [
                    "- **evidence_label**: self-reported by automation",
                    "- **implementer_commands_run**: "
                    f"{_metadata_value(metadata.get('implementer_commands_run', 'n/a'))}",
                    "- **reviewer_pre_fix_failure**: "
                    f"{_metadata_value(metadata.get('reviewer_pre_fix_failure', 'n/a'))}",
                    "- **reviewer_post_fix_result**: "
                    f"{_metadata_value(metadata.get('reviewer_post_fix_result', 'n/a'))}",
                    f"- **diff_range**: {_metadata_value(metadata.get('diff_range', 'n/a'))}",
                ]
            )
    return "\n".join(sections).rstrip() + "\n"


def render_issue_title(candidate: Candidate, generated_title: str) -> str:
    """Render the lane-specific issue title without propagating assignees."""
    if candidate.lane is Lane.DEPRECATIONS:
        return f"[SIP] {generated_title}"
    return generated_title


def render_pr_title(candidate: Candidate) -> str:
    """Render a short, conventional Superset title for any remediation lane."""

    def subject(value: str, fallback: str) -> str:
        normalized = re.sub(r"\s+", " ", value).strip()
        return (normalized or fallback)[:60].rstrip(" .,;:")

    if candidate.lane is Lane.CODEQL:
        return f"fix(security): remediate {subject(candidate.rule_id or '', 'CodeQL alert')}"
    if candidate.lane is Lane.SKIPPED_TESTS:
        nodeid = candidate.nodeid or candidate.stable_locator
        return f"test: re-enable {subject(nodeid.rsplit('::', 1)[-1], 'skipped test')}"
    symbol = candidate.qualname or candidate.stable_locator.rsplit(":", 1)[-1]
    return f"refactor: remove deprecated {subject(symbol, 'deprecated symbol')}"


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
            raise ArtifactValidationError("unsupported security issue mode")
        values = {
            "### SUMMARY (no exploit detail)": generated_summary,
            "### SCOPE (files or modules only)": candidate.file_path or "<module>",
            "### REMEDIATION STATUS": "Remediation is being tracked by the pipeline.",
            "### VERIFICATION": verification,
            "### REFERENCES (rule ID only)": candidate.rule_id or "<rule>",
        }
        body = template.rstrip()
        for heading, value in values.items():
            body = body.replace(f"{heading}\n", f"{heading}\n{value}\n", 1)
        return f"{marker}\n{body}\n"

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
        raise ArtifactValidationError("PR headings are out of order")
    if "### CHECKLIST" in body:
        raise ArtifactValidationError("PR body must not add a CHECKLIST heading")
    if "- [ ] Has associated issue:" not in body:
        raise ArtifactValidationError("PR body lost the Superset checkbox block")
    if "### AUTOMATION METADATA" in body and body.index("### AUTOMATION METADATA") < positions[-1]:
        raise ArtifactValidationError("automation metadata must be last")
    if "- **ci_evidence_mode**: local" in body:
        required_evidence = (
            "- **evidence_label**: self-reported by automation",
            "- **implementer_commands_run**:",
            "- **reviewer_pre_fix_failure**:",
            "- **reviewer_post_fix_result**:",
            "- **diff_range**:",
        )
        if any(item not in body for item in required_evidence):
            raise ArtifactValidationError("local PR body lacks automation evidence block")
        for field in (
            "implementer_commands_run",
            "reviewer_pre_fix_failure",
            "reviewer_post_fix_result",
            "diff_range",
        ):
            prefix = f"- **{field}**:"
            values = [
                line[len(prefix) :].strip() for line in body.splitlines() if line.startswith(prefix)
            ]
            value = values[-1] if values else ""
            if not value or value.lower() in {"n/a", "none", "null"}:
                raise ArtifactValidationError(f"local PR body lacks value for {field}")
            if (
                field == "diff_range"
                and re.fullmatch(r"[0-9a-f]{7,40}\.\.[0-9a-f]{7,40}", value) is None
            ):
                raise ArtifactValidationError("local PR body has an invalid diff_range")


def validate_issue_body(body: str, candidate: Candidate) -> None:
    """Validate the lane-specific issue body used by issues and comments."""
    if candidate_marker(candidate.candidate_id) not in body:
        raise ArtifactValidationError("issue body lacks candidate marker")
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
        raise ArtifactValidationError("artifact headings are out of order")


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
    "ArtifactValidationError",
    "compare_template_files",
    "render_degraded_comment_body",
    "render_issue_body",
    "render_issue_title",
    "render_pr_body",
    "render_pr_title",
    "templates_match",
    "validate_pr_body",
    "validate_issue_body",
    "validate_pr_title",
    "validate_template_sections",
]
