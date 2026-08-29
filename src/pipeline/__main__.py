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

"""Command-line entrypoint for one complete remediation pipeline run."""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path

from pipeline.config import (
    AlertSource,
    CiEvidenceMode,
    ConfigError,
    Mode,
    PipelineConfig,
    load_config,
)
from pipeline.dispatch import dispatch_candidates
from pipeline.gate import evaluate_gates
from pipeline.github_client import GitHubClient, publish_artifacts, publish_degraded
from pipeline.http_transport import HttpTransportError, UrllibDevinTransport, UrllibGitHubTransport
from pipeline.lanes.codeql import enumerate_from_config, read_alert_fixture
from pipeline.lanes.deprecations import enumerate_deprecations, is_eol
from pipeline.lanes.skipped_tests import enumerate_skipped_tests
from pipeline.preflight import LivePreflight, PreflightError, run_live_preflight
from pipeline.review_loop import apply_review_result
from pipeline.rubric import load_rubrics
from pipeline.schemas import Action, Candidate, CandidateState, DefinitionKind, Lane, ReasonCode
from pipeline.score import apply_score
from pipeline.session_client import (
    DevinTransport,
    RuntimeOrchestrator,
    SessionCeilingError,
    SessionClient,
)
from pipeline.simulation import simulate_run
from pipeline.state import CandidateStateStore, repository_marker_search
from pipeline.templates.render import (
    render_degraded_comment_body,
    render_issue_body,
    render_issue_title,
    render_pr_body,
    validate_issue_body,
    validate_pr_body,
)


class RunAbort(RuntimeError):
    """Raised when a blocking capability or runtime guard aborts a run."""


def _publish_live(
    candidates: Sequence[Candidate],
    *,
    config: PipelineConfig,
    output_dir: Path,
    repo_path: Path,
    planner_outputs: Mapping[str, Mapping[str, object]],
    reviewer_outputs: Mapping[str, Mapping[str, object]],
    head_branch: str,
    base_branch: str,
    transport: UrllibGitHubTransport,
) -> list[Candidate]:
    """Publish live artifacts after review, preserving the mandated write order."""
    client = GitHubClient(
        config,
        transport=transport,
        clock=time.monotonic,
        sleeper=time.sleep,
    )
    templates = {
        Lane.CODEQL: config.templates_dir / "issues/security_tracking.md",
        Lane.SKIPPED_TESTS: config.templates_dir / "issues/bug_report.yml",
        Lane.DEPRECATIONS: config.templates_dir / "issues/sip.md",
    }
    state_store = CandidateStateStore(
        output_dir / "state" / "candidates.jsonl",
        marker_search=repository_marker_search(repo_path) if repo_path.exists() else None,
    )
    published: list[Candidate] = []
    for candidate in candidates:
        if candidate.action not in {Action.OPEN_PR, Action.OPEN_ISSUE}:
            published.append(candidate)
            continue
        if not config.has_issues and candidate.action is Action.OPEN_ISSUE:
            raise RunAbort(
                "issues are disabled; issue-only candidates require issue_sink=pr_comment"
            )
        if not state_store.append_if_new_artifact(
            candidate.model_copy(update={"state": CandidateState.DISPATCHING})
        ):
            raise RunAbort(f"candidate artifact already exists: {candidate.candidate_id}")
        issue_template = templates[candidate.lane].read_text(encoding="utf-8")
        summary = f"Remediation tracking for {candidate.stable_locator}."
        labels = list(candidate.labels)
        if config.ci_evidence_mode is CiEvidenceMode.LOCAL and "needs-human-review" not in labels:
            labels.append("needs-human-review")
        if config.has_issues:
            issue_body = render_issue_body(
                issue_template,
                candidate,
                generated_summary=summary,
            )
        else:
            issue_body = render_degraded_comment_body(
                issue_template,
                candidate,
                generated_summary=summary,
            )
        if config.has_issues:
            validate_issue_body(issue_body, candidate)
        issue_title = render_issue_title(candidate, f"Remediate {candidate.stable_locator}")
        if candidate.action is Action.OPEN_ISSUE:
            _issue_number, issue_url = client.create_issue(issue_title, issue_body, labels)
            published.append(
                candidate.model_copy(
                    update={
                        "state": CandidateState.ISSUE_CREATED,
                        "issue_url": issue_url,
                    }
                )
            )
            continue
        pr_body = render_pr_body(
            (config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
                encoding="utf-8"
            ),
            candidate,
            planner_outputs.get(candidate.candidate_id, {}),
            reviewer_outputs.get(candidate.candidate_id, {}),
            automation_metadata={"mode": "live", "would_write": False},
        )
        validate_pr_body(pr_body)
        pr_title = f"fix: remediate {candidate.stable_locator}"
        if config.has_issues:
            links = publish_artifacts(
                client,
                candidate,
                issue_title=issue_title,
                issue_body=issue_body,
                pr_title=pr_title,
                pr_body=pr_body,
                head=head_branch,
                base=base_branch,
                labels=labels,
            )
        else:
            links = publish_degraded(
                client,
                candidate,
                pr_title=pr_title,
                pr_body=pr_body,
                comment_body=issue_body,
                head=head_branch,
                base=base_branch,
            )
        if links.pr_number is not None and labels:
            client.add_labels(links.pr_number, labels)
        published.append(
            candidate.model_copy(
                update={
                    "state": CandidateState.PR_CREATED,
                    "issue_url": links.issue_url,
                    "pr_url": links.pr_url,
                }
            )
        )
    return published


def _extract_runtime_args(args: Sequence[str]) -> tuple[dict[str, str], list[str]]:
    """Split entrypoint paths from the existing configuration CLI arguments."""
    runtime: dict[str, str] = {}
    config_args: list[str] = []
    index = 0
    names = {"repo-path": "repo_path", "output-dir": "output_dir", "baseline": "baseline"}
    names.update(
        {
            "config": "config",
            "base-sha": "base_sha",
            "head-branch": "head_branch",
            "base-branch": "base_branch",
        }
    )
    while index < len(args):
        token = args[index]
        if token in {"--help", "-h"}:
            raise RunAbort(
                "usage: python -m pipeline [--repo-path PATH] [--output-dir PATH] "
                "[--baseline PATH] [--config PATH] [configuration options]"
            )
        key = token[2:].split("=", 1)[0] if token.startswith("--") else ""
        if key not in names:
            if token == "--simulate":
                config_args.append("--mode=simulate")
            else:
                config_args.append(token)
            index += 1
            continue
        if "=" in token:
            raw_value = token.split("=", 1)[1]
        else:
            index += 1
            if index >= len(args):
                raise ConfigError(f"missing value for --{key}")
            raw_value = args[index]
        runtime[names[key]] = raw_value
        index += 1
    return runtime, config_args


def _record_candidate(
    record: Mapping[str, object],
    *,
    lane: Lane,
    repo: str,
    locator: str,
    current_major: int | None = None,
) -> Candidate:
    """Build a candidate from a baseline record when the target checkout is absent."""
    candidate = Candidate(
        candidate_id=f"{lane.value}-{uuid.uuid5(uuid.NAMESPACE_URL, locator).hex}",
        lane=lane,
        repo=repo,
        stable_locator=locator,
        trigger_exists=True,
        verifiability_exists=True,
    )
    if lane is Lane.SKIPPED_TESTS:
        nodeid = str(record.get("nodeid", locator))
        return candidate.model_copy(
            update={
                "candidate_id": f"{lane.value}-{uuid.uuid5(uuid.NAMESPACE_URL, nodeid).hex}",
                "stable_locator": nodeid,
                "nodeid": nodeid,
                "kind": (
                    DefinitionKind.CLASS
                    if record.get("kind") == "class"
                    else DefinitionKind.FUNCTION
                ),
                "enclosed_tests": record["enclosed_tests"]
                if isinstance(record.get("enclosed_tests"), int)
                else 0,
                "parametrized": bool(record.get("parametrized", False)),
                "collects_single_item": bool(record.get("collects_single_item", True)),
                "enclosing_skip_nodeid": (
                    str(record["enclosing_skip_nodeid"])
                    if record.get("enclosing_skip_nodeid")
                    else None
                ),
                "skip_reason": (
                    str(record["reason"]) if record.get("reason") is not None else None
                ),
                "scope_is_test_only": True,
                "targeted_test_signal": "targeted",
            }
        )
    locator_value = str(record.get("locator", locator))
    module, _, qualname = locator_value.partition(":")
    deprecated_in = record.get("deprecated_in")
    deprecated_value = str(deprecated_in) if isinstance(deprecated_in, str) else None
    eol = (
        is_eol(
            deprecated_value,
            current_major,
            removed_in=(
                str(record["removed_in"]) if isinstance(record.get("removed_in"), str) else None
            ),
            current_release=None,
        )
        if deprecated_value is not None and current_major is not None
        else False
    )
    return candidate.model_copy(
        update={
            "candidate_id": f"{lane.value}-{uuid.uuid5(uuid.NAMESPACE_URL, locator_value).hex}",
            "stable_locator": locator_value,
            "module": module,
            "qualname": qualname,
            "deprecated_in": deprecated_value,
            "removed_in": (
                str(record["removed_in"]) if record.get("removed_in") is not None else None
            ),
            "targeted_test_signal": "targeted",
            "transformation_scope": "isolated_removal",
            "reason": None if eol else ReasonCode.NOT_EOL,
        }
    )


def _fixture_candidates(
    baseline: Mapping[str, object],
    *,
    lane: Lane,
    repo: str,
    current_major: int | None = None,
) -> list[Candidate]:
    """Materialize baseline records as a deterministic offline fallback."""
    key = "skipped_tests" if lane is Lane.SKIPPED_TESTS else "deprecations"
    records = baseline.get(key)
    if not isinstance(records, list):
        return []
    return [
        _record_candidate(
            item,
            lane=lane,
            repo=repo,
            locator=f"{lane.value}:{index}",
            current_major=current_major,
        )
        for index, item in enumerate(records)
        if isinstance(item, Mapping)
    ]


def _load_baseline(path: Path) -> dict[str, object]:
    """Load the Phase 0c baseline used for capability and KPI semantics."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunAbort(f"cannot read baseline: {path}") from exc
    if not isinstance(value, dict):
        raise RunAbort("baseline must contain an object")
    return value


def _capability_notes(
    baseline: Mapping[str, object],
    *,
    target_exists: bool,
    config: PipelineConfig,
) -> list[str]:
    """Return explicit Phase 0 capability notes for the run report."""
    notes: list[str] = []
    valid = baseline.get("baseline_valid_lanes")
    valid_lanes = {str(item) for item in valid} if isinstance(valid, list) else set()
    for lane in Lane:
        if lane.value not in valid_lanes:
            notes.append(f"{lane.value}: capability_unavailable")
    if not target_exists:
        notes.append("target checkout unavailable; baseline fixture fallback used")
    if config.mode is Mode.SIMULATE:
        notes.append("ci_evidence_mode: local (no live Actions probe in SIMULATE)")
    return notes


def run_once(
    *,
    config: PipelineConfig,
    repo_path: Path,
    output_dir: Path,
    baseline_path: Path,
    base_sha: str | None = None,
    head_branch: str | None = None,
    base_branch: str = "master",
) -> tuple[str, tuple[Path, ...]]:
    """Execute enumeration, selection, review orchestration, and reporting."""
    run_id = uuid.uuid4().hex
    baseline = _load_baseline(baseline_path)
    if config.mode is Mode.SIMULATE and config.ci_evidence_mode is not CiEvidenceMode.LOCAL:
        config = config.model_copy(
            update={"ci_evidence_mode": CiEvidenceMode.LOCAL, "auto_merge_enabled": False}
        )
    target_exists = repo_path.exists()
    preflight_notes: list[str] = []
    preflight: LivePreflight | None = None
    session_transport: DevinTransport | None = None
    github_transport: UrllibGitHubTransport | None = None
    if config.mode is Mode.LIVE:
        if head_branch is None:
            raise RunAbort("LIVE requires --head-branch for PR publication")
        github_transport = UrllibGitHubTransport()
        try:
            preflight = run_live_preflight(config, github_transport)
        except PreflightError as exc:
            raise RunAbort(f"{exc.reason.value}: {exc}") from exc
        config = config.model_copy(
            update={
                "has_issues": preflight.has_issues,
                "ci_evidence_mode": preflight.ci_evidence_mode,
                "auto_merge_enabled": (
                    config.auto_merge_enabled
                    and preflight.ci_evidence_mode is CiEvidenceMode.GITHUB
                ),
            }
        )
        preflight_notes.extend(preflight.notes)
        if not preflight.code_scanning_available:
            config = config.model_copy(update={"alert_source": AlertSource.SARIF_FILE})
        session_transport = UrllibDevinTransport()
    repo_name = f"{config.target_owner}/{config.target_repo}"
    valid = baseline.get("baseline_valid_lanes")
    valid_lanes = {str(item) for item in valid} if isinstance(valid, list) else set()
    baseline_major = baseline.get("current_major")
    current_major = baseline_major if isinstance(baseline_major, int) else None
    candidates: list[Candidate] = []
    if "codeql" in valid_lanes:
        payload: object
        if config.mode is Mode.LIVE and preflight is not None and preflight.code_scanning_available:
            payload = preflight.code_scanning_alerts
        else:
            payload = read_alert_fixture(config.alert_fixture_path)
        candidates.extend(
            enumerate_from_config(
                config,
                repo_path=repo_path if target_exists else Path("/nonexistent"),
                repo=repo_name,
                payload=payload,
                base_sha=base_sha,
            )
        )
    if "skipped_tests" in valid_lanes:
        if target_exists:
            candidates.extend(enumerate_skipped_tests(repo_path, repo_name=repo_name)[0])
        else:
            candidates.extend(
                _fixture_candidates(
                    baseline,
                    lane=Lane.SKIPPED_TESTS,
                    repo=repo_name,
                    current_major=current_major,
                )
            )
    if "deprecations" in valid_lanes:
        if target_exists:
            release = baseline.get("current_release")
            major = baseline.get("current_major")
            candidates.extend(
                enumerate_deprecations(
                    repo_path,
                    current_release_value=release if isinstance(release, str) else None,
                    current_major=major if isinstance(major, int) else None,
                    version_source=config.version_source,
                    eol_major_lag=config.eol_major_lag,
                )
            )
        else:
            candidates.extend(
                _fixture_candidates(
                    baseline,
                    lane=Lane.DEPRECATIONS,
                    repo=repo_name,
                    current_major=current_major,
                )
            )

    rubrics = load_rubrics(config.rubrics_path)
    selected: list[Candidate] = []
    for candidate in candidates:
        gate = evaluate_gates(candidate, config, rubrics=rubrics)
        reason = (
            gate.gate_results[gate.failed_gate].reason if gate.failed_gate is not None else None
        )
        gated = candidate.model_copy(
            update={
                "gate_results": gate.gate_results,
                "gate_passed": gate.gate_passed,
                "failed_gate": gate.failed_gate,
                "reason": reason,
                "state": CandidateState.GATED,
            }
        )
        if gate.gate_passed:
            gated = apply_score(gated, config, rubrics, gate.resolved_factors).model_copy(
                update={"state": CandidateState.SCORED}
            )
        selected.append(gated)
    dispatched = dispatch_candidates(selected, config)
    if (
        config.mode is Mode.LIVE
        and not config.has_issues
        and any(candidate.action is Action.OPEN_ISSUE for candidate in dispatched)
    ):
        raise RunAbort("issues are disabled; issue-only candidates require issue_sink=pr_comment")
    orchestrator = RuntimeOrchestrator(SessionClient(config, transport=session_transport))
    reviewed: list[Candidate] = []
    planner_outputs: dict[str, Mapping[str, object]] = {}
    reviewer_outputs: dict[str, Mapping[str, object]] = {}
    for candidate in dispatched:
        if candidate.action is not Action.OPEN_PR:
            reviewed.append(candidate)
            continue
        try:
            result = orchestrator.run_candidate(
                candidate.candidate_id,
                f"Plan remediation for {candidate.stable_locator}.",
                f"Implement production changes for {candidate.stable_locator}; do not edit tests.",
                f"Author independent regression tests for {candidate.stable_locator}.",
            )
        except SessionCeilingError as exc:
            raise RunAbort(str(exc)) from exc
        planner_output = result.planner.snapshot.payload.get("structured_output")
        reviewer_output = result.reviewer.snapshot.payload.get("structured_output")
        if isinstance(planner_output, Mapping):
            planner_outputs[candidate.candidate_id] = planner_output
        if isinstance(reviewer_output, Mapping):
            reviewer_outputs[candidate.candidate_id] = reviewer_output
        reviewed_candidate = candidate.model_copy(
            update={
                "planner_session_id": result.planner.snapshot.session_id,
                "implementer_session_id": result.implementer.snapshot.session_id,
                "reviewer_session_id": result.reviewer.snapshot.session_id,
                "iterations": result.review.iterations if result.review is not None else 0,
            }
        )
        if result.review is not None:
            reviewed_candidate = apply_review_result(reviewed_candidate, result.review)
        reviewed.append(reviewed_candidate)

    notes = _capability_notes(
        baseline,
        target_exists=target_exists,
        config=config,
    )
    notes.extend(preflight_notes)
    if config.mode is Mode.LIVE:
        if preflight is None:
            raise RunAbort("LIVE preflight did not produce a capability result")
        if github_transport is None or head_branch is None:
            raise RunAbort("LIVE publication transport is unavailable")
        reviewed = _publish_live(
            reviewed,
            config=config,
            output_dir=output_dir,
            repo_path=repo_path,
            planner_outputs=planner_outputs,
            reviewer_outputs=reviewer_outputs,
            head_branch=head_branch,
            base_branch=base_branch,
            transport=github_transport,
        )
    produced = simulate_run(
        reviewed,
        run_id=run_id,
        output_dir=output_dir,
        baseline=baseline,
        config=config,
        planner_outputs=planner_outputs,
        reviewer_outputs=reviewer_outputs,
        capability_notes=notes,
    )
    return run_id, produced


def main(argv: Sequence[str] | None = None) -> int:
    """Run the pipeline CLI and return a shell-compatible exit code."""
    try:
        runtime, config_args = _extract_runtime_args(tuple(argv or sys.argv[1:]))
        config = load_config(
            runtime.get("config"),
            env=None,
            cli_args=config_args,
        )
        repo_path = Path(runtime.get("repo_path", "/home/ubuntu/repos/superset"))
        output_dir = Path(runtime.get("output_dir", "."))
        baseline_path = Path(runtime.get("baseline", "fixtures/baseline.json"))
        run_id, produced = run_once(
            config=config,
            repo_path=repo_path,
            output_dir=output_dir,
            baseline_path=baseline_path,
            base_sha=runtime.get("base_sha"),
            head_branch=runtime.get("head_branch"),
            base_branch=runtime.get("base_branch", "master"),
        )
    except (ConfigError, HttpTransportError, RunAbort, ValueError, OSError) as exc:
        print(f"pipeline run aborted: {exc}", file=sys.stderr)
        return 1
    print(f"run_id={run_id}")
    for path in produced:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
