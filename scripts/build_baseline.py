#!/usr/bin/env python3
"""Phase 0c: capture a BASELINE snapshot of REPO B and derive the SIMULATE fixture.

Read-only. Walks a local Superset checkout and records the raw, lane-relevant inventory:
skipped tests, EOL-passed deprecations, and the CodeQL alert set (empty on this fork).
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SKIP_DECORATORS = ("pytest.mark.skip", "unittest.skip", "pytest.mark.xfail")


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


def collect_skipped_tests(repo: Path) -> list[dict]:
    out: list[dict] = []
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
                if not any(name.endswith(s) or name == s for s in SKIP_DECORATORS):
                    continue
                # skipUnless/skipIf are environment-conditional, not backlog debt.
                if "Unless" in name or "If" in name:
                    continue
                out.append(
                    {
                        "path": str(path.relative_to(repo)),
                        "line": node.lineno,
                        "symbol": node.name,
                        "decorator": name,
                        "reason": _decorator_reason(dec),
                        "kind": "class" if isinstance(node, ast.ClassDef) else "function",
                    }
                )
    return out


DEPRECATED_RE = re.compile(r"@deprecated\(\s*deprecated_in\s*=\s*[\"']([^\"']+)[\"']")


def collect_deprecations(repo: Path) -> list[dict]:
    out: list[dict] = []
    for path in sorted((repo / "superset").rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for idx, line in enumerate(lines, start=1):
            match = DEPRECATED_RE.search(line)
            if not match:
                continue
            symbol = None
            for follow in lines[idx : idx + 6]:
                sig = re.match(r"\s*(?:async\s+)?def\s+(\w+)", follow)
                if sig:
                    symbol = sig.group(1)
                    break
            out.append(
                {
                    "path": str(path.relative_to(repo)),
                    "line": idx,
                    "symbol": symbol,
                    "deprecated_in": match.group(1),
                }
            )
    return out


def current_major(repo: Path) -> str:
    text = (repo / "superset-frontend" / "package.json").read_text(encoding="utf-8")
    return json.loads(text).get("version", "unknown")


def main() -> int:
    repo = Path(sys.argv[1]).resolve()
    out_dir = Path(sys.argv[2]).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    baseline = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "repo": "victorciao/superset",
        "head_sha": _git(repo, "rev-parse", "HEAD"),
        "default_branch": "master",
        "declared_version": current_major(repo),
        "codeql_alerts": {
            "open": [],
            "accessible": False,
            "note": (
                "GET /repos/victorciao/superset/code-scanning/alerts -> 403 "
                "(installation token lacks security_events); the fork has 0 Actions runs, "
                "so codeql-analysis.yml has never executed here."
            ),
        },
        "skipped_tests": collect_skipped_tests(repo),
        "deprecations": collect_deprecations(repo),
    }
    baseline["totals"] = {
        "skipped_tests": len(baseline["skipped_tests"]),
        "deprecations": len(baseline["deprecations"]),
        "codeql_open_alerts": 0,
    }

    path = out_dir / "baseline.json"
    path.write_text(json.dumps(baseline, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}: {baseline['totals']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
