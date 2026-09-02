"""Deterministic cross-run results report rendered from persisted run artifacts."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.observability.kpis import (
    KpiValue,
    NotApplicable,
    compute_kpis,
)
from pipeline.schemas import (
    REMOVED_LEGACY_CANDIDATE_KEYS,
    Candidate,
    CandidateState,
    EventRecord,
    ReasonCode,
)

_SHORT_ID_LENGTH = 12
_KPI_SECTION_KEYS = ("deferred_by_reason",)
LIFECYCLE_PROGRESS: dict[CandidateState, int] = {
    CandidateState.ENUMERATED: 0,
    CandidateState.GATED: 1,
    CandidateState.SCORED: 2,
    CandidateState.DEFERRED: 3,
    CandidateState.BLOCKED_BY_ENCLOSING_SKIP: 4,
    CandidateState.SUPPRESSED_BY_CONTAINMENT: 5,
    CandidateState.DISPATCHING: 6,
    CandidateState.ISSUE_CREATED: 7,
    CandidateState.SESSION_DONE: 8,
    CandidateState.VERIFIED: 9,
    CandidateState.PR_CREATED: 10,
    CandidateState.TERMINAL: 11,
    CandidateState.AWAITING_HUMAN_MERGE: 12,
    CandidateState.MERGED: 13,
}


class ResultsInputError(RuntimeError):
    """Raised when a run directory does not carry the artifacts a report needs."""


@dataclass(frozen=True)
class RunArtifacts:
    """One run's persisted candidate rows and Layer 1 events."""

    run_dir: Path
    state_path: Path
    candidates: tuple[Candidate, ...]
    events: tuple[EventRecord, ...]

    @property
    def run_ids(self) -> tuple[str, ...]:
        """Every run id recorded in this directory's events, in first-seen order."""
        seen: list[str] = []
        for event in self.events:
            if event.run_id not in seen:
                seen.append(event.run_id)
        return tuple(seen)


def _read_historical_events(path: Path) -> list[EventRecord]:
    """Read historical events while stripping fields removed from live contracts."""
    if not path.exists():
        return []
    events: list[EventRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            continue
        if payload.get("event_type") in {
            "run_capabilities",
            "ci_mode_transition",
            "marker_search_failure",
        }:
            continue
        payload = {
            key: value for key, value in payload.items() if key not in REMOVED_LEGACY_CANDIDATE_KEYS
        }
        events.append(EventRecord.model_validate(payload, strict=False))
    return events


def state_path(run_dir: Path) -> Path:
    """Return the one state JSONL file a run directory persisted."""
    state_dir = run_dir / "state"
    paths = sorted(path for path in state_dir.glob("*.jsonl") if path.is_file())
    if not paths:
        raise ResultsInputError(f"no state JSONL file under {state_dir}")
    if len(paths) > 1:
        raise ResultsInputError(f"ambiguous state JSONL files under {state_dir}")
    return paths[0]


def read_run(run_dir: Path) -> RunArtifacts:
    """Read the latest row per candidate and every event one run persisted."""
    path = state_path(run_dir)
    latest: dict[str, Candidate] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        if isinstance(payload, dict):
            payload = {
                key: value
                for key, value in payload.items()
                if key not in REMOVED_LEGACY_CANDIDATE_KEYS
            }
        candidate = Candidate.model_validate(payload, strict=False)
        latest[candidate.candidate_id] = candidate
    events = _read_historical_events(run_dir / "reports" / "events.jsonl")
    return RunArtifacts(
        run_dir=run_dir,
        state_path=path,
        candidates=tuple(latest.values()),
        events=tuple(events),
    )


def aggregate(runs: Sequence[RunArtifacts]) -> tuple[list[Candidate], list[EventRecord]]:
    """Merge runs by lifecycle progress, breaking ties in favor of later runs."""
    latest: dict[str, tuple[int, int, Candidate]] = {}
    events: list[EventRecord] = []
    for run_index, run in enumerate(runs):
        for candidate in run.candidates:
            rank = LIFECYCLE_PROGRESS[candidate.state]
            previous = latest.get(candidate.candidate_id)
            if previous is None or (rank, run_index) >= (previous[0], previous[1]):
                latest[candidate.candidate_id] = (rank, run_index, candidate)
        events.extend(run.events)
    return [candidate for _, _, candidate in latest.values()], events


def _sorted_candidates(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Order candidates by lane, descending score, then identity."""
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.lane.value,
            -(candidate.score if candidate.score is not None else 0.0),
            candidate.candidate_id,
        ),
    )


def _artifact_urls(
    candidate: Candidate,
    events: Sequence[EventRecord],
) -> tuple[str | None, str | None]:
    """Return artifact URLs from the latest state row, with event evidence as fallback."""
    issue_url = candidate.issue_url
    pr_url = candidate.pr_url
    for event in events:
        if event.candidate_id != candidate.candidate_id:
            continue
        issue_url = issue_url or event.issue_url
        pr_url = pr_url or event.pr_url
    return issue_url, pr_url


def _cell(value: object) -> str:
    """Render one table cell, never inventing a value that is not recorded."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _criterion_cell(candidate: Candidate) -> str:
    """Render the criterion outcome the orchestrator actually observed."""
    evidence = candidate.criterion_evidence
    return _cell(evidence.satisfied) if evidence is not None else "n/a"


def _kpi_cell(value: KpiValue) -> str:
    """Render one KPI value, distinguishing unknown from zero."""
    if value is None:
        return "n/a"
    if isinstance(value, NotApplicable):
        return f"n/a ({value.reason.value})"
    if isinstance(value, dict):
        return (
            ", ".join(f"{key}={value[key]}" for key in sorted(value)) if value else "none recorded"
        )
    return str(value)


def render_results(
    runs: Sequence[RunArtifacts],
    config: PipelineConfig,
) -> str:
    """Render the cross-run results report; every number comes from the artifacts."""
    runs = tuple(sorted(runs, key=lambda run: run.run_dir.name))
    candidates, events = aggregate(runs)
    ordered = _sorted_candidates(candidates)
    published = [candidate for candidate in ordered if any(_artifact_urls(candidate, events))]
    metrics = compute_kpis(candidates, events, config)
    lines = [
        "# Remediation results",
        "",
        "Generated by `python -m pipeline.observability.results` from persisted run "
        "artifacts. Every value below is read from those files.",
        "",
        "## Runs",
        "",
    ]
    if runs:
        for run in runs:
            run_ids = ", ".join(f"`{run_id}`" for run_id in run.run_ids) or "none recorded"
            lines.append(f"- `{run.run_dir.name}` (state `{run.state_path.name}`): {run_ids}")
    else:
        lines.append("- no run directory was supplied")
    lines.extend(
        [
            "",
            "## Per run",
            "",
            "| Run | Problems seen | First seen here | Issues created | Sessions | PRs opened "
            "| Criterion satisfied | Session failures |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    earlier_candidate_ids: set[str] = set()
    for run in runs:
        run_candidates = list(run.candidates)
        run_events = list(run.events)
        run_metrics = compute_kpis(run_candidates, run_events, config)
        run_candidate_ids = {candidate.candidate_id for candidate in run_candidates}
        first_seen = len(run_candidate_ids - earlier_candidate_ids)
        criterion_ids = {
            event.candidate_id
            for event in run_events
            if event.criterion_evidence is not None and event.criterion_evidence.satisfied is True
        }
        session_failures = sum(
            event.reason
            in {
                ReasonCode.SESSION_CEILING,
                ReasonCode.SESSION_FAILED,
                ReasonCode.SESSION_BLOCKED,
            }
            for event in run_events
        )
        criterion_evidence_available = any(
            event.criterion_evidence is not None for event in run_events
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{run.run_dir.name}`",
                    str(len(run_candidates)),
                    str(first_seen),
                    _kpi_cell(run_metrics["issues_created"]),
                    _kpi_cell(run_metrics["sessions_created"]),
                    _kpi_cell(run_metrics["pull_requests_opened"]),
                    _cell(len(criterion_ids) if criterion_evidence_available else None),
                    _cell(session_failures if run_events else None),
                )
            )
            + " |"
        )
        earlier_candidate_ids.update(run_candidate_ids)
    if not runs:
        lines.append("| _no run directory was supplied_ | | | | | | | |")
    lines.extend(
        [
            "",
            "## Published candidates",
            "",
            "| Candidate | Lane | Tier | Score | Issue | PR | Criterion satisfied "
            "| State | Reason |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for candidate in published:
        issue_url, pr_url = _artifact_urls(candidate, events)
        lines.append(
            "| "
            + " | ".join(
                (
                    f"`{candidate.candidate_id[:_SHORT_ID_LENGTH]}`",
                    candidate.lane.value,
                    _cell(candidate.tier.value if candidate.tier is not None else None),
                    _cell(candidate.score),
                    _cell(issue_url),
                    _cell(pr_url),
                    _criterion_cell(candidate),
                    candidate.state.value,
                    _cell(candidate.reason.value if candidate.reason is not None else None),
                )
            )
            + " |"
        )
    if not published:
        lines.append("| _no candidate reached an issue or a pull request_ | | | | | | | | |")
    lines.extend(
        [
            "",
            f"Candidates without an issue or a pull request: {len(ordered) - len(published)} "
            "(excluded from the table above, counted in every total below).",
            "",
            "## KPI snapshot",
            "",
        ]
    )
    for name, value in metrics.items():
        if name in _KPI_SECTION_KEYS:
            continue
        label = name.replace("_", " ").title()
        if name == "dispatched_pr":
            label = "Problems With Pull Request"
        elif name == "pull_requests_opened":
            label = "Pull Requests Opened"
        lines.append(f"- **{label}:** {_kpi_cell(value)}")
    lines.extend(["", "## Deferred by reason", ""])
    deferred = metrics["deferred_by_reason"]
    if isinstance(deferred, dict) and deferred:
        lines.extend(f"- **{reason}:** {count}" for reason, count in sorted(deferred.items()))
    else:
        lines.append("- none recorded")
    merged = [
        candidate
        for candidate in ordered
        if candidate.state is CandidateState.MERGED
        and candidate.merged_at is not None
        and candidate.merge_verified
    ]
    lines.extend(
        [
            "",
            "## Merges",
            "",
            f"- Candidates observed merged (a human merges; the pipeline never does): "
            f"{len(merged)} "
            "(rows in state `merged` with a verified merge observation).",
            "",
        ]
    )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    """Render the results report for one or more run directories."""
    parser = argparse.ArgumentParser(
        prog="python -m pipeline.observability.results",
        description="Render RESULTS.md from persisted remediation run artifacts.",
    )
    parser.add_argument("--run-dir", action="append", default=[], type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    runs = [read_run(run_dir) for run_dir in args.run_dir]
    report = render_results(runs, PipelineConfig())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
