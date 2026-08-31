"""Pure rendering and validation for Superset issue and pull-request artifacts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from pipeline.config import SECURITY_ISSUE_MODE
from pipeline.schemas import Candidate, Lane, MergeMode


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


def _locator_text(candidate: Candidate) -> str:
    """Render the candidate's lane locator without exploit detail."""
    if candidate.lane is Lane.CODEQL:
        return f"rule `{candidate.rule_id or '<rule>'}` in `{candidate.file_path or '<module>'}`"
    if candidate.lane is Lane.SKIPPED_TESTS:
        return f"`{candidate.nodeid or candidate.stable_locator}`"
    return f"`{candidate.module or '<module>'}:{candidate.qualname or '<symbol>'}`"


def _score_text(candidate: Candidate) -> list[str]:
    """Render the score and its factor breakdown for an issue body."""
    lines = [
        f"- **lane**: {candidate.lane.value}",
        f"- **locator**: {_locator_text(candidate)}",
        f"- **score**: {candidate.score if candidate.score is not None else 'n/a'}"
        f" (tier {candidate.tier.value if candidate.tier is not None else 'n/a'})",
        f"- **business_impact**: {candidate.business_impact}",
        f"- **verifiability**: {candidate.verifiability}",
        f"- **automatability**: {candidate.automatability}",
        f"- **signal_quality**: {candidate.signal_quality}",
        f"- **risk**: {candidate.risk}",
    ]
    lines.extend(f"- **{factor} row**: {row}" for factor, row in candidate.factor_rows.items())
    return lines


def _evidence_text(candidate: Candidate) -> list[str]:
    """Render the criterion and the commands the orchestrator itself ran."""
    evidence = candidate.criterion_evidence
    criterion = candidate.success_criterion or (
        evidence.criterion if evidence is not None else "n/a"
    )
    lines = [f"- **criterion**: {criterion}"]
    if evidence is None:
        lines.append("- **observed**: no orchestrator observation is recorded")
        return lines
    lines.append(
        "- **observed by**: the orchestrator, by executing the commands below; "
        "the session's own account is not evidence"
    )
    lines.extend(f"- **command**: `{command}`" for command in evidence.commands)
    lines.extend(f"- **result**: {observation}" for observation in evidence.observations)
    lines.append(
        "- **satisfied**: "
        + (
            "pending post-PR observation"
            if evidence.satisfied is None
            else ("yes" if evidence.satisfied else "no")
        )
    )
    return lines


def _testing_text(candidate: Candidate) -> str:
    """Render testing instructions from the recorded test evidence."""
    lines: list[str] = []
    if candidate.test_nodeid is not None:
        lines.append(f"- Run `pytest {candidate.test_nodeid}`.")
    lines.extend(f"- Test file: `{path}`" for path in candidate.test_paths)
    if candidate.suite_scope:
        lines.append(
            "- Run the suite covering: "
            + ", ".join(f"`{scope}`" for scope in candidate.suite_scope)
        )
    if not lines:
        lines.append("- Run the suite covering the changed paths.")
    return "\n".join(lines)


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
    *,
    automation_metadata: Mapping[str, object] | None = None,
    issue_number: int | None = None,
    commit_message: str | None = None,
) -> str:
    """Render a PR body carrying the criterion, its evidence and `Closes #<n>`."""
    summary = [
        "Technical remediation for engineers and AI reviewers; candidate surfaced by "
        f"the {candidate.lane.value} lane.",
        candidate.fix_summary or f"Remediates {_locator_text(candidate)}.",
        "",
        "Every commit in this pull request carries the `Signed-off-by` trailer.",
    ]
    if candidate.merge_mode is MergeMode.MANUAL:
        summary.append("A human owns the merge of this pull request; the pipeline never merges it.")
    if issue_number is not None:
        summary.extend(["", f"Closes #{issue_number}"])
    prefix = template.split("### SUMMARY", 1)[0].rstrip()
    additional = _section_body(template, "### ADDITIONAL INFORMATION")
    sections = [
        candidate_marker(candidate.candidate_id),
        *(
            ["### SIMULATED ARTIFACT", "Writes are suppressed; no remote artifact exists."]
            if candidate.artifact_simulated
            else []
        ),
        prefix,
        "### SUMMARY",
        *summary,
        "",
        "### EVIDENCE",
        *_evidence_text(candidate),
        "",
        "### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF",
        "n/a",
        "",
        "### TESTING INSTRUCTIONS",
        _testing_text(candidate),
        "",
        "### ADDITIONAL INFORMATION",
        additional,
    ]
    signoff = _commit_signoff(commit_message)
    if signoff is not None:
        sections.extend(["", signoff])
    if automation_metadata is not None:
        sections.extend(
            [
                "",
                "### AUTOMATION METADATA",
                *(
                    f"- **{key}**: {_metadata_value(value)}"
                    for key, value in dict(automation_metadata).items()
                ),
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
    verification: str = "Remediation status is tracked by the pipeline.",
    references: str | None = None,
    not_automated_reason: str | None = None,
) -> str:
    """Render a tracking issue body in the selected Superset shape."""
    marker = candidate_marker(candidate.candidate_id)
    details = "\n".join(
        [
            *_score_text(candidate),
            *(
                [f"- **not automated**: {not_automated_reason}"]
                if not_automated_reason is not None
                else []
            ),
        ]
    )
    if candidate.lane is Lane.CODEQL:
        if SECURITY_ISSUE_MODE != "generic_tracking":
            raise ArtifactValidationError("unsupported security issue mode")
        if template:
            raise ArtifactValidationError("CodeQL issue rendering does not consume a template")
        values = {
            "### SUMMARY (no exploit detail)": generated_summary,
            "### SCOPE (files or modules only)": candidate.file_path or "<module>",
            "### REMEDIATION STATUS": f"{verification}\n{details}",
            "### VERIFICATION": candidate.success_criterion or verification,
            "### REFERENCES (rule ID only)": candidate.rule_id or "<rule>",
        }
        simulated_heading_codeql = (
            ["### SIMULATED ARTIFACT", "Writes are suppressed; no remote artifact exists.", ""]
            if candidate.artifact_simulated
            else []
        )
        return (
            "\n".join(
                [
                    marker,
                    *simulated_heading_codeql,
                    *sum(([heading, value, ""] for heading, value in values.items()), []),
                ]
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
            f"For EM/PM tracking: {generated_summary}\n\n{details}",
        )
        simulated_heading = (
            "\n### SIMULATED ARTIFACT\nWrites are suppressed; no remote artifact exists.\n"
            if candidate.artifact_simulated
            else "\n"
        )
        return f"{marker}{simulated_heading}\n{body.rstrip()}\n"

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
        f"{verification}\n\n{details}",
        "- [ ] Pipeline-generated remediation",
    ]
    pairs = ([heading, value, ""] for heading, value in zip(headings, content, strict=True))
    simulated = (
        ["### SIMULATED ARTIFACT", "Writes are suppressed; no remote artifact exists.", ""]
        if candidate.artifact_simulated
        else []
    )
    return "\n".join([marker, *simulated, *sum(pairs, [])]).rstrip() + "\n"


def validate_pr_body(body: str, *, issue_number: int | None = None) -> None:
    """Validate required PR headings, order and the cross-link to the issue."""
    headings = [
        "### SUMMARY",
        "### EVIDENCE",
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
    if "Signed-off-by" not in body:
        raise ArtifactValidationError("PR body must state the sign-off trailer")
    if issue_number is not None and f"Closes #{issue_number}" not in body:
        raise ArtifactValidationError("PR body lacks the Closes reference to its tracking issue")
    if "### AUTOMATION METADATA" in body and body.index("### AUTOMATION METADATA") < positions[-1]:
        raise ArtifactValidationError("automation metadata must be last")


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
