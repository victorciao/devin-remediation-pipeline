"""CodeQL alert normalization and candidate enumeration."""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import cast

from pipeline.config import AlertSource, PipelineConfig
from pipeline.schemas import Candidate, Lane
from pipeline.verify import declare_success_criterion

JsonObject = dict[str, object]
AlertReader = Callable[[str], object]
RegionReader = Callable[[str, int, int], str]
_SECURITY_SEVERITY_ROWS = ("critical", "high", "medium", "low", "note")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("CodeQL alert must be an object")
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _security_severity(raw: str | None) -> str | None:
    """Map a CodeQL severity value onto a business-impact rubric row.

    Returns None when the alert carries no security severity, so scoring falls back
    to the configured default anchor. SARIF reports the CVSS score rather than a band.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if text in _SECURITY_SEVERITY_ROWS:
        return text
    try:
        score = float(text)
    except ValueError:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0.0:
        return "low"
    return "note"


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def candidate_id(repo: str, stable_locator: str) -> str:
    """Return the stable LANE 1 identity for a repository locator."""
    return hashlib.sha256(f"{Lane.CODEQL.value}|{repo}|{stable_locator}".encode()).hexdigest()


def position_digest(location: Mapping[str, object]) -> str:
    """Digest the complete CodeQL source range, including columns."""
    start_line = _integer(location.get("start_line")) or 0
    start_column = _integer(location.get("start_column")) or 0
    end_line = _integer(location.get("end_line")) or start_line
    end_column = _integer(location.get("end_column")) or start_column
    return _digest(f"{start_line}:{start_column}-{end_line}:{end_column}")


def _default_region_reader(
    repo_path: Path | None,
    path: str,
    start_line: int,
    end_line: int,
) -> str:
    if repo_path is None:
        return ""
    source = repo_path / path
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    return "\n".join(lines[max(start_line - 1, 0) : end_line])


def _module_name(path: str) -> str:
    module = path.removesuffix(".py").replace("/", ".")
    return module.removesuffix(".__init__")


def _enclosing_symbol(
    repo_path: Path | None,
    path: str,
    start_line: int,
) -> tuple[str, int, str]:
    """Return ``qualname``, definition line, and derivation source for an alert."""
    if repo_path is None:
        return "<module>", 1, "module_fallback"
    source_path = repo_path / path
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return "<module>", 1, "module_fallback"
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    enclosing: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        end_line = node.end_lineno or node.lineno
        if node.lineno <= start_line <= end_line:
            enclosing.append(node)
    if not enclosing:
        return "<module>", 1, "module_fallback"
    node = max(enclosing, key=lambda item: item.lineno)
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(current)
    return ".".join(reversed(names)), node.lineno, "ast"


def _blast_radius(repo_path: Path | None, path: str) -> str:
    """Bucket importer count, with security/view areas as critical tie-breakers.

    Importers are counted by distinct Python files under ``superset/`` that
    import the touched module directly or by a submodule. Critical security and
    routed-view areas take precedence over importer-count buckets.
    """
    critical = path.startswith(("superset/security/", "superset/views/"))
    if repo_path is None:
        return "critical_surface" if critical else "local_module"
    target = _module_name(path)
    importers = 0
    for candidate in (repo_path / "superset").rglob("*.py"):
        if str(candidate.relative_to(repo_path)) == path:
            continue
        try:
            tree = ast.parse(candidate.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = imported or any(
                    alias.name == target or alias.name.startswith(f"{target}.")
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported = imported or (
                    node.module == target or node.module.startswith(f"{target}.")
                )
        if imported:
            importers += 1
    if critical:
        return "critical_surface"
    if importers <= 1:
        return "local_module"
    if importers <= 5:
        return "bounded_module"
    if importers <= 20:
        return "shared_module"
    return "broad_module"


def _alert_list(payload: object) -> Sequence[object]:
    if isinstance(payload, list):
        return payload
    root = _mapping(payload)
    alerts = root.get("open", root.get("alerts"))
    if isinstance(alerts, list):
        return alerts
    runs = root.get("runs")
    if not isinstance(runs, list):
        raise ValueError("CodeQL payload must contain an alert list or SARIF runs")
    normalized: list[JsonObject] = []
    for raw_run in runs:
        run = _mapping(raw_run)
        results = run.get("results")
        if not isinstance(results, list):
            continue
        rules = _sarif_rules(run)
        for raw_result in results:
            result = _mapping(raw_result)
            normalized.append(_sarif_alert(result, rules))
    return normalized


def _sarif_rules(run: Mapping[str, object]) -> dict[str, JsonObject]:
    raw_tool = run.get("tool")
    tool = _mapping(raw_tool) if raw_tool is not None else {}
    raw_driver = tool.get("driver")
    driver = _mapping(raw_driver) if raw_driver is not None else {}
    raw_rules = driver.get("rules")
    if not isinstance(raw_rules, list):
        return {}
    rules: dict[str, JsonObject] = {}
    for raw_rule in raw_rules:
        rule = _mapping(raw_rule)
        rule_id = _text(rule.get("id"))
        if rule_id is not None:
            rules[rule_id] = dict(rule)
    return rules


def _sarif_alert(
    result: Mapping[str, object],
    rules: Mapping[str, JsonObject],
) -> JsonObject:
    rule_id = _text(result.get("ruleId")) or ""
    rule = dict(rules.get(rule_id, {}))
    raw_properties = rule.get("properties")
    properties = _mapping(raw_properties) if raw_properties is not None else {}
    raw_result_properties = result.get("properties")
    result_properties = _mapping(raw_result_properties) if raw_result_properties is not None else {}
    precision = _text(properties.get("precision")) or _text(result_properties.get("precision"))
    if precision is not None:
        rule["precision"] = precision
    severity = _text(properties.get("security-severity")) or _text(
        result_properties.get("security-severity")
    )
    if severity is not None:
        rule["security_severity_level"] = severity
    rule["id"] = rule_id
    locations = result.get("locations")
    location = _mapping(locations[0]) if isinstance(locations, list) and locations else {}
    physical = _mapping(location.get("physicalLocation"))
    artifact = _mapping(physical.get("artifactLocation"))
    region = _mapping(physical.get("region"))
    uri = _text(artifact.get("uri")) or ""
    if uri.startswith("./"):
        uri = uri[2:]
    api_location: JsonObject = {
        "path": uri,
        "start_line": _integer(region.get("startLine")),
        "start_column": _integer(region.get("startColumn")),
        "end_line": _integer(region.get("endLine")),
        "end_column": _integer(region.get("endColumn")),
    }
    message = _mapping(result.get("message"))
    updated_at = _text(result_properties.get("updated_at"))
    return {
        "rule": rule,
        "most_recent_instance": {
            "location": api_location,
            "message": {"text": _text(message.get("text")) or ""},
        },
        "updated_at": updated_at,
    }


def enumerate_codeql_candidates(
    payload: object,
    repo: str,
    *,
    repo_path: Path | None = None,
    base_sha: str | None = None,
    freshness_cutoff: datetime | None = None,
    region_reader: RegionReader | None = None,
) -> list[Candidate]:
    """Normalize CodeQL alerts into candidates without network or clock access."""
    candidates: list[Candidate] = []
    for raw_alert in _alert_list(payload):
        alert = _mapping(raw_alert)
        rule = _mapping(alert.get("rule"))
        instance = _mapping(alert.get("most_recent_instance"))
        location = _mapping(instance.get("location"))
        path = _text(location.get("path"))
        rule_id = _text(rule.get("id"))
        if path is None or rule_id is None:
            raise ValueError("CodeQL alert lacks rule.id or location.path")
        start_line = _integer(location.get("start_line")) or 0
        end_line = _integer(location.get("end_line")) or start_line
        digest = position_digest(location)
        normalized_symbol, symbol_start_line, symbol_source = _enclosing_symbol(
            repo_path, path, start_line
        )
        stable_locator = "|".join((rule_id, path, normalized_symbol))
        message = _mapping(instance.get("message"))
        region_source = "source_region"
        region = (
            region_reader(path, start_line, end_line)
            if region_reader is not None
            else _default_region_reader(repo_path, path, start_line, end_line)
        )
        if not region:
            region = _text(message.get("text")) or ""
            region_source = "alert_message"
        updated_at_raw = _text(alert.get("updated_at"))
        updated_at = (
            datetime.fromisoformat(updated_at_raw.replace("Z", "+00:00"))
            if updated_at_raw is not None
            else None
        )
        fresh = (
            updated_at >= freshness_cutoff
            if updated_at is not None and freshness_cutoff is not None
            else updated_at is not None
        )
        candidate = Candidate(
            candidate_id=candidate_id(repo, stable_locator),
            lane=Lane.CODEQL,
            repo=repo,
            stable_locator=stable_locator,
            trigger_exists=True,
            rule_id=rule_id,
            file_path=path,
            normalized_symbol=normalized_symbol,
            alert_number=_integer(alert.get("number")),
            security_severity_level=_security_severity(_text(rule.get("security_severity_level"))),
            rule_precision=_text(rule.get("precision")) or "medium",
            blast_radius=_blast_radius(repo_path, path),
            updated_at_fresh=fresh,
            updated_at=updated_at,
            position_digest=digest,
            region_digest=_digest(" ".join(region.split())),
            region_source=region_source,
            symbol_relative_offset=(
                max(start_line - symbol_start_line, 0) if symbol_source == "ast" else 0
            ),
            symbol_source=symbol_source,
            base_sha=base_sha,
            line=start_line,
            success_criterion=declare_success_criterion(Lane.CODEQL),
            suite_scope=[path],
        )
        candidates.append(candidate)
    return _collapse_duplicate_alerts(candidates)


def _alert_order(candidate: Candidate) -> float:
    """Sort key that places an absent alert number after every present one."""
    return candidate.alert_number if candidate.alert_number is not None else float("inf")


def _collapse_duplicate_alerts(candidates: list[Candidate]) -> list[Candidate]:
    """Keep one candidate per identity, recording the alert numbers it now covers.

    Two alerts of the same rule inside the same symbol are one defect, one issue and one
    pull request: the position digest that once separated them is drift metadata, not
    identity.
    """
    grouped: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.candidate_id, []).append(candidate)
    survivors: list[Candidate] = []
    emitted: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in emitted:
            continue
        emitted.add(candidate.candidate_id)
        duplicates = grouped[candidate.candidate_id]
        survivor = min(duplicates, key=_alert_order)
        covered = sorted(
            row.alert_number
            for row in duplicates
            if row is not survivor and row.alert_number is not None
        )
        survivors.append(survivor.model_copy(update={"duplicate_alert_numbers": covered}))
    return survivors


def read_alert_fixture(path: Path) -> object:
    """Read a captured CodeQL JSON or SARIF payload from disk."""
    try:
        return cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read CodeQL fixture: {path}") from exc


def fetch_alerts(
    owner: str,
    repo: str,
    reader: AlertReader,
) -> object:
    """Fetch API data through an injected reader."""
    return reader(f"/repos/{owner}/{repo}/code-scanning/alerts")


def enumerate_from_config(
    config: PipelineConfig,
    *,
    repo_path: Path,
    repo: str | None = None,
    payload: object | None = None,
    api_reader: AlertReader | None = None,
    base_sha: str | None = None,
) -> list[Candidate]:
    """Enumerate from the configured API or captured fixture source."""
    target_repo = repo or f"{config.target_owner}/{config.target_repo}"
    source = payload
    if source is None and config.alert_source is AlertSource.SARIF_FILE:
        source = read_alert_fixture(config.alert_fixture_path)
    if source is None and api_reader is not None:
        source = fetch_alerts(config.target_owner, config.target_repo, api_reader)
    if source is None:
        raise ValueError("CodeQL payload is required in simulate mode")
    return enumerate_codeql_candidates(
        source,
        target_repo,
        repo_path=repo_path,
        base_sha=base_sha,
    )


__all__ = [
    "candidate_id",
    "enumerate_codeql_candidates",
    "enumerate_from_config",
    "fetch_alerts",
    "position_digest",
    "read_alert_fixture",
]
