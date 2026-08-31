"""§13/§14.1 crash windows — §17.1 requires real process-kill recovery.

These scenarios launch the runnable harness and send SIGKILL immediately before and after PR
creation.  Existing in-process ``TransportInterrupted`` tests were insufficient because they do
not prove that OS-process death preserves the append-only state and the accepted remote write.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = "tests.integration.crash_harness"


def run_leg(kill_point: str, state_dir: Path, ledger: Path) -> subprocess.CompletedProcess[str]:
    """Launch one real harness process."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            HARNESS,
            "--kill-point",
            kill_point,
            "--state-dir",
            str(state_dir),
            "--ledger",
            str(ledger),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def ledger_rows(path: Path) -> list[dict[str, Any]]:
    """Read server mutations left by both process legs."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def state_rows(state_dir: Path) -> list[dict[str, Any]]:
    """Read the append-only LIVE state file."""
    path = state_dir / "state" / "candidates-live.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def latest_state(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the latest row for the sole candidate."""
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        latest[str(row["candidate_id"])] = row
    assert len(latest) == 1
    return next(iter(latest.values()))


def assert_crash_before_pr(tmp_path: Path) -> None:
    """A pre-write kill leaves no PR, then resume creates exactly one."""
    state_dir = tmp_path / "before-state"
    ledger = tmp_path / "before-ledger.jsonl"
    first = run_leg("before_pr", state_dir, ledger)
    assert first.returncode == -signal.SIGKILL
    assert first.returncode == -9
    first_rows = ledger_rows(ledger)
    assert sum(row["path"].endswith("/pulls") for row in first_rows) == 0
    first_state = latest_state(state_rows(state_dir))
    assert isinstance(first_state["issue_url"], str)
    assert first_state["pr_url"] is None
    assert first_state["pr_number"] is None

    resumed = run_leg("none", state_dir, ledger)
    assert resumed.returncode == 0, resumed.stderr
    rows = ledger_rows(ledger)
    assert sum(row["path"].endswith("/pulls") for row in rows) == 1
    assert sum(row["path"].endswith("/issues") for row in rows) == 1
    final = latest_state(state_rows(state_dir))
    assert isinstance(final["pr_url"], str)


def assert_crash_after_pr(tmp_path: Path) -> None:
    """A post-write kill leaves one durable PR, then resume adopts it."""
    state_dir = tmp_path / "after-state"
    ledger = tmp_path / "after-ledger.jsonl"
    first = run_leg("after_pr", state_dir, ledger)
    assert first.returncode == -signal.SIGKILL
    assert first.returncode == -9
    first_rows = ledger_rows(ledger)
    assert sum(row["path"].endswith("/pulls") for row in first_rows) == 1
    first_state = latest_state(state_rows(state_dir))
    assert first_state["pr_url"] is None
    assert first_state["pr_number"] is None

    resumed = run_leg("none", state_dir, ledger)
    assert resumed.returncode == 0, resumed.stderr
    rows = ledger_rows(ledger)
    assert sum(row["path"].endswith("/pulls") for row in rows) == 1
    assert sum(row["path"].endswith("/issues") for row in rows) == 1
    pr_write = next(row for row in rows if row["path"].endswith("/pulls"))
    pr_number = sum(
        1 for row in rows[: rows.index(pr_write) + 1] if row["method"] in {"post", "patch", "put"}
    )
    final = latest_state(state_rows(state_dir))
    assert final["pr_number"] == pr_number
    assert final["pr_url"] == f"https://github.test/victorciao/superset/pull/{pr_number}"


def test_crash_before_pr_write_recovers_without_duplicate_issue(tmp_path: Path) -> None:
    """§17.1 — the first crash window resumes publication exactly once."""
    assert_crash_before_pr(tmp_path)


def test_crash_after_pr_write_adopts_without_duplicate_issue(tmp_path: Path) -> None:
    """§17.1 — the second crash window adopts the server-side PR exactly once."""
    assert_crash_after_pr(tmp_path)
