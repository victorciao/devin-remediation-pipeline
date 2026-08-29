"""AST-based skipped-test candidate enumeration."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Callable, Iterator
from pathlib import Path

from pipeline.schemas import Candidate, DefinitionKind, Lane

Record = dict[str, str | int | None]
LiveCountProvider = Callable[[str], int | None]
Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

SKIP_DECORATORS = ("pytest.mark.skip", "unittest.skip", "unittest.case.skip")
CONDITIONAL_SKIP_MARKERS = ("skipif", "skipUnless", "xfail")
XFAIL_MARKERS = ("xfail",)


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return f"{_decorator_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """Map each locally bound import name to its dotted target."""
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bindings


def _resolve(name: str, bindings: dict[str, str]) -> str:
    """Rewrite a decorator name through module-level import bindings."""
    if not name:
        return name
    head, _, rest = name.partition(".")
    target = bindings.get(head)
    if target is None:
        return name
    return f"{target}.{rest}" if rest else target


def _decorator_reason(node: ast.expr) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    for keyword in node.keywords:
        if keyword.arg == "reason" and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    for argument in node.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
    return None


def _is_unconditional_skip(name: str) -> bool:
    """Return whether a resolved decorator disables a test outright."""
    if any(marker in name for marker in CONDITIONAL_SKIP_MARKERS):
        return False
    return any(name == marker or name.endswith(f".{marker}") for marker in SKIP_DECORATORS)


def _exclusion_reason(name: str) -> str | None:
    """Return the audit reason for an excluded conditional marker."""
    if any(marker in name for marker in XFAIL_MARKERS):
        return "expected_failure_xfail"
    if any(marker in name for marker in CONDITIONAL_SKIP_MARKERS):
        return "conditional_environment_guard"
    return None


def _iter_definitions(
    body: list[ast.stmt],
    scope: tuple[str, ...] = (),
) -> Iterator[tuple[Definition, tuple[str, ...]]]:
    """Yield definitions with their enclosing class scope."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node, scope
            yield from _iter_definitions(node.body, (*scope, node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, scope


def _enclosed_tests(node: Definition) -> int:
    """Count direct-body test methods for a class-level skip."""
    if not isinstance(node, ast.ClassDef):
        return 0
    return sum(
        1
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name.startswith("test")
    )


def _is_parametrized(node: Definition, bindings: dict[str, str]) -> bool:
    return any(
        _resolve(_decorator_name(decorator), bindings).endswith("mark.parametrize")
        for decorator in node.decorator_list
    )


def _candidate_id(repo: str, nodeid: str) -> str:
    return hashlib.sha256(f"{Lane.SKIPPED_TESTS.value}|{repo}|{nodeid}".encode()).hexdigest()


def _site_record(
    path: Path,
    repo: Path,
    node: Definition,
    scope: tuple[str, ...],
    decorator: ast.expr,
    resolved: str,
) -> Record:
    return {
        "path": str(path.relative_to(repo)),
        "line": node.lineno,
        "decorator_line": decorator.lineno,
        "symbol": node.name,
        "decorator": _decorator_name(decorator),
        "resolved_decorator": resolved,
        "reason": _decorator_reason(decorator),
        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
        "class_scope": "::".join(scope) or None,
    }


def enumerate_skipped_tests(
    repo: Path,
    *,
    repo_name: str = "victorciao/superset",
    live_count_provider: LiveCountProvider | None = None,
    failures: list[Record] | None = None,
) -> tuple[list[Candidate], list[Record]]:
    """Enumerate unconditional skips and return conditional exclusions separately."""
    candidates: list[Candidate] = []
    excluded: list[Record] = []
    skipped_scopes: dict[tuple[str, ...], str] = {}
    for path in sorted((repo / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            if failures is not None:
                failures.append(
                    {
                        "path": str(path.relative_to(repo)),
                        "reason": "collection_error",
                    }
                )
            continue
        bindings = _import_bindings(tree)
        skipped_scopes.clear()
        for node, scope in _iter_definitions(tree.body):
            for decorator in node.decorator_list:
                written = _decorator_name(decorator)
                resolved = _resolve(written, bindings)
                record = _site_record(path, repo, node, scope, decorator, resolved)
                if _is_unconditional_skip(resolved):
                    nodeid = "::".join((str(record["path"]), *scope, node.name))
                    enclosed = _enclosed_tests(node)
                    parameterized = _is_parametrized(node, bindings)
                    enclosing = next(
                        (
                            skipped_scopes[scope[:index]]
                            for index in range(len(scope), 0, -1)
                            if scope[:index] in skipped_scopes
                        ),
                        None,
                    )
                    live_count = (
                        live_count_provider(nodeid) if live_count_provider is not None else None
                    )
                    candidates.append(
                        Candidate(
                            candidate_id=_candidate_id(repo_name, nodeid),
                            lane=Lane.SKIPPED_TESTS,
                            repo=repo_name,
                            stable_locator=nodeid,
                            trigger_exists=True,
                            verifiability_exists=True,
                            nodeid=nodeid,
                            class_scope="::".join(scope) or None,
                            kind=(
                                DefinitionKind.CLASS
                                if isinstance(node, ast.ClassDef)
                                else DefinitionKind.FUNCTION
                            ),
                            enclosed_tests=enclosed,
                            live_enclosed_tests=live_count,
                            parametrized=parameterized,
                            collects_single_item=enclosed == 0 and not parameterized,
                            enclosing_skip_nodeid=enclosing,
                            skip_reason=_decorator_reason(decorator),
                            decorator=written,
                            resolved_decorator=resolved,
                            line=node.lineno,
                            decorator_line=decorator.lineno,
                        )
                    )
                    record.update(
                        {
                            "nodeid": nodeid,
                            "enclosed_tests": enclosed,
                            "parametrized": int(parameterized),
                            "collects_single_item": int(enclosed == 0 and not parameterized),
                            "enclosing_skip_nodeid": enclosing,
                        }
                    )
                    if isinstance(node, ast.ClassDef):
                        skipped_scopes[(*scope, node.name)] = nodeid
                elif (reason := _exclusion_reason(resolved)) is not None:
                    record["excluded_reason"] = reason
                    excluded.append(record)
    return candidates, excluded


def collect_skipped_tests(repo: Path) -> tuple[list[Record], list[Record]]:
    """Return baseline-compatible records from the canonical enumerator."""
    candidates, excluded = enumerate_skipped_tests(repo)
    included: list[Record] = []
    for candidate in candidates:
        included.append(
            {
                "path": candidate.nodeid.split("::", 1)[0] if candidate.nodeid else None,
                "line": candidate.line,
                "decorator_line": candidate.decorator_line,
                "symbol": (
                    candidate.nodeid.rsplit("::", 1)[-1] if candidate.nodeid is not None else None
                ),
                "decorator": candidate.decorator,
                "resolved_decorator": candidate.resolved_decorator,
                "reason": candidate.skip_reason,
                "kind": candidate.kind.value if candidate.kind is not None else None,
                "class_scope": candidate.class_scope,
                "nodeid": candidate.nodeid,
                "enclosed_tests": candidate.enclosed_tests,
                "parametrized": int(candidate.parametrized is True),
                "collects_single_item": int(candidate.collects_single_item is True),
                "enclosing_skip_nodeid": candidate.enclosing_skip_nodeid,
            }
        )
    return included, excluded


__all__ = ["collect_skipped_tests", "enumerate_skipped_tests"]
