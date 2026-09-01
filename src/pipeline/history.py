"""Seed and export append-only state across hosted remediation runs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pipeline.observability.results import state_path
from pipeline.schemas import REMOVED_LEGACY_CANDIDATE_KEYS, Candidate


def _strip_legacy_keys(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip removed fields so historical rows can be validated by live models."""
    return {
        key: value for key, value in payload.items() if key not in REMOVED_LEGACY_CANDIDATE_KEYS
    }


def _write_candidate(handle: Any, candidate: Candidate) -> None:  # noqa: ANN401
    """Write one state row using the state store's canonical serialization."""
    handle.write(json.dumps(candidate.model_dump(mode="json"), sort_keys=True) + "\n")


def seed(history_dir: Path, output: Path) -> list[str]:
    """Seed a state file from every historical run in chronological directory order."""
    if output.exists() and output.stat().st_size > 0:
        raise RuntimeError(f"refusing to seed non-empty state file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_dirs = (
        sorted(path for path in history_dir.iterdir() if path.is_dir())
        if history_dir.exists()
        else []
    )
    # Hosted run directories use UTC timestamp prefixes, so lexicographic order is chronological.
    run_ids: set[str] = set()
    with output.open("w", encoding="utf-8") as handle:
        for run_dir in run_dirs:
            for line in state_path(run_dir).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise RuntimeError(f"state row is not an object: {run_dir}")
                candidate = Candidate.model_validate(_strip_legacy_keys(payload), strict=False)
                run_ids.add(candidate.run_id or "")
                _write_candidate(handle, candidate)
    seeded = sorted(run_id for run_id in run_ids if run_id)
    output.with_name(output.name + ".seed.json").write_text(
        json.dumps({"run_ids": seeded}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"seeded {len(seeded)} run ids into {output}")
    return seeded


def _seeded_run_ids(path: Path) -> set[str]:
    """Read seed metadata, treating absent metadata as an empty seed."""
    if not path.exists():
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("run_ids"), list):
        raise RuntimeError(f"invalid seed metadata: {path}")
    return {value for value in payload["run_ids"] if isinstance(value, str)}


def export_rows(state: Path, seed_meta: Path, output: Path) -> int:
    """Export only rows appended after the seeded run IDs."""
    seeded = _seeded_run_ids(seed_meta)
    rows: list[Candidate] = []
    if state.exists():
        for line in state.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise RuntimeError(f"state row is not an object: {state}")
            candidate = Candidate.model_validate(payload, strict=False)
            if candidate.run_id not in seeded:
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
