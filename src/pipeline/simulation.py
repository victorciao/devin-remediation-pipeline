"""Credential-free simulation artifact generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from pipeline.config import Mode, PipelineConfig
from pipeline.observability.events import EventLog, append_candidate_events
from pipeline.observability.kpis import write_kpi_report
from pipeline.observability.report import write_run_report
from pipeline.schemas import Action, Candidate, Lane, RunEventRecord
from pipeline.state import (
    SETTLED_STATES,
    CandidateStateStore,
    StatePreservationError,
    has_local_artifact,
)
from pipeline.templates.render import (
    render_issue_body,
    render_pr_body,
    validate_issue_body,
    validate_pr_body,
)


def render_run_artifacts(
    candidates: Sequence[Candidate],
    *,
    run_id: str,
    output_dir: Path,
    baseline: dict[str, object],
    config: PipelineConfig,
    fix_outputs: Mapping[str, Mapping[str, object]] | None = None,
    capability_notes: Sequence[str] = (),
    token_login: str | None = None,
    token_scopes: Sequence[str] = (),
    run_events: Sequence[RunEventRecord] = (),
) -> tuple[Path, ...]:
    """Render a complete run without invoking a remote write transport."""
    state_path = (
        output_dir
        / "state"
        / ("candidates-live.jsonl" if config.mode is Mode.LIVE else "candidates.jsonl")
    )
    events_path = output_dir / "reports" / "events.jsonl"
    store = CandidateStateStore(
        state_path,
        artifact_simulated=config.mode is Mode.SIMULATE,
    )
    rendered_candidates = [
        candidate.model_copy(
            update={
                "artifact_simulated": config.mode is Mode.SIMULATE,
            }
        )
        for candidate in candidates
    ]
    for candidate in rendered_candidates:
        try:
            store.append(candidate)
        except StatePreservationError:
            latest = store.latest().get(candidate.candidate_id)
            if (
                latest is None
                or latest.state not in SETTLED_STATES
                or not has_local_artifact(latest)
            ):
                raise

    fixes = fix_outputs or {}
    pr_template = (config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )
    issue_templates = {
        Lane.CODEQL: config.templates_dir / "issues/security_tracking.md",
        Lane.SKIPPED_TESTS: config.templates_dir / "issues/bug_report.yml",
        Lane.DEPRECATIONS: config.templates_dir / "issues/sip.md",
    }
    produced: list[Path] = [state_path, events_path]
    for candidate in rendered_candidates:
        routed = candidate.action in {Action.OPEN_PR, Action.OPEN_ISSUE}
        if config.mode is Mode.LIVE and not routed:
            continue
        template_text = (
            ""
            if candidate.lane is Lane.CODEQL
            else issue_templates[candidate.lane].read_text(encoding="utf-8")
        )
        summary = (
            f"Remediation tracking for {candidate.stable_locator}."
            if config.mode is Mode.LIVE
            else f"SIMULATED remediation for {candidate.candidate_id}."
        )
        issue_body = render_issue_body(
            template_text,
            candidate,
            generated_summary=summary,
        )
        validate_issue_body(issue_body, candidate)
        issue_path = output_dir / "reports" / "issues" / f"{candidate.candidate_id}.md"
        issue_path.parent.mkdir(parents=True, exist_ok=True)
        issue_path.write_text(issue_body, encoding="utf-8")
        produced.append(issue_path)
        if candidate.action is Action.OPEN_PR:
            fix_output = fixes.get(candidate.candidate_id)
            automation_metadata: dict[str, object] = {
                "mode": config.mode.value,
                "writes_suppressed": config.mode is Mode.SIMULATE,
                "artifact_simulated": config.mode is Mode.SIMULATE,
                "ci_evidence_mode": config.ci_evidence_mode.value,
                "diff_range": (f"{candidate.base_sha or 'n/a'}..{candidate.head_sha or 'n/a'}"),
            }
            if fix_output is not None:
                automation_metadata["session_verify_command"] = str(
                    fix_output.get("verify_command", "n/a")
                )
            pr_body = render_pr_body(
                pr_template,
                candidate,
                automation_metadata=automation_metadata,
                issue_number=candidate.issue_number,
            )
            validate_pr_body(pr_body, issue_number=candidate.issue_number)
            pr_path = output_dir / "reports" / "prs" / f"{candidate.candidate_id}.md"
            pr_path.parent.mkdir(parents=True, exist_ok=True)
            pr_path.write_text(pr_body, encoding="utf-8")
            produced.append(pr_path)

    event_log = EventLog(events_path)
    append_candidate_events(
        event_log,
        rendered_candidates,
        run_id=run_id,
        token_login=token_login,
        token_scopes=token_scopes,
        run_events=run_events,
    )
    run_path = output_dir / "reports" / f"run-{run_id}.md"
    write_run_report(
        run_path,
        rendered_candidates,
        run_id=run_id,
        capability_notes=capability_notes,
        mode=config.mode,
    )
    kpi_path = output_dir / "reports" / "kpis.md"
    write_kpi_report(kpi_path, list(rendered_candidates), event_log.read(), baseline, config)
    produced.extend((run_path, kpi_path))
    return tuple(produced)


simulate_run = render_run_artifacts

__all__ = ["render_run_artifacts", "simulate_run"]
