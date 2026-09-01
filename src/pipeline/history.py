"""Seed and export append-only state across hosted remediation runs."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, TextIO, cast

from pydantic import ValidationError

from pipeline.observability.results import ResultsInputError, state_path
from pipeline.schemas import REMOVED_LEGACY_CANDIDATE_KEYS, Candidate
from pipeline.state import settlement_violation


def _strip_legacy_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip removed fields so historical rows can be validated by live models."""
    return {
        key: value for key, value in payload.items() if key not in REMOVED_LEGACY_CANDIDATE_KEYS
    }


def _write_candidate(handle: TextIO, candidate: Candidate) -> None:
    """Write one state row using the state store's canonical serialization."""
    handle.write(json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n")


def _read_candidates(path: Path, *, quarantine: Path) -> tuple[list[Candidate], int]:
    """Read historical rows and quarantine malformed lines beside the seed output."""
    rows: list[Candidate] = []
    quarantined = 0
    seen = (
        set(quarantine.read_text(encoding="utf-8").splitlines()) if quarantine.exists() else set()
    )
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise TypeError("state row is not an object")
            candidate = Candidate.model_validate(_strip_legacy_keys(payload), strict=False)
        except (json.JSONDecodeError, TypeError, ValidationError):
            if line not in seen:
                with quarantine.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                seen.add(line)
                quarantined += 1
            continue
        rows.append(candidate)
    return rows, quarantined


def seed(history_dir: Path, output: Path) -> list[str]:
    """Seed one last-write-wins row per candidate from historical runs."""
    if output.exists() and output.stat().st_size > 0:
        raise RuntimeError(f"refusing to seed non-empty state file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_dirs = (
        sorted(path for path in history_dir.iterdir() if path.is_dir())
        if history_dir.exists()
        else []
    )
    # Hosted run directories use UTC timestamp prefixes, so lexicographic order is chronological.
    latest: dict[str, Candidate] = {}
    run_ids: list[str] = []
    quarantined = 0
    skipped_runs = 0
    skipped_simulated = 0
    preserved = 0
    quarantine = output.with_name(output.name + ".corrupt")
    for run_dir in run_dirs:
        try:
            source = state_path(run_dir)
        except ResultsInputError as exc:
            print(f"warning: skipping {run_dir}: {exc}")
            skipped_runs += 1
            continue
        rows, row_quarantine = _read_candidates(source, quarantine=quarantine)
        quarantined += row_quarantine
        for candidate in rows:
            if candidate.artifact_simulated:
                skipped_simulated += 1
                continue
            previous = latest.get(candidate.candidate_id)
            if previous is not None and settlement_violation(previous, candidate) is not None:
                preserved += 1
                continue
            if candidate.run_id and candidate.run_id not in run_ids:
                run_ids.append(candidate.run_id)
            latest[candidate.candidate_id] = candidate

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            for candidate in latest.values():
                _write_candidate(cast(TextIO, handle), candidate)
        os.replace(temp_path, output)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    output.with_name(output.name + ".seed.json").write_text(
        json.dumps({"seeded_rows": len(latest)}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"seeded {len(latest)} rows from {len(run_ids)} run ids into {output}"
        f" (quarantined {quarantined}, skipped runs {skipped_runs},"
        f" skipped simulated rows {skipped_simulated})"
    )
    print(f"preserved {preserved} settled rows")
    return run_ids


def _seeded_rows(path: Path) -> int:
    """Read the seeded line offset from required metadata."""
    if not path.exists():
        raise RuntimeError(f"missing seed metadata: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid seed metadata: {path}")
    value = payload.get("seeded_rows", 0)
    if not isinstance(value, int) or value < 0:
        raise RuntimeError(f"invalid seeded row count: {path}")
    return value


def export_rows(state: Path, seed_meta: Path, output: Path) -> int:
    """Export only rows appended after the seeded line offset."""
    seeded = _seeded_rows(seed_meta)
    rows: list[Candidate] = []
    if state.exists():
        lines = state.read_text(encoding="utf-8").splitlines()
        if len(lines) < seeded:
            raise RuntimeError(f"state file has fewer than seeded rows: {state}")
        for line in lines[seeded:]:
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise RuntimeError(f"state row is not an object: {state}")
            candidate = Candidate.model_validate(payload, strict=False)
            rows.append(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(
            json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n"
            for candidate in rows
        ),
        encoding="utf-8",
    )
    if rows:
        print(f"exported {len(rows)} rows to {output}")
    else:
        print("no post-seed state rows to export")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the history seed or export command."""
    parser = argparse.ArgumentParser(prog="python -m pipeline.history")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--history-dir", type=Path, required=True)
    seed_parser.add_argument("--out", type=Path, required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--state", type=Path, required=True)
    export_parser.add_argument("--seed-meta", type=Path, required=True)
    export_parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "seed":
        seed(args.history_dir, args.out)
        return 0
    return export_rows(args.state, args.seed_meta, args.out)


if __name__ == "__main__":  # pragma: no cover - module entrypoint
    raise SystemExit(main())
