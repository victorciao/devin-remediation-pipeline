"""Deterministic cross-run artifact report tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.config import PipelineConfig
from pipeline.observability.results import (
    LIFECYCLE_PROGRESS,
    RunArtifacts,
    aggregate,
    read_run,
    render_results,
)
from pipeline.schemas import (
    Candidate,
    CandidateState,
    CriterionEvidence,
    EventRecord,
    Lane,
    ReasonCode,
    Tier,
)
from tests.factories import codeql_candidate


def _run(
    name: str,
    candidate: Candidate,
    events: tuple[EventRecord, ...] = (),
) -> RunArtifacts:
    return RunArtifacts(Path(f"/runs/{name}"), Path(f"{name}.jsonl"), (candidate,), events)


def test_results_rendering_is_deterministic() -> None:
    candidate = codeql_candidate(
        candidate_id="published",
        score=30.0,
        tier=Tier.MEDIUM,
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://github.test/issues/1",
    )
    run = RunArtifacts(Path("/runs/one"), Path("candidates-live.jsonl"), (candidate,), ())

    assert render_results((run,), PipelineConfig()) == render_results((run,), PipelineConfig())


def test_results_names_the_run_scoped_column_and_cumulative_kpis() -> None:
    report = render_results((), PipelineConfig())

    assert "| Run | Rows written by this run | First seen here |" in report
    assert (
        "KPI values are cumulative across every recorded run; the per-run table is scoped "
        "to each run."
    ) in report


def test_results_rendering_handles_empty_input() -> None:
    report = render_results((), PipelineConfig())

    assert "no run directory was supplied" in report
    assert "no candidate reached an issue or a pull request" in report


def test_unpublished_candidates_are_excluded_but_counted() -> None:
    published = codeql_candidate(
        candidate_id="published",
        score=30.0,
        tier=Tier.MEDIUM,
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://github.test/issues/1",
    )
    unpublished = codeql_candidate(
        candidate_id="unpublished",
        score=40.0,
        state=CandidateState.DEFERRED,
    )
    run = RunArtifacts(
        Path("/runs/one"),
        Path("candidates-live.jsonl"),
        (published, unpublished),
        (),
    )

    report = render_results((run,), PipelineConfig())

    assert "`published`" in report
    assert "`unpublished`" not in report
    assert "Candidates without an issue or a pull request: 1" in report


def test_later_narrow_run_cannot_erase_a_proven_terminal_outcome() -> None:
    proven = codeql_candidate(
        candidate_id="same",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        issue_url="https://github.test/issues/1",
        pr_url="https://github.test/pull/2",
        criterion_evidence=CriterionEvidence(
            criterion="criterion",
            stage="post_pr",
            satisfied=True,
        ),
    )
    narrowed = proven.model_copy(
        update={
            "state": CandidateState.DEFERRED,
            "reason": ReasonCode.OUT_OF_DISPATCH_SCOPE,
            "issue_url": None,
            "pr_url": None,
            "criterion_evidence": None,
        }
    )
    runs = (
        RunArtifacts(Path("/runs/a"), Path("a.jsonl"), (proven,), ()),
        RunArtifacts(Path("/runs/b"), Path("b.jsonl"), (narrowed,), ()),
    )

    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [proven]
    assert "https://github.test/issues/1" in report
    assert "https://github.test/pull/2" in report
    assert "| awaiting_human_merge |" in report
    assert "| yes |" in report


def test_later_more_advanced_run_wins_over_deferred() -> None:
    deferred = codeql_candidate(
        candidate_id="same",
        state=CandidateState.DEFERRED,
        reason=ReasonCode.OUT_OF_DISPATCH_SCOPE,
    )
    advanced = deferred.model_copy(
        update={
            "state": CandidateState.AWAITING_HUMAN_MERGE,
            "issue_url": "https://github.test/issues/1",
            "pr_url": "https://github.test/pull/2",
        }
    )

    candidates, _ = aggregate(
        (
            RunArtifacts(Path("/runs/a"), Path("a.jsonl"), (deferred,), ()),
            RunArtifacts(Path("/runs/b"), Path("b.jsonl"), (advanced,), ()),
        )
    )

    assert candidates == [advanced]


def test_per_run_first_seen_counts_a_candidate_only_in_its_first_run() -> None:
    first_candidate = codeql_candidate(
        candidate_id="repeated",
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://github.test/issues/1",
    )
    later_candidate = first_candidate.model_copy(update={"state": CandidateState.SESSION_DONE})
    first = RunArtifacts(
        Path("/runs/20260101T000000Z-first"),
        Path("first.jsonl"),
        (first_candidate,),
        (),
    )
    later = RunArtifacts(
        Path("/runs/20260102T000000Z-later"),
        Path("later.jsonl"),
        (later_candidate,),
        (),
    )

    report = render_results((later, first), PipelineConfig())
    rows = [line for line in report.splitlines() if line.startswith("| `2026010")]

    assert rows[0].startswith("| `20260101T000000Z-first` | 1 | 1 |")
    assert rows[1].startswith("| `20260102T000000Z-later` | 1 | 0 |")


def test_per_run_missing_evidence_renders_na_instead_of_zero() -> None:
    candidate = codeql_candidate(candidate_id="without-evidence")
    run = RunArtifacts(
        Path("/runs/20260101T000000Z-missing"),
        Path("missing.jsonl"),
        (candidate,),
        (),
    )

    report = render_results((run,), PipelineConfig())

    assert "| `20260101T000000Z-missing` | 1 | 1 | 0 | 0 | 0 | n/a | n/a |" in report


def test_per_run_artifact_counts_are_differenced_by_url() -> None:
    first_candidate = codeql_candidate(
        candidate_id="first",
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://github.test/issues/1",
        pr_url="https://github.test/pulls/1",
    )
    later_candidate = first_candidate.model_copy(update={"state": CandidateState.PR_CREATED})
    first = RunArtifacts(
        Path("/runs/20260101T000000Z-first"), Path("first"), (first_candidate,), ()
    )
    later = RunArtifacts(
        Path("/runs/20260102T000000Z-later"), Path("later"), (later_candidate,), ()
    )

    report = render_results((first, later), PipelineConfig())
    rows = [line for line in report.splitlines() if line.startswith("| `2026010")]

    assert "| `20260101T000000Z-first` | 1 | 1 | 1 | 0 | 1 |" in rows[0]
    assert "| `20260102T000000Z-later` | 1 | 0 | 0 | 0 | 0 |" in rows[1]


def test_per_run_problems_excludes_superseded_rows() -> None:
    superseded = codeql_candidate(
        candidate_id="old",
        superseded_by="new",
        issue_url="https://github.test/issues/old",
    )
    current = codeql_candidate(candidate_id="new", supersedes="old")
    run = RunArtifacts(
        Path("/runs/20260101T000000Z-superseded"), Path("state"), (superseded, current), ()
    )

    report = render_results((run,), PipelineConfig())

    assert "| `20260101T000000Z-superseded` | 1 | 1 |" in report


def test_per_run_rows_use_event_run_id_attribution() -> None:
    reobserved = codeql_candidate(
        candidate_id="reobserved",
        run_id="previous-run",
        state=CandidateState.MERGED,
        pr_url="https://github.test/pulls/1",
    )
    run_event = EventRecord(
        run_id="current-run",
        lane=Lane.CODEQL,
        candidate_id="reobserved",
    )
    run = RunArtifacts(
        Path("/runs/20260101T000000Z-current"),
        Path("state"),
        (reobserved,),
        (run_event,),
    )

    report = render_results((run,), PipelineConfig())

    assert "| `20260101T000000Z-current` | 0 | 0 | 0 | 0 | 0 | n/a | 0 |" in report


def test_read_run_keeps_rows_written_by_other_runs(tmp_path: Path) -> None:
    run_dir = tmp_path / "20260101T000000Z-current"
    state_dir = run_dir / "state"
    reports_dir = run_dir / "reports"
    state_dir.mkdir(parents=True)
    reports_dir.mkdir()
    current = codeql_candidate(candidate_id="current", run_id="current-run")
    reobserved = codeql_candidate(candidate_id="reobserved", run_id="previous-run")
    legacy = codeql_candidate(candidate_id="legacy", run_id=None)
    (state_dir / "candidates-live.jsonl").write_text(
        "".join(f"{candidate.model_dump_json()}\n" for candidate in (current, reobserved, legacy)),
        encoding="utf-8",
    )
    (reports_dir / "events.jsonl").write_text(
        EventRecord(
            run_id="current-run",
            lane=Lane.CODEQL,
            candidate_id="current",
        ).model_dump_json()
        + "\n",
        encoding="utf-8",
    )

    run = read_run(run_dir)

    assert [candidate.candidate_id for candidate in run.candidates] == [
        "current",
        "reobserved",
        "legacy",
    ]


def test_reobserved_merge_is_latest_cumulative_state_but_not_per_run_problem(
    tmp_path: Path,
) -> None:
    pr_url = "https://github.test/pulls/1"
    pending = codeql_candidate(
        candidate_id="reobserved",
        run_id="previous-run",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        pr_url=pr_url,
        pr_number=1,
    )
    merged = pending.model_copy(
        update={
            "state": CandidateState.MERGED,
            "merged_at": "2026-09-01T00:00:00Z",
            "merge_verified": True,
        }
    )
    previous_event = EventRecord(
        run_id="previous-run",
        lane=Lane.CODEQL,
        candidate_id="reobserved",
        terminal_outcome=CandidateState.AWAITING_HUMAN_MERGE,
        pr_url=pr_url,
        pr_number=1,
    )
    merged_event = EventRecord(
        run_id="current-run",
        lane=Lane.CODEQL,
        candidate_id="reobserved",
        terminal_outcome=CandidateState.MERGED,
        pr_url=pr_url,
        pr_number=1,
        merged_at="2026-09-01T00:00:00Z",
        merge_verified=True,
    )
    runs = (
        RunArtifacts(
            Path("/runs/20260101T000000Z-previous"),
            Path("previous.jsonl"),
            (pending,),
            (previous_event,),
        ),
        RunArtifacts(
            Path("/runs/20260102T000000Z-current"),
            Path("current.jsonl"),
            (merged,),
            (merged_event,),
        ),
    )

    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [merged]
    assert "| merged |" in report
    assert "**Merged Clean:** 1" in report
    assert "| `20260102T000000Z-current` | 0 | 0 | 0 | 0 | 0 | n/a | 0 |" in report


def test_closed_pull_request_settlement_replaces_pending_row() -> None:
    pr_url = "https://github.test/pulls/1"
    pending = codeql_candidate(
        candidate_id="closed",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        issue_url="https://github.test/issues/1",
        pr_url=pr_url,
    )
    closed = pending.model_copy(
        update={
            "state": CandidateState.TERMINAL,
            "reason": ReasonCode.CLOSED_PULL_REQUEST,
        }
    )
    runs = (
        _run("20260101T000000Z-earlier", pending),
        _run("20260102T000000Z-later", closed),
    )

    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [closed]
    assert "| terminal | closed_pull_request |" in report
    assert "**Reached Manual Merge Gate (cumulative):** 0" in report
    assert "**Awaiting Merge Now:** 0" in report


@pytest.mark.parametrize("first_state", ["merged", "closed"])
def test_merged_row_outranks_closed_pull_request_in_either_order(first_state: str) -> None:
    pr_url = "https://github.test/pulls/1"
    merged = codeql_candidate(
        candidate_id="settled",
        state=CandidateState.MERGED,
        pr_url=pr_url,
        merged_at="2026-09-01T00:00:00Z",
        merge_verified=True,
    )
    closed = merged.model_copy(
        update={
            "state": CandidateState.TERMINAL,
            "reason": ReasonCode.CLOSED_PULL_REQUEST,
            "merged_at": None,
            "merge_verified": False,
        }
    )
    merged_run = _run("20260101T000000Z-merged", merged)
    closed_run = _run("20260102T000000Z-closed", closed)
    runs = (merged_run, closed_run) if first_state == "merged" else (closed_run, merged_run)

    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [merged]
    assert "| merged |" in report


def test_later_reenumeration_does_not_replace_pending_merge() -> None:
    pending = codeql_candidate(
        candidate_id="pending",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        pr_url="https://github.test/pulls/1",
    )
    reenumerated = pending.model_copy(
        update={
            "state": CandidateState.ENUMERATED,
            "reason": None,
            "pr_url": None,
        }
    )

    runs = (
        _run("20260101T000000Z-pending", pending),
        _run("20260102T000000Z-reenumerated", reenumerated),
    )
    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [pending]
    assert "| awaiting_human_merge |" in report


def test_other_terminal_reason_remains_below_pending_merge() -> None:
    pending = codeql_candidate(
        candidate_id="terminal",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        pr_url="https://github.test/pulls/1",
    )
    terminal = pending.model_copy(
        update={
            "state": CandidateState.TERMINAL,
            "reason": ReasonCode.SESSION_FAILED,
        }
    )
    runs = (
        _run("20260101T000000Z-pending", pending),
        _run("20260102T000000Z-terminal", terminal),
    )
    candidates, _ = aggregate(runs)
    report = render_results(runs, PipelineConfig())

    assert candidates == [pending]
    assert "| awaiting_human_merge |" in report


def test_lifecycle_progress_ranks_every_candidate_state() -> None:
    assert set(LIFECYCLE_PROGRESS) == set(CandidateState)
    assert LIFECYCLE_PROGRESS[CandidateState.ENUMERATED] < LIFECYCLE_PROGRESS[CandidateState.GATED]
    assert LIFECYCLE_PROGRESS[CandidateState.GATED] < LIFECYCLE_PROGRESS[CandidateState.SCORED]
    assert LIFECYCLE_PROGRESS[CandidateState.SCORED] < LIFECYCLE_PROGRESS[CandidateState.DEFERRED]
    assert (
        LIFECYCLE_PROGRESS[CandidateState.DEFERRED]
        < LIFECYCLE_PROGRESS[CandidateState.BLOCKED_BY_ENCLOSING_SKIP]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.BLOCKED_BY_ENCLOSING_SKIP]
        < LIFECYCLE_PROGRESS[CandidateState.SUPPRESSED_BY_CONTAINMENT]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.SUPPRESSED_BY_CONTAINMENT]
        < LIFECYCLE_PROGRESS[CandidateState.DISPATCHING]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.DISPATCHING]
        < LIFECYCLE_PROGRESS[CandidateState.ISSUE_CREATED]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.ISSUE_CREATED]
        < LIFECYCLE_PROGRESS[CandidateState.SESSION_DONE]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.SESSION_DONE]
        < LIFECYCLE_PROGRESS[CandidateState.VERIFIED]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.VERIFIED] < LIFECYCLE_PROGRESS[CandidateState.PR_CREATED]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.PR_CREATED] < LIFECYCLE_PROGRESS[CandidateState.TERMINAL]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.TERMINAL]
        < LIFECYCLE_PROGRESS[CandidateState.AWAITING_HUMAN_MERGE]
    )
    assert (
        LIFECYCLE_PROGRESS[CandidateState.AWAITING_HUMAN_MERGE]
        < LIFECYCLE_PROGRESS[CandidateState.MERGED]
    )
