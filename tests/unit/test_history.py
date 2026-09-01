"""§14.1 durable history seeding and current-run export tests."""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.history import export_rows, seed
from tests.factories import codeql_candidate


def write_run(run_dir: Path, rows: list[dict[str, object]]) -> None:
    """Write one historical state artifact in the hosted layout."""
    state_dir = run_dir / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "candidates-live.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )


def test_seed_preserves_run_order_and_strips_removed_keys(tmp_path: Path) -> None:
    """Historical rows are copied in chronological directory and original row order."""
    older = codeql_candidate(candidate_id="older", run_id="run-old").model_dump(mode="json")
    older["auto_merge_requested"] = False
    newer = codeql_candidate(candidate_id="newer", run_id="run-new").model_dump(mode="json")
    history_dir = tmp_path / "history"
    write_run(history_dir / "20260101T000000Z-old", [older])
    write_run(history_dir / "20260102T000000Z-new", [newer])

    output = tmp_path / "state" / "candidates-live.jsonl"
    assert seed(history_dir, output) == ["run-new", "run-old"]

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in rows] == ["older", "newer"]
    assert "auto_merge_requested" not in rows[0]
    assert json.loads(output.with_name(output.name + ".seed.json").read_text()) == {
        "run_ids": ["run-new", "run-old"]
    }


def test_export_writes_only_rows_from_current_run(tmp_path: Path) -> None:
    """Export excludes every row whose run was included in the seed."""
    old = codeql_candidate(candidate_id="old", run_id="run-old").model_dump(mode="json")
    current = codeql_candidate(candidate_id="current", run_id="run-current").model_dump(mode="json")
    state = tmp_path / "state.jsonl"
    state.write_text(json.dumps(old) + "\n" + json.dumps(current) + "\n", encoding="utf-8")
    metadata = tmp_path / "state.jsonl.seed.json"
    metadata.write_text(json.dumps({"run_ids": ["run-old"]}), encoding="utf-8")

    output = tmp_path / "export.jsonl"
    assert export_rows(state, metadata, output) == 0
    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [row["candidate_id"] for row in rows] == ["current"]


def test_export_treats_missing_seed_metadata_as_empty(tmp_path: Path) -> None:
    """A missing seed sidecar leaves every valid state row eligible for export."""
    current = codeql_candidate(candidate_id="current", run_id="run-current").model_dump(mode="json")
    state = tmp_path / "state.jsonl"
    state.write_text(json.dumps(current) + "\n", encoding="utf-8")

    output = tmp_path / "export.jsonl"
    assert export_rows(state, tmp_path / "missing.seed.json", output) == 0
    assert output.read_text(encoding="utf-8").count('"candidate_id"') == 1
