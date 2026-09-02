"""§14.1 durable history seeding and current-run export tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.history import export_rows, seed
from pipeline.schemas import CandidateState, ReasonCode
from tests.factories import codeql_candidate


def write_run(run_dir: Path, rows: list[dict[str, object]]) -> None:
    """Write one historical state artifact in the hosted layout."""
    state_dir = run_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "candidates-live.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_seed_collapses_candidates_and_strips_removed_keys(tmp_path: Path) -> None:
    """Historical rows use last-write-wins order and strip removed keys."""
    older = codeql_candidate(candidate_id="older", run_id="run-old").model_dump(mode="json")
    older["auto_merge_requested"] = False
    newer = codeql_candidate(candidate_id="newer", run_id="run-new").model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-old", [older])
    write_run(history_dir / "20260102T000000Z-new", [newer])

    output = tmp_path / "state" / "candidates-live.jsonl"
    assert seed(history_dir, output) == ["run-old", "run-new"]

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in rows] == ["older", "newer"]
    assert "auto_merge_requested" not in rows[0]
    assert json.loads(output.with_name(output.name + ".seed.json").read_text()) == {
        "seeded_rows": 2,
    }


def test_export_writes_only_rows_from_current_run(tmp_path: Path) -> None:
    """Export excludes the seeded prefix regardless of row run IDs."""
    old = codeql_candidate(candidate_id="old", run_id="run-old").model_dump(mode="json")
    current = codeql_candidate(candidate_id="current", run_id="run-current").model_dump(mode="json")
    state = tmp_path / "state.jsonl"
    state.write_text(json.dumps(old) + "\n" + json.dumps(current) + "\n", encoding="utf-8")
    metadata = tmp_path / "state.jsonl.seed.json"
    metadata.write_text(json.dumps({"run_ids": ["run-old"], "seeded_rows": 1}), encoding="utf-8")

    output = tmp_path / "export.jsonl"
    assert export_rows(state, metadata, output) == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in rows] == ["current"]


def test_seed_writes_one_row_per_line_for_multiline_state(tmp_path: Path) -> None:
    """Seed output remains JSONL when more than one candidate is present."""
    first = codeql_candidate(candidate_id="first", run_id="run-one").model_dump(mode="json")
    second = codeql_candidate(candidate_id="second", run_id="run-two").model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-one", [first, second])

    output = tmp_path / "state.jsonl"
    seed(history_dir, output)

    rows = output.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2
    assert [json.loads(line)["candidate_id"] for line in rows] == ["first", "second"]


def test_seed_preserves_settled_row_over_later_weaker_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seed applies the settlement rule when a later row weakens a settled PR."""
    first = codeql_candidate(
        candidate_id="same",
        run_id="run-one",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        pr_number=2,
        pr_url="https://github.test/pull/2",
    ).model_dump(mode="json")
    second = codeql_candidate(
        candidate_id="same",
        run_id="run-two",
        reason=ReasonCode.BUDGET_OVERFLOW,
        state=CandidateState.DEFERRED,
    ).model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-one", [first])
    write_run(history_dir / "20260102T000000Z-two", [second])

    output = tmp_path / "state.jsonl"
    assert seed(history_dir, output) == ["run-one"]

    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["state"] == "awaiting_human_merge"
    assert rows[0]["pr_url"] == "https://github.test/pull/2"
    assert "preserved 1 settled rows" in capsys.readouterr().out


def test_seed_accepts_historical_row_without_run_id(tmp_path: Path) -> None:
    """A legacy row without run metadata remains valid historical state."""
    row = codeql_candidate(candidate_id="no-run-id", run_id=None).model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-run", [row])

    output = tmp_path / "state.jsonl"
    assert seed(history_dir, output) == []
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line]
    assert rows[0]["candidate_id"] == "no-run-id"
    assert json.loads(output.with_name(output.name + ".seed.json").read_text()) == {
        "seeded_rows": 1
    }


def test_seed_skips_simulated_rows_and_quarantines_malformed_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Simulation identity cannot enter LIVE state, and bad history is quarantined."""
    simulated = codeql_candidate(candidate_id="simulated", artifact_simulated=True).model_dump(
        mode="json"
    )
    history_dir = tmp_path / "history"
    run_dir = history_dir / "20260101T000000Z-run"
    write_run(run_dir, [simulated])
    state = run_dir / "state" / "candidates-live.jsonl"
    state.write_text(state.read_text() + "{bad json\n", encoding="utf-8")

    output = tmp_path / "state.jsonl"
    assert seed(history_dir, output) == []
    assert output.read_text() == ""
    assert output.with_name(output.name + ".corrupt").read_text() == "{bad json\n"
    assert not state.with_suffix(state.suffix + ".corrupt").exists()
    summary = capsys.readouterr().out
    assert "skipped runs 0" in summary
    assert "skipped simulated rows 1" in summary


def test_seed_reports_unreadable_runs_and_simulated_rows_separately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seed distinguishes unreadable run directories from simulated rows."""
    history_dir = tmp_path / "history"
    (history_dir / "20260101T000000Z-unreadable").mkdir(parents=True)
    simulated = codeql_candidate(candidate_id="simulated", artifact_simulated=True).model_dump(
        mode="json"
    )
    write_run(history_dir / "20260102T000000Z-simulated", [simulated])

    output = tmp_path / "state.jsonl"
    assert seed(history_dir, output) == []

    summary = capsys.readouterr().out
    assert "skipped runs 1" in summary
    assert "skipped simulated rows 1" in summary


def test_export_rejects_state_shorter_than_seeded_prefix(tmp_path: Path) -> None:
    """A truncated live state file is corruption, not an empty export."""
    state = tmp_path / "state.jsonl"
    state.write_text("", encoding="utf-8")
    metadata = tmp_path / "seed.json"
    metadata.write_text(json.dumps({"seeded_rows": 1}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="fewer than seeded rows"):
        export_rows(state, metadata, tmp_path / "export.jsonl")


def test_seed_export_reseed_round_trip_preserves_live_identity(tmp_path: Path) -> None:
    """Two simulated runs cannot regress a seeded live candidate or lose its PR."""
    candidate_id = "05adf3ca-round-trip"
    live = codeql_candidate(
        candidate_id=candidate_id,
        run_id="live-run",
        state=CandidateState.AWAITING_HUMAN_MERGE,
        issue_number=1,
        issue_url="https://github.test/issues/1",
        pr_number=2,
        pr_url="https://github.test/pull/2",
    ).model_dump(mode="json")
    simulated = codeql_candidate(
        candidate_id=candidate_id,
        run_id="simulate-run",
        state=CandidateState.DEFERRED,
        reason=ReasonCode.BUDGET_OVERFLOW,
        issue_number=1,
        issue_url="https://github.test/issues/1",
        pr_number=2,
        pr_url="https://github.test/pull/2",
        artifact_simulated=True,
    ).model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-live", [live])
    seeded = tmp_path / "seeded.jsonl"
    seed(history_dir, seeded)

    with seeded.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(simulated) + "\n")
    exported = tmp_path / "export.jsonl"
    export_rows(seeded, seeded.with_name(seeded.name + ".seed.json"), exported)
    exported_rows = exported.read_text(encoding="utf-8").splitlines()
    assert len(exported_rows) == 1

    write_run(history_dir / "20260102T000000Z-simulated", [simulated])
    reseeded = tmp_path / "reseeded.jsonl"
    seed(history_dir, reseeded)
    rows = [json.loads(line) for line in reseeded.read_text(encoding="utf-8").splitlines()]
    preserved = next(row for row in rows if row["candidate_id"] == candidate_id)
    assert preserved["state"] == "awaiting_human_merge"
    assert preserved["pr_url"] == "https://github.test/pull/2"


def test_export_treats_missing_seed_metadata_as_empty_seed(tmp_path: Path) -> None:
    """A first hosted export has no seed sidecar and exports every state row."""
    current = codeql_candidate(candidate_id="current", run_id="run-current").model_dump(mode="json")
    state = tmp_path / "state.jsonl"
    state.write_text(json.dumps(current) + "\n", encoding="utf-8")

    output = tmp_path / "export.jsonl"
    assert export_rows(state, tmp_path / "missing.seed.json", output) == 0
    assert output.read_text(encoding="utf-8").strip() == json.dumps(current, sort_keys=True)


def test_export_rejects_malformed_seed_metadata(tmp_path: Path) -> None:
    """Malformed seed metadata remains an explicit history error."""
    metadata = tmp_path / "seed.json"
    metadata.write_text("not json", encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        export_rows(tmp_path / "state.jsonl", metadata, tmp_path / "export.jsonl")
