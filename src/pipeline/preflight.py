# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Read-only LIVE capability checks performed before candidate work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode

from pipeline.config import CiEvidenceMode, IssueSink, PipelineConfig
from pipeline.github_client import GitHubTransport
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import ReasonCode


class PreflightError(RuntimeError):
    """Raised when a blocking LIVE capability precondition is unmet."""

    def __init__(self, reason: ReasonCode, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class LivePreflight:
    """Read-only capability result used to configure a LIVE run."""

    has_issues: bool
    code_scanning_available: bool
    ci_evidence_mode: CiEvidenceMode
    token_login: str
    token_scopes: tuple[str, ...]
    notes: tuple[str, ...]
    code_scanning_alerts: object | None


def _mapping(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise PreflightError(ReasonCode.CAPABILITY_UNAVAILABLE, f"{description} response invalid")
    return {str(key): item for key, item in value.items()}


def _workflow_count(value: object) -> int:
    data = _mapping(value, "workflow listing")
    total = data.get("total_count")
    return total if isinstance(total, int) else 0


def _has_completed_run(value: object) -> bool:
    data = _mapping(value, "workflow history")
    runs = data.get("workflow_runs")
    return isinstance(runs, list) and any(
        isinstance(run, dict)
        and run.get("status") == "completed"
        and isinstance(run.get("event"), str)
        for run in runs
    )


def _path(config: PipelineConfig, suffix: str) -> str:
    return f"/repos/{config.target_owner}/{config.target_repo}{suffix}"


def run_live_preflight(config: PipelineConfig, transport: GitHubTransport) -> LivePreflight:
    """Probe repository capabilities before any candidate work or mutation."""
    try:
        repository = _mapping(transport.get(_path(config, "")), "repository")
    except HttpTransportError as exc:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read target repository capabilities",
        ) from exc

    has_issues = repository.get("has_issues")
    if not isinstance(has_issues, bool):
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "repository has_issues is unavailable",
        )
    if not has_issues and config.issue_sink is not IssueSink.PR_COMMENT:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "target repository has issues disabled; use issue_sink=pr_comment",
        )

    code_scanning_available = True
    code_scanning_alerts: object | None = None
    notes: list[str] = []
    try:
        code_scanning_alerts = transport.get(_path(config, "/code-scanning/alerts"))
    except HttpTransportError as exc:
        if exc.status_code == 403:
            raise PreflightError(
                ReasonCode.TOKEN_CAPABILITY_MISSING,
                "token cannot read code-scanning alerts",
            ) from exc
        if exc.status_code == 404:
            code_scanning_available = False
            notes.append("code_scanning: capability_unavailable")
        else:
            raise PreflightError(
                ReasonCode.CAPABILITY_UNAVAILABLE,
                "cannot read code-scanning alerts",
            ) from exc

    try:
        identity = _mapping(transport.get("/user"), "token identity")
        response_headers = transport.response_headers
        workflows = transport.get(_path(config, "/actions/workflows"))
        pull_request_runs = transport.get(
            _path(
                config,
                "/actions/runs?"
                + urlencode({"event": "pull_request", "status": "completed", "per_page": "1"}),
            )
        )
        dispatch_runs = transport.get(
            _path(
                config,
                "/actions/runs?"
                + urlencode({"event": "workflow_dispatch", "status": "completed", "per_page": "1"}),
            )
        )
    except HttpTransportError as exc:
        raise PreflightError(
            ReasonCode.CAPABILITY_UNAVAILABLE,
            "cannot read Actions capability history",
        ) from exc

    login = identity.get("login")
    if not isinstance(login, str) or not login:
        raise PreflightError(ReasonCode.TOKEN_CAPABILITY_MISSING, "token identity is unavailable")
    scopes = tuple(
        scope.strip()
        for key, value in response_headers.items()
        if key.casefold() == "x-oauth-scopes"
        for scope in value.split(",")
        if scope.strip()
    )
    has_completed_actions = _workflow_count(workflows) > 0 and (
        _has_completed_run(pull_request_runs) or _has_completed_run(dispatch_runs)
    )
    ci_mode = CiEvidenceMode.GITHUB if has_completed_actions else CiEvidenceMode.LOCAL
    if ci_mode is CiEvidenceMode.LOCAL:
        notes.append("ci_evidence_mode: local (no completed pull_request/workflow_dispatch run)")
    else:
        notes.append("ci_evidence_mode: github (completed Actions history observed)")
    notes.append(f"token_identity: {login}")
    notes.append(f"token_scopes: {', '.join(scopes) if scopes else 'none reported'}")
    if not has_issues:
        notes.append("artifact_degraded: issues disabled; PR comments selected")
    return LivePreflight(
        has_issues=has_issues,
        code_scanning_available=code_scanning_available,
        ci_evidence_mode=ci_mode,
        token_login=login,
        token_scopes=scopes,
        notes=tuple(notes),
        code_scanning_alerts=code_scanning_alerts,
    )


__all__ = ["LivePreflight", "PreflightError", "run_live_preflight"]
