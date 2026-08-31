"""Observation seams that let the orchestrator gather its own evidence.

Verification (§10) never trusts a session's account of its own results, so the
orchestrator observes outcomes here: it runs tests and re-checks symbols in a
detached worktree of the target checkout, and reads the fork's alerts for a
pull-request ref itself.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import PipelineConfig
from pipeline.lanes.codeql import enumerate_codeql_candidates
from pipeline.lanes.deprecations import reference_surface
from pipeline.schemas import Candidate, ItemOutcome, PerItemOutcome, ReasonCode
from pipeline.verify import (
    AlertObservation,
    ItemRunResult,
    SkipMarkerObservation,
    SuiteResult,
    SymbolObservation,
)

CommandRunner = Callable[[Sequence[str], Path], tuple[int, str]]
AlertReader = Callable[[str], object]
SkipNodeidResolver = Callable[[str, int], str]

_OUTCOME_PREFIXES = {
    "PASSED": ItemOutcome.PASSED,
    "FAILED": ItemOutcome.FAILED,
    "SKIPPED": ItemOutcome.SKIPPED,
    "ERROR": ItemOutcome.ERROR,
}


def is_test_path(path: str) -> bool:
    """Return whether a changed path names a pytest target."""
    return (
        path.startswith("tests/")
        or "/tests/" in path
        or path.startswith("test_")
        or path.endswith("_test.py")
    )


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


def parse_item_outcomes(
    output: str,
    *,
    resolve_skip_nodeid: SkipNodeidResolver | None = None,
) -> tuple[PerItemOutcome, ...]:
    """Parse pytest's short summary lines into per-item outcomes."""
    outcomes: list[PerItemOutcome] = []
    for line in output.splitlines():
        head, _, rest = line.strip().partition(" ")
        outcome = _OUTCOME_PREFIXES.get(head)
        if outcome is None or not rest:
            continue
        skip_match = re.match(r"\[\d+\]\s+(.+?):(\d+):\s*(.*)$", rest)
        if skip_match is not None:
            path = skip_match.group(1)
            line_number = int(skip_match.group(2))
            nodeid = (
                resolve_skip_nodeid(path, line_number) if resolve_skip_nodeid is not None else path
            )
            detail = skip_match.group(3)
        else:
            nodeid, _, detail = rest.partition(" - ")
        exception_type, _, message = detail.partition(": ")
        if outcome is ItemOutcome.FAILED and detail.strip().startswith("assert "):
            exception_type = "AssertionError"
            message = detail
        outcomes.append(
            PerItemOutcome(
                nodeid=nodeid.strip(),
                outcome=outcome,
                exception_type=exception_type.strip() or None,
                message=message.strip() or detail.strip() or None,
            )
        )
    return tuple(outcomes)


def _skip_nodeid(worktree: Path, path: str, line_number: int) -> str:
    """Resolve pytest's skipped ``path:line`` summary to a collected nodeid."""
    source_path = worktree / path
    if not source_path.exists():
        return path
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return path
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    candidates: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", node.lineno)
        start = min((decorator.lineno for decorator in node.decorator_list), default=node.lineno)
        if start <= line_number <= end:
            names = [node.name]
            parent = parents.get(node)
            while parent is not None:
                if isinstance(parent, ast.ClassDef):
                    names.append(parent.name)
                parent = parents.get(parent)
            candidates.append((start, "::".join(reversed(names))))
    if not candidates:
        return path
    _, name = min(candidates, key=lambda item: item[0])
    return f"{path}::{name}"


def _qualified_node(
    source: str,
    qualname: str,
) -> tuple[ast.AST, dict[ast.AST, ast.AST], ast.Module]:
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
            return node, parents, tree
    raise LookupError(f"qualname does not resolve: {qualname}")


def _qualname_resolves(source: str, qualname: str) -> bool:
    try:
        _qualified_node(source, qualname)
    except LookupError:
        return False
    return True


@dataclass
class LocalCheckout:
    """A target checkout the orchestrator can observe at arbitrary revisions."""

    repo_path: Path
    worktree_root: Path
    runner: CommandRunner = run_command
    pytest_command: tuple[str, ...] = (sys.executable, "-m", "pytest", "-q", "--tb=no", "-rA")

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
        try:
            code, output = self.runner(command, worktree)
        except (FileNotFoundError, OSError):
            return ItemRunResult(
                outcomes=(),
                command=" ".join(command),
                reason=ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        if code in {3, 4, 5} or "No module named pytest" in output:
            return ItemRunResult(
                outcomes=parse_item_outcomes(output),
                command=" ".join(command),
                reason=ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        outcomes = parse_item_outcomes(output)
        if not outcomes and code == 0 and re.search(r"\d+\s+passed", output):
            outcomes = (PerItemOutcome(nodeid=nodeid, outcome=ItemOutcome.PASSED),)
        return ItemRunResult(outcomes=outcomes, command=" ".join(command))

    def run_item_with_test_diff(
        self,
        base_sha: str,
        head_sha: str,
        nodeid: str,
        test_paths: Sequence[str],
    ) -> ItemRunResult:
        """Run an item at base after applying only the candidate test-path diff."""
        target = self.worktree_root / f"{base_sha}-tests-from-{head_sha}"
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            code, output = self.runner(
                (
                    "git",
                    "-C",
                    str(self.repo_path),
                    "worktree",
                    "add",
                    "--detach",
                    str(target),
                    base_sha,
                ),
                self.repo_path,
            )
            if code != 0:
                return ItemRunResult(
                    (),
                    f"git worktree add {target} {base_sha}: {output.strip()}",
                    ReasonCode.CAPABILITY_UNAVAILABLE,
                )
            code, output = self.runner(
                ("git", "-C", str(self.repo_path), "diff", "--name-only", base_sha, head_sha),
                self.repo_path,
            )
            if code != 0:
                return ItemRunResult(
                    (),
                    f"git diff --name-only {base_sha} {head_sha}: {output.strip()}",
                    ReasonCode.CAPABILITY_UNAVAILABLE,
                )
            paths = tuple(path for path in output.splitlines() if is_test_path(path))
            if paths:
                code, output = self.runner(
                    ("git", "-C", str(target), "checkout", head_sha, "--", *paths),
                    target,
                )
                if code != 0:
                    return ItemRunResult(
                        (),
                        f"git checkout {head_sha} -- {' '.join(paths)}: {output.strip()}",
                        ReasonCode.CAPABILITY_UNAVAILABLE,
                    )
        command = (*self.pytest_command, nodeid)
        try:
            code, output = self.runner(command, target)
        except (FileNotFoundError, OSError):
            return ItemRunResult((), " ".join(command), ReasonCode.CAPABILITY_UNAVAILABLE)
        if code in {3, 4, 5} or "No module named pytest" in output:
            return ItemRunResult(
                parse_item_outcomes(output), " ".join(command), ReasonCode.CAPABILITY_UNAVAILABLE
            )
        outcomes = parse_item_outcomes(output)
        if not outcomes and code == 0 and re.search(r"\d+\s+passed", output):
            outcomes = (PerItemOutcome(nodeid=nodeid, outcome=ItemOutcome.PASSED),)
        return ItemRunResult(outcomes, " ".join(command))

    def run_suite(self, scope: Sequence[str], sha: str) -> SuiteResult:
        """Run the suite covering ``scope`` at ``sha``."""
        worktree = self.revision(sha)
        targets = tuple(path for path in scope if is_test_path(path))
        command = (*self.pytest_command, *targets)
        try:
            code, output = self.runner(command, worktree)
        except (FileNotFoundError, OSError):
            return SuiteResult(
                passed=False,
                command=" ".join(command),
                reason=ReasonCode.CAPABILITY_UNAVAILABLE,
            )
        parsed = parse_item_outcomes(
            output,
            resolve_skip_nodeid=lambda path, line: _skip_nodeid(worktree, path, line),
        )
        failing = tuple(
            outcome.nodeid
            for outcome in parsed
            if outcome.outcome in (ItemOutcome.FAILED, ItemOutcome.ERROR)
        )
        capability = (
            code in {3, 4, 5}
            or bool(re.search(r"(?:collected\s+0\s+items|no tests ran)", output, re.IGNORECASE))
            or "No module named pytest" in output
            or "ERROR collecting" in output
        )
        return SuiteResult(
            passed=code == 0 and not capability,
            command=" ".join(command),
            failing_nodeids=failing,
            reason=ReasonCode.CAPABILITY_UNAVAILABLE if capability else None,
        )

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

    def probe_skip_marker(self, candidate: Candidate, sha: str) -> SkipMarkerObservation:
        """Inspect a test source for skip decorators and in-body ``pytest.skip`` calls."""
        nodeid = candidate.nodeid
        command = f"ast skip-marker probe of {nodeid or 'unknown nodeid'} at {sha}"
        if nodeid is None:
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail="candidate has no test nodeid",
            )
        path, separator, qualname = nodeid.partition("::")
        if not separator or not qualname:
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=f"nodeid does not contain a qualified test name: {nodeid}",
            )
        try:
            worktree = self.revision(sha)
        except (OSError, subprocess.SubprocessError) as exc:
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=f"could not materialize revision {sha}: {exc}",
            )
        source_path = worktree / path
        if not source_path.exists():
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=f"test source file is missing: {path}",
            )
        try:
            source = source_path.read_text(encoding="utf-8")
            target, parents, _ = _qualified_node(source, qualname.replace("::", "."))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=f"could not parse test source {path}: {exc}",
            )
        except LookupError as exc:
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=str(exc),
            )
        if not isinstance(target, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return SkipMarkerObservation(
                present=False,
                command=command,
                available=False,
                detail=f"qualified test target is not a function: {qualname}",
            )

        markers: list[str] = []

        def add_decorator_markers(node: ast.AST, owner: str) -> None:
            decorators = getattr(node, "decorator_list", ())
            for decorator in decorators:
                name = ""
                if isinstance(decorator, ast.Name):
                    name = decorator.id
                elif isinstance(decorator, ast.Attribute):
                    name = decorator.attr
                elif isinstance(decorator, ast.Call):
                    function = decorator.func
                    if isinstance(function, ast.Name):
                        name = function.id
                    elif isinstance(function, ast.Attribute):
                        name = function.attr
                if name in {"skip", "skipif"}:
                    markers.append(f"{owner} decorator: {ast.unparse(decorator)}")

        add_decorator_markers(target, "function")
        parent = parents.get(target)
        while parent is not None:
            if isinstance(parent, ast.ClassDef):
                add_decorator_markers(parent, f"class {parent.name}")
            parent = parents.get(parent)

        for node in ast.walk(target):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if (
                node.func.attr == "skip"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "pytest"
            ):
                markers.append(f"in-body call: {ast.unparse(node)}")
        return SkipMarkerObservation(
            present=bool(markers),
            command=command,
            markers=tuple(markers),
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
