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
from datetime import datetime, timezone
from pathlib import Path

#: LANE 2 scope (plan §5): unconditional skips only. ``xfail`` is a different signal (an
#: expected failure, not disabled coverage) and every ``skipif``/``skipUnless`` site in the
#: target repo is a correct-by-design environment guard, so both are excluded.
SKIP_DECORATORS = ("pytest.mark.skip", "unittest.skip")

#: Conditional-skip decorators, counted separately so the exclusion is auditable rather
#: than invisible.
CONDITIONAL_SKIP_MARKERS = ("skipif", "skipUnless", "xfail")

#: LANE 3 (plan §4.2): the repo declares no static version (``pyproject.toml`` uses
#: ``dynamic = ["version"]`` and ``package.json`` says ``0.0.0-dev``), so the current major
#: is derived from the highest concrete release offered by the bug-report issue form.
VERSION_SOURCE = ".github/ISSUE_TEMPLATE/bug-report.yml"
EOL_MAJOR_LAG = 2


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
    return any(name == s or name.endswith(s) for s in SKIP_DECORATORS)


def collect_skipped_tests(repo: Path) -> tuple[list[dict], list[dict]]:
    """Enumerate LANE 2 candidates and the conditional sites deliberately excluded."""
    included: list[dict] = []
    excluded: list[dict] = []
    for path in sorted((repo / "tests").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for dec in node.decorator_list:
                name = _decorator_name(dec)
                record = {
                    "path": str(path.relative_to(repo)),
                    "line": node.lineno,
                    "symbol": node.name,
                    "decorator": name,
                    "reason": _decorator_reason(dec),
                    "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                }
                if _is_unconditional_skip(name):
                    record["nodeid"] = f"{record['path']}::{node.name}"
                    included.append(record)
                elif any(marker in name for marker in CONDITIONAL_SKIP_MARKERS):
                    record["excluded_reason"] = "conditional_environment_guard"
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


def collect_deprecations(repo: Path) -> list[dict]:
    """Enumerate ``@deprecated(deprecated_in=...)`` sites with their qualified names."""
    out: list[dict] = []
    for path in sorted((repo / "superset").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                version = _deprecated_in(dec)
                if version is None:
                    continue
                owner = parents.get(node)
                qualname = (
                    f"{owner.name}.{node.name}" if isinstance(owner, ast.ClassDef) else node.name
                )
                module = str(path.relative_to(repo)).removesuffix(".py").replace("/", ".")
                out.append(
                    {
                        "path": str(path.relative_to(repo)),
                        "line": dec.lineno,
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
                releases.append(tuple(int(g) for g in match.groups()))  # type: ignore[arg-type]
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
            1
            for d in deprecations
            if d["deprecated_in"].split(".")[0].isdigit()
            and int(d["deprecated_in"].split(".")[0]) <= major - EOL_MAJOR_LAG
        ),
        "codeql_open_alerts": len(codeql["open"]),
    }

    path = out_dir / "baseline.json"
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}: {baseline['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
