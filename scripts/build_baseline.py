#!/usr/bin/env python3
"""Phase 0c: capture a BASELINE snapshot of REPO B and derive the SIMULATE fixture.

Read-only. Walks a local Superset checkout and records the lane-relevant inventory exactly
as the LANE 2 / LANE 3 enumerators define it (plan §5, §4.2), plus the CodeQL alert set read
from a previously captured alert fixture.

Usage:
    build_baseline.py <superset-checkout> <out-dir> [codeql-alerts.json]
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

#: LANE 2 scope (plan §5): unconditional skips only. Decorator names are resolved against
#: the module's import bindings first, so an alias import (``from unittest import skip``
#: then ``@skip("Flaky")``) is enumerated like a fully qualified one.
SKIP_DECORATORS = ("pytest.mark.skip", "unittest.skip", "unittest.case.skip")

#: Conditional-skip decorators, counted separately so the exclusion is auditable rather
#: than invisible. ``xfail`` is a different signal again — an expected failure that is still
#: collected — so it carries its own exclusion reason.
CONDITIONAL_SKIP_MARKERS = ("skipif", "skipUnless", "xfail")
XFAIL_MARKERS = ("xfail",)

#: LANE 3 (plan §4.2): the repo declares no static version (``pyproject.toml`` uses
#: ``dynamic = ["version"]`` and ``package.json`` says ``0.0.0-dev``), so the current major
#: is derived from the highest concrete release offered by the bug-report issue form.
VERSION_SOURCE = ".github/ISSUE_TEMPLATE/bug-report.yml"
EOL_MAJOR_LAG = 2

#: One enumerated site: string/int fields only, so the record serializes straight to JSON.
Record = dict[str, str | int | None]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return f"{_decorator_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _import_bindings(tree: ast.Module) -> dict[str, str]:
    """Map each locally bound name to the dotted path it refers to."""
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
    """Rewrite a decorator's dotted name through the module's import bindings."""
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
    for kw in node.keywords:
        if kw.arg == "reason" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _is_unconditional_skip(name: str) -> bool:
    """Return True for a decorator that disables a test outright."""
    if any(marker in name for marker in CONDITIONAL_SKIP_MARKERS):
        return False
    return any(name == s or name.endswith(f".{s}") for s in SKIP_DECORATORS)


def _exclusion_reason(name: str) -> str | None:
    """Classify a conditional decorator into its auditable exclusion reason."""
    if any(marker in name for marker in XFAIL_MARKERS):
        return "expected_failure_xfail"
    if any(marker in name for marker in CONDITIONAL_SKIP_MARKERS):
        return "conditional_environment_guard"
    return None


Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _iter_definitions(
    body: list[ast.stmt], scope: tuple[str, ...] = ()
) -> Iterator[tuple[Definition, tuple[str, ...]]]:
    """Yield each definition with the class scope enclosing it, outermost first."""
    for node in body:
        if isinstance(node, ast.ClassDef):
            yield node, scope
            yield from _iter_definitions(node.body, (*scope, node.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node, scope


def _enclosed_tests(node: Definition) -> int:
    """Count the test methods a class-level skip disables (0 for a function)."""
    if not isinstance(node, ast.ClassDef):
        return 0
    return sum(
        1
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        and child.name.startswith("test")
    )


def _is_parametrized(node: Definition, bindings: dict[str, str]) -> bool:
    """Return True when the node's own nodeid expands to one item per parameter set."""
    return any(
        _resolve(_decorator_name(dec), bindings).endswith("mark.parametrize")
        for dec in node.decorator_list
    )


def collect_skipped_tests(repo: Path) -> tuple[list[Record], list[Record]]:
    """Enumerate LANE 2 candidates and the conditional sites deliberately excluded."""
    included: list[Record] = []
    excluded: list[Record] = []
    for path in sorted((repo / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        bindings = _import_bindings(tree)
        skipped_scopes: dict[tuple[str, ...], str] = {}
        for node, scope in _iter_definitions(tree.body):
            for dec in node.decorator_list:
                written = _decorator_name(dec)
                name = _resolve(written, bindings)
                record: Record = {
                    "path": str(path.relative_to(repo)),
                    "line": node.lineno,
                    "decorator_line": dec.lineno,
                    "symbol": node.name,
                    "decorator": written,
                    "resolved_decorator": name,
                    "reason": _decorator_reason(dec),
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                }
                record["class_scope"] = "::".join(scope) or None
                if _is_unconditional_skip(name):
                    nodeid = "::".join((str(record["path"]), *scope, node.name))
                    enclosed = _enclosed_tests(node)
                    record["nodeid"] = nodeid
                    record["enclosed_tests"] = enclosed
                    record["parametrized"] = int(_is_parametrized(node, bindings))
                    record["collects_single_item"] = int(
                        enclosed == 0 and not _is_parametrized(node, bindings)
                    )
                    record["enclosing_skip_nodeid"] = next(
                        (
                            skipped_scopes[scope[:i]]
                            for i in range(len(scope), 0, -1)
                            if scope[:i] in skipped_scopes
                        ),
                        None,
                    )
                    if isinstance(node, ast.ClassDef):
                        skipped_scopes[(*scope, node.name)] = nodeid
                    included.append(record)
                elif (reason := _exclusion_reason(name)) is not None:
                    record["excluded_reason"] = reason
                    excluded.append(record)
    return included, excluded


def _deprecated_in(dec: ast.expr) -> str | None:
    if not isinstance(dec, ast.Call) or _decorator_name(dec) != "deprecated":
        return None
    for kw in dec.keywords:
        if kw.arg == "deprecated_in" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _removed_in(dec: ast.expr) -> str | None:
    if not isinstance(dec, ast.Call):
        return None
    for kw in dec.keywords:
        if kw.arg == "removed_in" and isinstance(kw.value, ast.Constant):
            return str(kw.value.value)
    return None


def _eol_passed(deprecated_in: str, current_major: int) -> bool:
    """Return True when a deprecation's major is at least EOL_MAJOR_LAG behind current."""
    head = deprecated_in.split(".")[0]
    return head.isdigit() and int(head) <= current_major - EOL_MAJOR_LAG


def collect_deprecations(repo: Path) -> list[Record]:
    """Enumerate ``@deprecated(deprecated_in=...)`` sites with their qualified names."""
    out: list[Record] = []
    for path in sorted((repo / "superset").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                version = _deprecated_in(dec)
                if version is None:
                    continue
                owner = parents.get(node)
                qualname = (
                    f"{owner.name}.{node.name}"
                    if isinstance(owner, ast.ClassDef)
                    else node.name
                )
                module = (
                    str(path.relative_to(repo)).removesuffix(".py").replace("/", ".")
                )
                out.append(
                    {
                        "path": str(path.relative_to(repo)),
                        "line": node.lineno,
                        "decorator_line": dec.lineno,
                        "symbol": node.name,
                        "qualname": qualname,
                        "locator": f"{module}:{qualname}",
                        "deprecated_in": version,
                        "removed_in": _removed_in(dec),
                    }
                )
    return out


RELEASE_RE = re.compile(r'^\s*-\s*"(\d+)\.(\d+)\.(\d+)"\s*$')


def current_release(repo: Path) -> tuple[str, int]:
    """Resolve the target's current release from the bug-report issue form.

    Returns the release string and its major component. The form's ``Superset version``
    dropdown is the only in-repo list of concrete released versions.
    """
    lines = (repo / VERSION_SOURCE).read_text(encoding="utf-8").splitlines()
    releases: list[tuple[int, int, int]] = []
    in_block = False
    for line in lines:
        if "id: superset-version" in line:
            in_block = True
            continue
        if in_block:
            match = RELEASE_RE.match(line)
            if match:
                major, minor, patch = (int(g) for g in match.groups())
                releases.append((major, minor, patch))
            elif line.strip().startswith("- type:"):
                break
    if not releases:
        raise RuntimeError(f"no concrete release found in {VERSION_SOURCE}")
    top = max(releases)
    return ".".join(str(part) for part in top), top[0]


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    alerts_path = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None

    release, major = current_release(repo)
    skipped, excluded = collect_skipped_tests(repo)
    deprecations = collect_deprecations(repo)

    if alerts_path and alerts_path.exists():
        alerts = json.loads(alerts_path.read_text(encoding="utf-8"))
        codeql = {
            "open": alerts,
            "accessible": True,
            "source": str(alerts_path.name),
        }
    else:
        codeql = {
            "open": [],
            "accessible": False,
            "note": "code scanning was capability_unavailable when this snapshot was taken",
        }

    baseline = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repo": "victorciao/superset",
        "head_sha": _git(repo, "rev-parse", "HEAD"),
        "default_branch": "master",
        "current_release": release,
        "current_major": major,
        "eol_major_lag": EOL_MAJOR_LAG,
        "eol_threshold_major": major - EOL_MAJOR_LAG,
        "version_source": VERSION_SOURCE,
        "codeql_alerts": codeql,
        "skipped_tests": skipped,
        "excluded_conditional_skips": excluded,
        "deprecations": deprecations,
    }
    baseline["baseline_valid_lanes"] = [
        lane
        for lane, ok in (
            ("codeql", codeql["accessible"]),
            ("skipped_tests", True),
            ("deprecations", True),
        )
        if ok
    ]
    baseline["totals"] = {
        "skipped_tests": len(skipped),
        "excluded_conditional_skips": len(excluded),
        "deprecations": len(deprecations),
        "eol_passed_deprecations": sum(
            1 for d in deprecations if _eol_passed(str(d["deprecated_in"]), major)
        ),
        "codeql_open_alerts": len(codeql["open"]),
        "multi_item_skipped_tests": sum(
            1 for s in skipped if not s["collects_single_item"]
        ),
        "nested_under_skipped_class": sum(
            1 for s in skipped if s["enclosing_skip_nodeid"]
        ),
    }

    path = out_dir / "baseline.json"
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}: {baseline['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
