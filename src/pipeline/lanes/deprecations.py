"""AST-based EOL deprecation candidate enumeration."""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from pipeline.config import ConfigError
from pipeline.schemas import Candidate, Lane, ReasonCode

Record = dict[str, str | int | bool | None]
VERSION_SOURCE = ".github/ISSUE_TEMPLATE/bug-report.yml"


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Attribute):
        return f"{_decorator_name(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _keyword_string(node: ast.expr, name: str) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    for keyword in node.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return str(keyword.value.value)
    return None


def _version_tuple(value: str) -> tuple[int, ...] | None:
    parts = value.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def is_eol(
    deprecated_in: str,
    current_major: int,
    *,
    removed_in: str | None = None,
    current_release: str | None = None,
    eol_major_lag: int = 2,
) -> bool:
    """Apply the §4.2 removed-version or major-lag EOL rule."""
    if removed_in is not None and current_release is not None:
        removed = _version_tuple(removed_in)
        current = _version_tuple(current_release)
        return removed is not None and current is not None and removed <= current
    deprecated = _version_tuple(deprecated_in)
    return (
        deprecated is not None
        and bool(deprecated)
        and deprecated[0] <= current_major - eol_major_lag
    )


def current_release(
    repo: Path,
    version_source: str = VERSION_SOURCE,
) -> tuple[str, int]:
    """Resolve the highest concrete release in the version dropdown.

    Raises when the source drifts and contains no concrete release, rather than
    silently producing an empty deprecation lane.
    """
    lines = (repo / version_source).read_text(encoding="utf-8").splitlines()
    releases: list[tuple[int, int, int]] = []
    in_block = False
    for line in lines:
        if "id: superset-version" in line:
            in_block = True
            continue
        if in_block:
            match = re.match(r'^\s*-\s*["\']?(\d+)\.(\d+)\.(\d+)["\']?\s*$', line)
            if match:
                major, minor, patch = (int(group) for group in match.groups())
                releases.append((major, minor, patch))
            elif line.strip().startswith("- type:"):
                break
    if not releases:
        raise ConfigError(f"no concrete release found in {version_source}")
    top = max(releases)
    return ".".join(str(part) for part in top), top[0]


def _qualname(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> str:
    parts: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(parts))


def _decorated_functions(
    tree: ast.Module,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.expr, Mapping[ast.AST, ast.AST]]]:
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    sites: list[
        tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.expr, Mapping[ast.AST, ast.AST]]
    ] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if _decorator_name(decorator) == "deprecated":
                sites.append((node, decorator, parents))
    return sites


def _references(repo: Path, symbol: str, defining_path: Path, defining_line: int) -> int:
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    count = 0
    for path in sorted((repo / "superset").rglob("*.py")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            if path == defining_path and line_number == defining_line:
                continue
            count += len(pattern.findall(line))
    return count


def _override_references(repo: Path, symbol: str) -> int:
    paths = (repo / "superset/db_engine_specs/lib.py", repo / "superset/db_engine_specs/README.md")
    return sum(
        len(re.findall(rf"\b{re.escape(symbol)}\b", path.read_text(encoding="utf-8")))
        for path in paths
        if path.exists()
    )


def enumerate_deprecations(
    repo: Path,
    *,
    current_release_value: str | None = None,
    current_major: int | None = None,
    version_source: str = VERSION_SOURCE,
    eol_major_lag: int = 2,
) -> list[Candidate]:
    """Enumerate deprecated symbols and annotate EOL and caller observables."""
    release = current_release_value
    major = current_major
    if release is None or major is None:
        release, major = current_release(repo, version_source)
    candidates: list[Candidate] = []
    for path in sorted((repo / "superset").rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node, decorator, parents in _decorated_functions(tree):
            deprecated_in = _keyword_string(decorator, "deprecated_in")
            if deprecated_in is None:
                continue
            removed_in = _keyword_string(decorator, "removed_in")
            qualname = _qualname(node, parents)
            module = str(path.relative_to(repo)).removesuffix(".py").replace("/", ".")
            public = any(_decorator_name(item).endswith("expose") for item in node.decorator_list)
            callers = _references(repo, node.name, path, node.lineno)
            overrides = _override_references(repo, node.name)
            eol = is_eol(
                deprecated_in,
                major,
                removed_in=removed_in,
                current_release=release,
                eol_major_lag=eol_major_lag,
            )
            locator = f"{module}:{qualname}"
            candidates.append(
                Candidate(
                    candidate_id=hashlib.sha256(
                        f"{Lane.DEPRECATIONS.value}|victorciao/superset|{locator}".encode()
                    ).hexdigest(),
                    lane=Lane.DEPRECATIONS,
                    repo="victorciao/superset",
                    stable_locator=locator,
                    trigger_exists=True,
                    verifiability_exists=True,
                    module=module,
                    qualname=qualname,
                    deprecated_in=deprecated_in,
                    removed_in=removed_in,
                    current_major=major,
                    caller_count=callers,
                    override_count=overrides,
                    targeted_test_signal="targeted",
                    transformation_scope="isolated_removal",
                    public_api_surface=public,
                    internal_caller=callers > 0,
                    override_surface=overrides > 0,
                    line=node.lineno,
                    decorator_line=decorator.lineno,
                    reason=None if eol else ReasonCode.NOT_EOL,
                )
            )
    return candidates


def collect_deprecations(
    repo: Path,
    *,
    current_major: int | None = None,
    current_release_value: str | None = None,
    eol_major_lag: int = 2,
) -> list[Record]:
    """Return baseline-compatible deprecation records."""
    candidates = enumerate_deprecations(
        repo,
        current_major=current_major,
        current_release_value=current_release_value,
        eol_major_lag=eol_major_lag,
    )
    return [
        {
            "path": (
                f"{candidate.module.replace('.', '/')}.py" if candidate.module is not None else None
            ),
            "line": candidate.line,
            "decorator_line": candidate.decorator_line,
            "symbol": (
                candidate.qualname.rsplit(".", 1)[-1] if candidate.qualname is not None else None
            ),
            "qualname": candidate.qualname,
            "locator": candidate.stable_locator,
            "deprecated_in": candidate.deprecated_in,
            "removed_in": candidate.removed_in,
        }
        for candidate in candidates
    ]


__all__ = ["collect_deprecations", "current_release", "enumerate_deprecations", "is_eol"]
