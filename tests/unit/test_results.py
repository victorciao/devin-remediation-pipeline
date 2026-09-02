"""Deterministic cross-run artifact report tests."""

from __future__ import annotations

from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.observability.results import (
    LIFECYCLE_PROGRESS,
    RunArtifacts,
    aggregate,
    render_results,
)
from pipeline.schemas import CandidateState, CriterionEvidence, ReasonCode, Tier
from tests.factories import codeql_candidate


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
