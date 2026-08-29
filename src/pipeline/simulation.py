"""Credential-free simulation artifact generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.observability.events import EventLog, append_candidate_events
from pipeline.observability.kpis import write_kpi_report
from pipeline.observability.report import write_run_report
from pipeline.schemas import Action, Candidate, Lane, RunEventRecord
from pipeline.state import CandidateStateStore
from pipeline.templates.render import (
    render_degraded_comment_body,
    render_issue_body,
    render_pr_body,
    validate_issue_body,
    validate_pr_body,
)


def simulate_run(
    candidates: Sequence[Candidate],
    *,
    run_id: str,
    output_dir: Path,
    baseline: dict[str, object],
    config: PipelineConfig,
    planner_outputs: Mapping[str, Mapping[str, object]] | None = None,
    reviewer_outputs: Mapping[str, Mapping[str, object]] | None = None,
    capability_notes: Sequence[str] = (),
    token_login: str | None = None,
    token_scopes: Sequence[str] = (),
    run_events: Sequence[RunEventRecord] = (),
) -> tuple[Path, ...]:
    """Render a complete run without invoking a remote write transport."""
    state_path = output_dir / "state" / "candidates.jsonl"
    events_path = output_dir / "reports" / "events.jsonl"
    store = CandidateStateStore(state_path)
    for candidate in candidates:
        store.append(candidate)

    planner = planner_outputs or {}
    reviewer = reviewer_outputs or {}
    pr_template = (config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )
    issue_templates = {
        Lane.CODEQL: config.templates_dir / "issues/security_tracking.md",
        Lane.SKIPPED_TESTS: config.templates_dir / "issues/bug_report.yml",
        Lane.DEPRECATIONS: config.templates_dir / "issues/sip.md",
    }
    produced: list[Path] = [state_path, events_path]
    for candidate in candidates:
        template_text = issue_templates[candidate.lane].read_text(encoding="utf-8")
        if not config.has_issues and config.issue_sink.value == "pr_comment":
            issue_body = render_degraded_comment_body(
                template_text,
                candidate,
                generated_summary=f"Simulated remediation for {candidate.candidate_id}.",
            )
        else:
            issue_body = render_issue_body(
                template_text,
                candidate,
                generated_summary=f"Simulated remediation for {candidate.candidate_id}.",
            )
            validate_issue_body(issue_body, candidate)
        issue_path = output_dir / "reports" / "issues" / f"{candidate.candidate_id}.md"
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(issue_body, encoding="utf-8")
        produced.append(issue_path)
        if candidate.action is Action.OPEN_PR:
            pr_body = render_pr_body(
                pr_template,
                candidate,
                planner.get(candidate.candidate_id, {}),
                reviewer.get(candidate.candidate_id, {}),
                automation_metadata={
                    "mode": config.mode.value,
                    "would_write": config.mode.value == "simulate",
                },
            )
            validate_pr_body(pr_body)
            pr_path = output_dir / "reports" / "prs" / f"{candidate.candidate_id}.md"
            pr_path.parent.mkdir(parents=True, exist_ok=True)
            pr_path.write_text(pr_body, encoding="utf-8")
            produced.append(pr_path)

    event_log = EventLog(events_path)
    append_candidate_events(
        event_log,
        candidates,
        run_id=run_id,
        token_login=token_login,
        token_scopes=token_scopes,
        run_events=run_events,
    )
    run_path = output_dir / "reports" / f"run-{run_id}.md"
    write_run_report(
        run_path,
        candidates,
        run_id=run_id,
        capability_notes=capability_notes,
    )
    if not config.has_issues and config.issue_sink.value == "pr_comment":
        run_path.write_text(
            run_path.read_text(encoding="utf-8") + "\n- **Artifact mode:** `artifact_degraded`\n",
            encoding="utf-8",
        )
    kpi_path = output_dir / "reports" / "kpis.md"
    write_kpi_report(kpi_path, list(candidates), event_log.read(), baseline, config)
    produced.extend((run_path, kpi_path))
    return tuple(produced)


__all__ = ["simulate_run"]
