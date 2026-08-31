"""Observation seams that let the orchestrator gather its own evidence.

Verification (§10) never trusts a session's account of its own results, so the
orchestrator observes outcomes here: it runs tests and re-checks symbols in a
detached worktree of the target checkout, and reads the fork's alerts for a
pull-request ref itself.
"""

from __future__ import annotations

import ast
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.lanes.codeql import enumerate_codeql_candidates
from pipeline.lanes.deprecations import reference_surface
from pipeline.schemas import Candidate, ItemOutcome, PerItemOutcome
from pipeline.verify import AlertObservation, ItemRunResult, SuiteResult, SymbolObservation

CommandRunner = Callable[[Sequence[str], Path], tuple[int, str]]
AlertReader = Callable[[str], object]

_OUTCOME_PREFIXES = {
    "PASSED": ItemOutcome.PASSED,
    "FAILED": ItemOutcome.FAILED,
    "SKIPPED": ItemOutcome.SKIPPED,
    "ERROR": ItemOutcome.ERROR,
}


def run_command(command: Sequence[str], cwd: Path) -> tuple[int, str]:
    """Run a command in ``cwd`` and return its exit code and combined output."""
    completed = subprocess.run(  # noqa: S603 - fixed argument vectors, no shell
        list(command),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, completed.stdout + completed.stderr


def parse_item_outcomes(output: str) -> tuple[PerItemOutcome, ...]:
    """Parse pytest's short summary lines into per-item outcomes."""
    outcomes: list[PerItemOutcome] = []
    for line in output.splitlines():
        head, _, rest = line.strip().partition(" ")
        outcome = _OUTCOME_PREFIXES.get(head)
        if outcome is None or not rest:
            continue
        nodeid, _, detail = rest.partition(" - ")
        exception_type, _, message = detail.partition(": ")
        outcomes.append(
            PerItemOutcome(
                nodeid=nodeid.strip(),
                outcome=outcome,
                exception_type=exception_type.strip() or None,
                message=message.strip() or detail.strip() or None,
            )
        )
    return tuple(outcomes)


def _qualname_resolves(source: str, qualname: str) -> bool:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    wanted = qualname.split(".")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        names: list[str] = []
        current: ast.AST | None = node
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(current.name)
            current = parents.get(current)
        if list(reversed(names)) == wanted:
            return True
    return False


@dataclass
class LocalCheckout:
    """A target checkout the orchestrator can observe at arbitrary revisions."""

    repo_path: Path
    worktree_root: Path
    runner: CommandRunner = run_command
    pytest_command: tuple[str, ...] = ("python", "-m", "pytest", "-q", "--tb=no", "-rA")

    def revision(self, sha: str) -> Path:
        """Materialize ``sha`` as a detached worktree and return its path."""
        target = self.worktree_root / sha
        if target.exists():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        self.runner(
            ("git", "-C", str(self.repo_path), "worktree", "add", "--detach", str(target), sha),
            self.repo_path,
        )
        return target

    def run_item(self, sha: str, nodeid: str) -> ItemRunResult:
        """Run one nodeid at ``sha`` and report the per-item outcomes observed."""
        worktree = self.revision(sha)
        command = (*self.pytest_command, nodeid)
        _, output = self.runner(command, worktree)
        return ItemRunResult(outcomes=parse_item_outcomes(output), command=" ".join(command))

    def run_suite(self, scope: Sequence[str], sha: str) -> SuiteResult:
        """Run the suite covering ``scope`` at ``sha``."""
        worktree = self.revision(sha)
        command = (*self.pytest_command, *scope)
        code, output = self.runner(command, worktree)
        failing = tuple(
            outcome.nodeid
            for outcome in parse_item_outcomes(output)
            if outcome.outcome in (ItemOutcome.FAILED, ItemOutcome.ERROR)
        )
        return SuiteResult(passed=code == 0, command=" ".join(command), failing_nodeids=failing)

    def probe_symbol(self, candidate: Candidate, sha: str) -> SymbolObservation:
        """Re-check a LANE 3 symbol and its reference surface at ``sha``."""
        worktree = self.revision(sha)
        module = candidate.module or ""
        qualname = candidate.qualname or ""
        module_path = worktree / (module.replace(".", "/") + ".py")
        resolves = False
        if module_path.exists():
            try:
                resolves = _qualname_resolves(module_path.read_text(encoding="utf-8"), qualname)
            except (OSError, SyntaxError, UnicodeDecodeError):
                resolves = False
        symbol = qualname.rsplit(".", 1)[-1]
        callers, overrides = reference_surface(worktree, symbol, module_path, candidate.line or 1)
        return SymbolObservation(
            resolves=resolves,
            caller_count=callers,
            override_count=overrides,
            command=f"ast re-check of {module}:{qualname} at {sha}",
        )


@dataclass
class PullRequestAlerts:
    """Reads the fork's CodeQL alerts for a pull-request ref (§10, LANE 1)."""

    config: PipelineConfig
    reader: AlertReader
    pr_number: int
    repo_path: Path | None = None
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_interval_s: float = 60.0

    def _ref(self) -> str:
        return f"refs/pull/{self.pr_number}/head"

    def _endpoint(self, resource: str) -> str:
        owner = self.config.target_owner
        repo = self.config.target_repo
        return f"/repos/{owner}/{repo}/code-scanning/{resource}?ref={self._ref()}"

    def _analysed(self, head_sha: str) -> bool:
        payload = self.reader(self._endpoint("analyses"))
        if not isinstance(payload, list):
            return False
        return any(
            isinstance(entry, dict) and entry.get("commit_sha") == head_sha for entry in payload
        )

    def probe(self, candidate: Candidate, head_sha: str) -> AlertObservation:
        """Return the alert locators observed once the head has been analysed."""
        endpoint = self._endpoint("alerts")
        command = f"GET {endpoint}"
        deadline = self.clock() + self.config.alert_analysis_wait_s
        while not self._analysed(head_sha):
            if self.clock() >= deadline:
                return AlertObservation(
                    locators=(),
                    command=command,
                    available=False,
                    detail=(
                        f"no analysis of {head_sha} on {self._ref()} within "
                        f"alert_analysis_wait_s={self.config.alert_analysis_wait_s}"
                    ),
                )
            self.sleep(self.poll_interval_s)
        try:
            alerts = enumerate_codeql_candidates(
                self.reader(endpoint),
                candidate.repo,
                repo_path=self.repo_path,
                base_sha=head_sha,
            )
        except ValueError as exc:
            return AlertObservation(
                locators=(),
                command=command,
                available=False,
                detail=f"alerts for the pull-request ref were unreadable: {exc}",
            )
        return AlertObservation(
            locators=tuple(alert.stable_locator for alert in alerts),
            command=command,
        )


__all__ = [
    "AlertReader",
    "CommandRunner",
    "LocalCheckout",
    "PullRequestAlerts",
    "parse_item_outcomes",
    "run_command",
]
