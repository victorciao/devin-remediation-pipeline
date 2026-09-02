"""Tests for shared observability attribution scoping."""

from __future__ import annotations

from pipeline.observability.scope import written_by_run
from tests.factories import codeql_candidate


def test_written_by_run_keeps_unattributed_rows_and_selected_runs() -> None:
    rows = (
        codeql_candidate(candidate_id="legacy", run_id=None),
        codeql_candidate(candidate_id="previous", run_id="run-previous"),
        codeql_candidate(candidate_id="current", run_id="run-current"),
    )

    assert [candidate.candidate_id for candidate in written_by_run(rows, {"run-current"})] == [
        "legacy",
        "current",
    ]


def test_written_by_run_with_no_run_ids_keeps_everything() -> None:
    rows = (
        codeql_candidate(candidate_id="legacy", run_id=None),
        codeql_candidate(candidate_id="current", run_id="run-current"),
    )

    assert written_by_run(rows, ()) == rows
