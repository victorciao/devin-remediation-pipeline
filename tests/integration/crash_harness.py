"""§13/§14.1 crash windows — §17.1 requires recovery after a real process kill.

The harness drives the real LIVE entrypoint with a durable fake GitHub server.  An in-process
``TransportInterrupted`` exception is insufficient here: it does not prove that the append-only
state and the remote write survive an operating-system process death.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
from collections.abc import Mapping
from pathlib import Path

from pipeline import __main__ as entrypoint
from pipeline.config import CiEvidenceMode, Mode, PipelineConfig
from pipeline.lanes.codeql import read_alert_fixture
from pipeline.schemas import Lane
from tests.conftest import FIXTURES_DIR, RUBRICS_PATH, TEMPLATES_DIR
from tests.fakes import HEAD_SHA, FakeGitHubTransport

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_MUTATION_KEYS = ("method", "path", "payload")


def _ledger_rows(path: Path) -> list[dict[str, object]]:
    """Read the durable server-side mutation ledger."""
    if not path.exists():
        return []
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _append_ledger(path: Path, method: str, endpoint: str, payload: Mapping[str, object]) -> None:
    """Flush one server mutation durably before the fake returns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = dict(zip(LEDGER_MUTATION_KEYS, (method, endpoint, payload), strict=True))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _mutation_number(rows: list[dict[str, object]], index: int) -> int:
    """Match FakeGitHubTransport's monotonically allocated mutation number."""
    return sum(1 for row in rows[:index] if row.get("method") in {"post", "patch", "put"}) + 1


def _fixture_alert() -> Mapping[str, object]:
    """Select one high-scoring alert from the shipped fixture."""
    value = read_alert_fixture(FIXTURES_DIR / "codeql_alerts.json")
    if isinstance(value, list):
        for alert in value:
            if not isinstance(alert, Mapping):
                continue
            rule = alert.get("rule")
            if (
                isinstance(rule, Mapping)
                and rule.get("id") == "py/overly-large-range"
                and alert.get("number") == 6
            ):
                return alert
    raise RuntimeError("the CodeQL fixture does not contain alert py/overly-large-range number 6")


def _branch_created(rows: list[dict[str, object]], prefix: str, branch: str) -> bool:
    """Return whether the fake server has accepted the requested branch."""
    expected_path = f"{prefix}/git/refs"
    expected_ref = f"refs/heads/{branch}"
    for row in rows:
        payload = row.get("payload")
        if (
            row.get("method") == "post"
            and row.get("path") == expected_path
            and isinstance(payload, Mapping)
            and payload.get("ref") == expected_ref
        ):
            return True
    return False


class DurableLedgerGitHubTransport(FakeGitHubTransport):
    """A FakeGitHubTransport whose writes survive a killed process."""

    def __init__(self, ledger: Path, *, kill_point: str, base_sha: str) -> None:
        rows = _ledger_rows(ledger)
        super().__init__(
            base_sha=base_sha,
            head_sha=HEAD_SHA,
            code_scanning_alerts=[_fixture_alert()],
            code_scanning_analyses=[
                {
                    "commit_sha": base_sha,
                    "ref": "refs/heads/master",
                    "category": "/language:python",
                }
            ],
            completed_workflow_runs=True,
            next_number=sum(1 for row in rows if row.get("method") in {"post", "patch", "put"}) + 1,
        )
        self.ledger = ledger
        self.kill_point = kill_point

    def _kill(self) -> None:
        """Terminate this real OS process with the required signal."""
        os.kill(os.getpid(), signal.SIGKILL)

    def _record(self, method: str, path: str, payload: Mapping[str, object]) -> None:
        """Persist a mutation before delegating to the ordinary fake."""
        if path.endswith("/pulls") and self.kill_point == "before_pr":
            self._kill()
        _append_ledger(self.ledger, method, path, payload)
        if path.endswith("/pulls") and self.kill_point == "after_pr":
            self._kill()

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        self._record("post", path, payload)
        return super().post(path, payload)

    def patch(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        self._record("patch", path, payload)
        return super().patch(path, payload)

    def put(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """The base fake has no PUT operation to delegate to."""
        self._record("put", path, payload)
        return {"ok": True}

    def get(self, path: str) -> object:
        """Read issue and PR artifacts from the durable ledger, not a fixture."""
        rows = _ledger_rows(self.ledger)
        prefix = self._prefix()
        if path.startswith("/search/issues"):
            items: list[dict[str, object]] = []
            for index, row in enumerate(rows):
                if row.get("method") != "post" or row.get("path") != f"{prefix}/issues":
                    continue
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                body = payload.get("body")
                if not isinstance(body, str):
                    continue
                number = _mutation_number(rows, index)
                items.append(
                    {
                        "number": number,
                        "html_url": f"https://github.test/{self.owner}/{self.repo}/issues/{number}",
                        "body": body,
                    }
                )
            return {"total_count": len(items), "items": items}
        if path.startswith(f"{prefix}/pulls?"):
            pulls: list[dict[str, object]] = []
            for index, row in enumerate(rows):
                if row.get("method") != "post" or row.get("path") != f"{prefix}/pulls":
                    continue
                payload = row.get("payload")
                if not isinstance(payload, Mapping):
                    continue
                number = _mutation_number(rows, index)
                pulls.append(
                    {
                        "number": number,
                        "html_url": f"https://github.test/{self.owner}/{self.repo}/pull/{number}",
                        "state": "open",
                        "merged_at": None,
                        "head": {"sha": HEAD_SHA, "ref": payload.get("head")},
                    }
                )
            return pulls
        if path.startswith(f"{prefix}/git/ref/heads/"):
            branch = path.split(f"{prefix}/git/ref/heads/", 1)[1]
            if branch != "master":
                if _branch_created(rows, prefix, branch):
                    return {"object": {"sha": HEAD_SHA}}
            return super().get(path)
        if path.startswith(f"{prefix}/pulls/"):
            number = int(path.rsplit("/", 1)[-1])
            return {
                "number": number,
                "html_url": f"https://github.test/{self.owner}/{self.repo}/pull/{number}",
                "state": "open",
                "merged_at": None,
                "head": {
                    "sha": HEAD_SHA,
                    "repo": {"full_name": f"{self.owner}/{self.repo}"},
                },
            }
        if "/code-scanning/analyses?ref=refs/pull/" in path:
            return [{"commit_sha": HEAD_SHA, "ref": path.split("?ref=", 1)[-1]}]
        if "/code-scanning/alerts?ref=refs/pull/" in path:
            return []
        if "/check-runs" in path:
            return {
                "check_runs": [
                    {"name": "pre-commit (current)", "conclusion": "success"},
                ]
            }
        return super().get(path)


class SuccessfulDevinTransport:
    """A normal successful Devin session that returns a valid fix response."""

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        del payload
        if path != "/v1/sessions":
            raise AssertionError(f"unexpected Devin POST: {path}")
        return {"session_id": "crash-harness-session", "is_new_session": True}

    def get(self, path: str) -> Mapping[str, object]:
        if path != "/v1/sessions/crash-harness-session":
            raise AssertionError(f"unexpected Devin GET: {path}")
        return {
            "status_enum": "finished",
            "structured_output": {
                "files_changed": [
                    "superset/mcp_service/dashboard/tool/add_chart_to_existing_dashboard.py"
                ],
                "test_nodeid": None,
                "test_paths": [],
                "verify_command": "pytest superset/mcp_service/dashboard/tool",
                "head_sha": HEAD_SHA,
                "suite_scope": ["superset/mcp_service/dashboard/tool"],
                "fix_summary": "Crash recovery harness fix.",
                "testing_notes": "Harness verification.",
                "criterion_notes": "Harness verification.",
                "feasible": True,
                "infeasible_reason": None,
            },
        }


def _target_checkout(state_dir: Path) -> Path:
    """Create the smallest valid target checkout for one isolated LIVE run."""
    target = state_dir / "target-checkout"
    if not (target / ".git").exists():
        target.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(target)], check=True)
        subprocess.run(
            ["git", "-C", str(target), "config", "user.name", "crash-harness"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(target), "config", "user.email", "crash-harness@example.test"],
            check=True,
        )
        (target / "README.md").write_text("crash harness target\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
        subprocess.run(
            ["git", "-C", str(target), "commit", "-qm", "chore: create crash harness target"],
            check=True,
        )
    return target


def _base_sha(target: Path) -> str:
    """Use the isolated target checkout revision for the LIVE preflight."""
    completed = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def run_harness(*, kill_point: str, state_dir: Path, ledger: Path) -> int:
    """Run one real LIVE entrypoint leg with the selected crash window."""
    target = _target_checkout(state_dir)
    base_sha = _base_sha(target)
    github = DurableLedgerGitHubTransport(ledger, kill_point=kill_point, base_sha=base_sha)
    devin = SuccessfulDevinTransport()
    setattr(entrypoint, "UrllibGitHubTransport", lambda: github)  # noqa: B010
    setattr(entrypoint, "UrllibDevinTransport", lambda: devin)  # noqa: B010
    config = PipelineConfig(
        mode=Mode.LIVE,
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        alert_fixture_path=FIXTURES_DIR / "codeql_alerts.json",
        only_lanes=(Lane.CODEQL,),
        budget_N=1,
        max_sessions=1,
        ci_wait_timeout_s=1,
        alert_analysis_wait_s=1,
        ci_evidence_mode=CiEvidenceMode.ACTIONS,
    )
    entrypoint.run_once(
        config=config,
        repo_path=target,
        output_dir=state_dir,
        baseline_path=FIXTURES_DIR / "baseline.json",
        base_sha=base_sha,
        head_branch="devin/crash-harness",
        base_branch="master",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse harness arguments and execute one process leg."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--kill-point", choices=("before_pr", "after_pr", "none"), required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    return run_harness(
        kill_point=args.kill_point,
        state_dir=args.state_dir,
        ledger=args.ledger,
    )


if __name__ == "__main__":
    raise SystemExit(main())
