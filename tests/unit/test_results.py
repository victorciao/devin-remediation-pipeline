"""Deterministic cross-run artifact report tests."""

from __future__ import annotations

from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.observability.results import RunArtifacts, render_results
from pipeline.schemas import CandidateState, Tier
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

    assert render_results((run,), {}, PipelineConfig()) == render_results(
        (run,), {}, PipelineConfig()
    )


def test_results_rendering_handles_empty_input() -> None:
    report = render_results((), {}, PipelineConfig())

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

    report = render_results((run,), {}, PipelineConfig())

    assert "`published`" in report
    assert "`unpublished`" not in report
    assert "Candidates without an issue or a pull request: 1" in report
