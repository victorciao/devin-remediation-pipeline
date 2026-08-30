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
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import urlencode

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
from pipeline.github_client import (
    ArtifactUnavailableError,
    CiModeTransition,
    CiWaitResult,
    ClosedPullRequestError,
    GitHubClient,
    GitHubResponseError,
    LivePreflight,
    MergedPullRequestError,
    PreflightError,
    publish_artifacts,
    publish_degraded,
    run_live_preflight,
    wait_for_required_contexts,
)
from pipeline.http_transport import HttpTransportError, UrllibDevinTransport, UrllibGitHubTransport
from pipeline.lanes.codeql import enumerate_from_config, read_alert_fixture
from pipeline.lanes.deprecations import enumerate_deprecations, is_eol
from pipeline.lanes.skipped_tests import enumerate_skipped_tests
from pipeline.prompts import (
    render_implementer_prompt,
    render_planner_prompt,
    render_reviewer_phase_b_prompt,
    render_reviewer_prompt,
)
from pipeline.review_loop import apply_review_result
from pipeline.rubric import load_rubrics
from pipeline.schemas import (
    Action,
    Candidate,
    CandidateState,
    DefinitionKind,
    Lane,
    ReasonCode,
    RunEventRecord,
)
from pipeline.score import apply_score
from pipeline.session_client import (
    DevinTransport,
    PlannerOutputError,
    RoleCollisionError,
    RuntimeOrchestrator,
    SessionCeilingError,
    SessionClient,
    SessionDedupeError,
)
from pipeline.simulation import simulate_run
from pipeline.state import (
    CandidateStateStore,
    ResumeAction,
    StatePreservationError,
    github_marker_search,
)
from pipeline.templates.render import (
    ArtifactValidationError,
    render_degraded_comment_body,
    render_issue_body,
    render_issue_title,
    render_pr_body,
    render_pr_title,
    validate_issue_body,
    validate_pr_body,
    validate_pr_title,
)


class RunAbort(RuntimeError):
    """Raised when a blocking capability or runtime guard aborts a run."""


_SIGNOFF_TRAILER = re.compile(r"(?im)^Signed-off-by:\s+\S+\s+<[^>\r\n]+>\s*$")


def _commit_message(repo_path: Path, revision: str) -> str | None:
    """Read the candidate branch commit message without exposing command failures."""
    try:
        result = subprocess.run(
            ["git", "show", "-s", "--format=%B", revision],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout


def _publish_live(
    candidates: Sequence[Candidate],
    *,
    config: PipelineConfig,
    output_dir: Path,
    repo_path: Path,
    planner_outputs: Mapping[str, Mapping[str, object]],
    reviewer_outputs: Mapping[str, Mapping[str, object]],
    base_sha: str | None,
    head_branch: str,
    base_branch: str,
    transport: UrllibGitHubTransport,
    state_store: CandidateStateStore,
    ci_mode_events: list[RunEventRecord],
    ci_mode_state: list[CiEvidenceMode],
    ci_elapsed_s: list[float],
    run_id: str,
) -> list[Candidate]:
    """Publish live artifacts after review, preserving the mandated write order."""
    client = GitHubClient(
        config,
        transport=transport,
    )
    templates = {
        Lane.CODEQL: config.templates_dir / "issues/security_tracking.md",
        Lane.SKIPPED_TESTS: config.templates_dir / "issues/bug_report.yml",
        Lane.DEPRECATIONS: config.templates_dir / "issues/sip.md",
    }

    base_response = transport.get(
        f"/repos/{config.target_owner}/{config.target_repo}/git/ref/heads/{base_branch}"
    )
    resolved_base_sha = base_sha
    if isinstance(base_response, Mapping):
        raw_object = base_response.get("object")
        if isinstance(raw_object, Mapping):
            raw_sha = raw_object.get("sha")
            if isinstance(raw_sha, str) and raw_sha:
                resolved_base_sha = raw_sha
    if resolved_base_sha is None:
        raise RunAbort("target base SHA is unavailable")
    published: list[Candidate] = []
    for candidate in candidates:
        if candidate.action not in {Action.OPEN_PR, Action.OPEN_ISSUE}:
            published.append(candidate)
            continue
        if not config.has_issues and candidate.action is Action.OPEN_ISSUE:
            persisted_degraded = state_store.resume(candidate.candidate_id)
            if persisted_degraded is not None and persisted_degraded.state in {
                CandidateState.TERMINAL,
                CandidateState.CONVERGED,
            }:
                published.append(persisted_degraded)
                continue
            issue_body = render_degraded_comment_body(
                templates[candidate.lane].read_text(encoding="utf-8"),
                candidate,
                generated_summary=f"Remediation tracking for {candidate.stable_locator}.",
            )
            report_path = output_dir / "reports" / "issues" / f"{candidate.candidate_id}.md"
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(issue_body, encoding="utf-8")
            degraded = candidate.model_copy(
                update={
                    "state": CandidateState.TERMINAL,
                    "reason": ReasonCode.ARTIFACT_DEGRADED,
                    "artifact_degraded": True,
                }
            )
            state_store.append(degraded)
            published.append(degraded)
            continue
        persisted = state_store.resume(candidate.candidate_id)
        decision = state_store.resume_decision(candidate.candidate_id)
        if decision.action is ResumeAction.SKIP:
            published.append(persisted or candidate)
            continue
        if decision.action is ResumeAction.DEFER:
            deferred = (persisted or candidate).model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                }
            )
            state_store.append(deferred)
            published.append(deferred)
            continue
        if persisted is None:
            reservation = candidate.model_copy(
                update={"state": CandidateState.DISPATCHING, "base_sha": resolved_base_sha}
            )
            if state_store.append_if_new_artifact(reservation):
                persisted = reservation
            else:
                persisted = state_store.resume(candidate.candidate_id)
        if persisted is None:
            deferred = candidate.model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                }
            )
            state_store.append(deferred)
            published.append(deferred)
            continue
        candidate = candidate.model_copy(
            update={
                "base_sha": persisted.base_sha or resolved_base_sha,
                "issue_number": persisted.issue_number,
                "issue_url": persisted.issue_url,
                "comment_url": persisted.comment_url,
                "pr_number": persisted.pr_number,
                "pr_url": persisted.pr_url,
                "head_branch": persisted.head_branch or candidate.head_branch,
                "head_sha": persisted.head_sha,
                "reason": persisted.reason,
                "reason_detail": persisted.reason_detail,
                "artifact_degraded": persisted.artifact_degraded,
                "merge_verified": persisted.merge_verified,
            }
        )
        issue_template = templates[candidate.lane].read_text(encoding="utf-8")
        summary = f"Remediation tracking for {candidate.stable_locator}."
        requested_labels = list(candidate.labels)
        if (
            config.ci_evidence_mode is CiEvidenceMode.LOCAL
            and "needs-human-review" not in requested_labels
        ):
            requested_labels.append("needs-human-review")
        labels: list[str] = []
        label_failures: list[str] = []
        for label in requested_labels:
            if client.ensure_label(label):
                labels.append(label)
            else:
                label_failures.append(label)
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
        issue_number_holder: list[int | None] = [candidate.issue_number]

        def after_issue(
            issue_number: int,
            issue_url: str,
            candidate_for_callback: Candidate = candidate,
            issue_numbers: list[int | None] = issue_number_holder,
        ) -> None:
            issue_numbers[0] = issue_number
            state_store.append(
                candidate_for_callback.model_copy(
                    update={
                        "state": CandidateState.ISSUE_CREATED,
                        "issue_number": issue_number,
                        "issue_url": issue_url,
                        "base_sha": candidate_for_callback.base_sha,
                        "head_branch": candidate_for_callback.head_branch,
                    }
                )
            )

        if candidate.action is Action.OPEN_ISSUE:
            links = publish_artifacts(
                client,
                candidate,
                issue_title=issue_title,
                issue_body=issue_body,
                labels=labels,
                ci_probe=None,
                existing_issue_number=candidate.issue_number,
                existing_issue_url=candidate.issue_url,
                after_issue=after_issue,
            )
            published.append(state_store.resume(candidate.candidate_id) or candidate)
            continue
        branch = candidate.head_branch
        if branch is None:
            raise RunAbort("candidate branch was not prepared before publication")
        pr_body = render_pr_body(
            (config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
                encoding="utf-8"
            ),
            candidate,
            planner_outputs.get(candidate.candidate_id, {}),
            reviewer_outputs.get(candidate.candidate_id, {}),
            automation_metadata={"mode": "live", "would_write": False},
            commit_message=None,
        )
        validate_pr_body(pr_body)
        pr_title = render_pr_title(candidate)
        regex = (config.templates_dir / "superset/pr_title_regex.txt").read_text(encoding="utf-8")
        if not validate_pr_title(pr_title, regex):
            deferred = candidate.model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                }
            )
            state_store.append(deferred)
            published.append(deferred)
            continue
        ci_reason_holder: list[ReasonCode | None] = [None]
        ci_detail_holder: list[str | None] = [None]
        pr_url_holder: list[str | None] = [candidate.pr_url]
        pr_number_holder: list[int | None] = [candidate.pr_number]
        head_sha_holder: list[str | None] = [None]
        commit_message_holder: list[str | None] = [None]

        def record_ci_mode_transition(transition: CiModeTransition) -> None:
            ci_mode_events.append(
                RunEventRecord(
                    event_type="ci_mode_transition",
                    run_id=run_id,
                    mode_from=CiEvidenceMode.LOCAL.value,
                    mode_to=transition.mode.value,
                    transition_reason=transition.reason,
                )
            )

        def after_pr_created(
            pr_number: int,
            pr_url: str,
            candidate_for_callback: Candidate = candidate,
            pr_url_for_callback: list[str | None] = pr_url_holder,
            pr_number_for_callback: list[int | None] = pr_number_holder,
            head_shas: list[str | None] = head_sha_holder,
            ci_reasons: list[ReasonCode | None] = ci_reason_holder,
            ci_details: list[str | None] = ci_detail_holder,
            issue_numbers: list[int | None] = issue_number_holder,
        ) -> None:
            pr_url_for_callback[0] = pr_url
            pr_number_for_callback[0] = pr_number
            state_store.append(
                candidate_for_callback.model_copy(
                    update={
                        "state": CandidateState.PR_CREATED,
                        "pr_number": pr_number,
                        "pr_url": pr_url,
                        "head_sha": head_shas[0],
                        "reason": ci_reasons[0],
                        "reason_detail": ci_details[0],
                        "comment_url": candidate_for_callback.comment_url,
                        "issue_number": issue_numbers[0],
                        "artifact_degraded": candidate_for_callback.artifact_degraded,
                        "merge_verified": candidate_for_callback.merge_verified,
                        "auto_merge_requested": candidate_for_callback.auto_merge_requested,
                        "ci_evidence_mode": ci_mode_state[0].value,
                    }
                )
            )

        def ci_probe(
            pr_number: int,
            candidate_for_probe: Candidate = candidate,
            planner_for_probe: Mapping[str, object] = planner_outputs.get(
                candidate.candidate_id, {}
            ),
            reviewer_for_probe: Mapping[str, object] = reviewer_outputs.get(
                candidate.candidate_id, {}
            ),
            wait_for_ci: bool = True,
            head_shas: list[str | None] = head_sha_holder,
            ci_reasons: list[ReasonCode | None] = ci_reason_holder,
            ci_details: list[str | None] = ci_detail_holder,
            commit_messages_holder: list[str | None] = commit_message_holder,
        ) -> CiWaitResult:
            head_sha, is_fork = client.pull_request_head_metadata(pr_number)
            head_shas[0] = head_sha
            if head_sha is None:
                ci_reasons[0] = ReasonCode.CI_EVIDENCE_UNAVAILABLE
                return CiWaitResult(ci_mode_state[0], ci_reasons[0], False)
            base_for_probe = candidate_for_probe.base_sha
            if base_for_probe is None:
                ci_reasons[0] = ReasonCode.CI_EVIDENCE_UNAVAILABLE
                return CiWaitResult(ci_mode_state[0], ci_reasons[0], False)
            commit_messages = client.commit_messages_between(base_for_probe, head_sha)
            if not commit_messages or any(
                _SIGNOFF_TRAILER.search(message) is None for message in commit_messages
            ):
                ci_reasons[0] = ReasonCode.DCO_TRAILER_MISSING
                return CiWaitResult(ci_mode_state[0], ci_reasons[0], False)
            commit_messages_holder[0] = commit_messages[-1]
            if not wait_for_ci:
                return CiWaitResult(ci_mode_state[0], None, False)
            wait_started = time.monotonic()
            wait_result = wait_for_required_contexts(
                config,
                client=transport,
                elapsed_s=ci_elapsed_s[0],
                sha=head_sha,
                poll=True,
                on_mode_transition=record_ci_mode_transition,
                ci_mode=ci_mode_state[0],
                is_fork=is_fork,
            )
            ci_elapsed_s[0] += time.monotonic() - wait_started
            ci_mode_state[0] = wait_result.mode
            ci_reasons[0] = wait_result.reason
            ci_details[0] = wait_result.detail
            return wait_result

        def after_ci(
            pr_number: int,
            candidate_for_body: Candidate = candidate,
            planner_for_body: Mapping[str, object] = planner_outputs.get(
                candidate.candidate_id, {}
            ),
            reviewer_for_body: Mapping[str, object] = reviewer_outputs.get(
                candidate.candidate_id, {}
            ),
            issue_numbers: list[int | None] = issue_number_holder,
            commit_messages: list[str | None] = commit_message_holder,
        ) -> None:
            if commit_messages[0] is None:
                return
            rendered = render_pr_body(
                (config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
                    encoding="utf-8"
                ),
                candidate_for_body,
                planner_for_body,
                reviewer_for_body,
                automation_metadata={"mode": "live", "would_write": False},
                issue_number=issue_numbers[0],
                commit_message=commit_messages[0],
            )
            validate_pr_body(rendered)
            client.patch_pr_body(pr_number, rendered)

        def after_issue_patched(
            issue_url: str,
            candidate_for_callback: Candidate = candidate,
            pr_url_for_callback: list[str | None] = pr_url_holder,
            pr_numbers: list[int | None] = pr_number_holder,
            issue_numbers: list[int | None] = issue_number_holder,
            head_shas: list[str | None] = head_sha_holder,
            ci_reasons: list[ReasonCode | None] = ci_reason_holder,
            ci_details: list[str | None] = ci_detail_holder,
        ) -> None:
            state_store.append(
                candidate_for_callback.model_copy(
                    update={
                        "state": CandidateState.ISSUE_PATCHED,
                        "pr_url": pr_url_for_callback[0],
                        "pr_number": pr_numbers[0],
                        "issue_number": issue_numbers[0],
                        "issue_url": issue_url,
                        "head_sha": head_shas[0],
                        "reason": ci_reasons[0],
                        "reason_detail": ci_details[0],
                        "comment_url": candidate_for_callback.comment_url,
                        "head_branch": candidate_for_callback.head_branch,
                    }
                )
            )

        def after_comment_created(
            comment_url: str,
            candidate_for_callback: Candidate = candidate,
            pr_url_for_callback: list[str | None] = pr_url_holder,
            pr_numbers: list[int | None] = pr_number_holder,
            head_shas: list[str | None] = head_sha_holder,
        ) -> None:
            state_store.append(
                candidate_for_callback.model_copy(
                    update={
                        "state": CandidateState.COMMENT_CREATED,
                        "pr_url": pr_url_for_callback[0],
                        "pr_number": pr_numbers[0],
                        "head_sha": head_shas[0],
                        "comment_url": comment_url,
                        "reason": ReasonCode.ARTIFACT_DEGRADED,
                        "reason_detail": None,
                        "artifact_degraded": True,
                    }
                )
            )

        try:
            if config.has_issues:
                links = publish_artifacts(
                    client,
                    candidate,
                    issue_title=issue_title,
                    issue_body=issue_body,
                    pr_title=pr_title,
                    pr_body=pr_body,
                    head=branch,
                    base=base_branch,
                    labels=labels,
                    ci_probe=ci_probe,
                    existing_issue_number=candidate.issue_number,
                    existing_issue_url=candidate.issue_url,
                    existing_pr_number=candidate.pr_number,
                    existing_pr_url=candidate.pr_url,
                    after_issue=after_issue,
                    after_pr_created=after_pr_created,
                    after_ci=after_ci,
                    after_issue_patched=after_issue_patched,
                )
            else:
                links = publish_degraded(
                    client,
                    candidate,
                    pr_title=pr_title,
                    pr_body=pr_body,
                    comment_body=issue_body,
                    head=branch,
                    base=base_branch,
                    after_pr_created=after_pr_created,
                    after_comment_created=after_comment_created,
                )
                if links.pr_number is not None:
                    ci_probe(links.pr_number, wait_for_ci=False)
        except ClosedPullRequestError:
            latest = state_store.resume(candidate.candidate_id) or candidate
            closed = latest.model_copy(
                update={
                    "state": CandidateState.TERMINAL,
                    "action": Action.HUMAN_REVIEW,
                    "reason": ReasonCode.CLOSED_PULL_REQUEST,
                }
            )
            state_store.append(closed)
            published.append(closed)
            continue
        except MergedPullRequestError as exc:
            latest = state_store.resume(candidate.candidate_id) or candidate
            pipeline_verified = (
                latest.state is CandidateState.ISSUE_PATCHED
                and latest.pr_number == exc.match.number
                and latest.reason is None
                and latest.auto_merge_requested
            )
            merged = latest.model_copy(
                update={
                    "state": CandidateState.TERMINAL,
                    "pr_number": exc.match.number,
                    "pr_url": exc.match.url,
                    "merged_at": exc.match.merged_at,
                    "reason": (
                        None if pipeline_verified else ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
                    ),
                    "merge_verified": pipeline_verified,
                }
            )
            state_store.append(merged)
            published.append(merged)
            continue
        except (
            ArtifactUnavailableError,
            GitHubResponseError,
            StatePreservationError,
            ArtifactValidationError,
            PreflightError,
            HttpTransportError,
            OSError,
        ):
            latest = state_store.resume(candidate.candidate_id) or candidate
            deferred = latest.model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                    "auto_merge_eligible": False,
                }
            )
            state_store.append(deferred)
            published.append(deferred)
            continue
        if ci_reason_holder[0] is not None and "needs-human-review" not in labels:
            if client.ensure_label("needs-human-review"):
                labels.append("needs-human-review")
            else:
                label_failures.append("needs-human-review")
        if links.pr_number is not None and labels:
            try:
                client.add_labels(links.pr_number, labels)
            except HttpTransportError:
                label_failures.extend(label for label in labels if label not in label_failures)
        comment_url_value: str | None = candidate.comment_url
        if label_failures and links.pr_number is not None:
            try:
                comment_url_value = client.comment_pr(
                    links.pr_number,
                    "Label publication was unavailable for: "
                    + ", ".join(sorted(set(label_failures)))
                    + ". Human review is required.",
                )
            except HttpTransportError:
                pass
        latest_after_publication = state_store.resume(candidate.candidate_id)
        publication_source = latest_after_publication or candidate
        if ci_reason_holder[0] is not None:
            publication_source = publication_source.model_copy(
                update={
                    "reason": ci_reason_holder[0],
                    "reason_detail": ci_detail_holder[0],
                    "auto_merge_eligible": False,
                }
            )
        elif label_failures:
            publication_source = publication_source.model_copy(
                update={
                    "auto_merge_eligible": False,
                    "artifact_degraded": True,
                }
            )
        published_candidate = publication_source.model_copy(
            update={
                "state": (
                    CandidateState.ISSUE_PATCHED
                    if config.has_issues
                    else CandidateState.COMMENT_CREATED
                ),
                "issue_number": links.issue_number or publication_source.issue_number,
                "pr_number": links.pr_number,
                "issue_url": links.issue_url or publication_source.issue_url,
                "pr_url": links.pr_url,
                "head_branch": branch,
                "head_sha": head_sha_holder[0] or publication_source.head_sha,
                "comment_url": comment_url_value,
                "merged_at": publication_source.merged_at,
                "ci_evidence_mode": ci_mode_state[0].value,
                "auto_merge_requested": (
                    links.auto_merge_requested or publication_source.auto_merge_requested
                ),
                "merge_verified": publication_source.merge_verified,
            }
        )
        latest_persisted: Candidate | None = latest_after_publication
        if latest_persisted is None or latest_persisted.model_dump(
            mode="json"
        ) != published_candidate.model_dump(mode="json"):
            state_store.append(published_candidate)
            latest_persisted = published_candidate
        published.append(latest_persisted or published_candidate)
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


def _prepare_live_candidate(
    candidate: Candidate,
    *,
    state_store: CandidateStateStore,
    client: GitHubClient,
    base_sha: str,
    head_branch: str,
) -> Candidate:
    """Reserve and create a candidate branch before any role session starts."""
    persisted = state_store.resume(candidate.candidate_id)
    decision = state_store.resume_decision(candidate.candidate_id)
    if decision.action is ResumeAction.SKIP:
        return persisted if persisted is not None else candidate
    if decision.action is ResumeAction.DEFER:
        deferred = (persisted or candidate).model_copy(
            update={
                "state": CandidateState.DEFERRED,
                "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
            }
        )
        state_store.append(deferred)
        return deferred
    branch = (
        persisted.head_branch
        if persisted is not None and persisted.head_branch is not None
        else f"{head_branch.rstrip('/')}/{candidate.candidate_id}"
    )
    prepared = (persisted or candidate).model_copy(
        update={
            "state": CandidateState.DISPATCHING,
            "base_sha": persisted.base_sha if persisted and persisted.base_sha else base_sha,
            "head_branch": branch,
        }
    )
    if persisted is None:
        if not state_store.append_if_new_artifact(prepared):
            resumed = state_store.resume(candidate.candidate_id)
            if resumed is None:
                deferred = candidate.model_copy(
                    update={
                        "state": CandidateState.DEFERRED,
                        "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                    }
                )
                state_store.append(deferred)
                return deferred
            prepared = resumed.model_copy(
                update={
                    "base_sha": resumed.base_sha or base_sha,
                    "head_branch": resumed.head_branch or branch,
                }
            )
    else:
        if persisted.model_dump(mode="json") != prepared.model_dump(mode="json"):
            state_store.append(prepared)
    try:
        client.create_branch(prepared.head_branch or branch, prepared.base_sha or base_sha)
    except HttpTransportError as exc:
        if exc.status_code != 422:
            raise
        existing_sha = client.branch_sha(prepared.head_branch or branch)
        recorded_base = prepared.base_sha or base_sha
        descendant = (
            existing_sha is not None
            and recorded_base is not None
            and existing_sha != recorded_base
            and bool(client.commit_messages_between(recorded_base, existing_sha))
        )
        if existing_sha != recorded_base and not descendant:
            deferred = prepared.model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                }
            )
            state_store.append(deferred)
            return deferred
        if existing_sha is not None:
            prepared = prepared.model_copy(update={"head_sha": existing_sha})
    if persisted is not None:
        for field in (
            "issue_url",
            "pr_url",
            "comment_url",
            "merged_at",
            "issue_number",
            "pr_number",
        ):
            previous = getattr(persisted, field)
            current = getattr(prepared, field)
            if previous is not None and current != previous:
                raise StatePreservationError(f"resume preparation discarded persisted {field}")
        if persisted.head_sha is not None:
            if prepared.head_sha is None:
                raise StatePreservationError("resume preparation discarded persisted head_sha")
    return prepared


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
    ci_mode_events: list[RunEventRecord] = []
    ci_mode_state = [config.ci_evidence_mode]
    ci_elapsed_s = [0.0]
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
                "auto_merge_enabled": config.auto_merge_enabled,
            }
        )
        preflight_notes.extend(preflight.notes)
        if not preflight.code_scanning_available:
            config = config.model_copy(update={"alert_source": AlertSource.SARIF_FILE})
        session_transport = UrllibDevinTransport()
    repo_name = f"{config.target_owner}/{config.target_repo}"
    marker_search = None
    if github_transport is not None:
        marker_search = github_marker_search(
            lambda marker: github_transport.get(
                "/search/issues?"
                + urlencode({"q": f'repo:{config.target_owner}/{config.target_repo} "{marker}"'})
            )
        )
    state_store = CandidateStateStore(
        output_dir / "state" / "candidates.jsonl",
        marker_search=marker_search,
    )
    valid = baseline.get("baseline_valid_lanes")
    valid_lanes = {str(item) for item in valid} if isinstance(valid, list) else set()
    baseline_major = baseline.get("current_major")
    current_major = baseline_major if isinstance(baseline_major, int) else None
    candidates: list[Candidate] = []
    skipped_failures: list[dict[str, str | int | None]] = []
    deprecation_failures: list[dict[str, str | int | bool | None]] = []
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
            skipped, _failures = enumerate_skipped_tests(
                repo_path,
                repo_name=repo_name,
                failures=skipped_failures,
            )
            candidates.extend(skipped)
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
                    failures=deprecation_failures,
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

    prior_rows = state_store.latest()
    normalized_candidates: list[Candidate] = []
    for candidate in candidates:
        prior = prior_rows.get(candidate.candidate_id)
        if prior is not None:
            if prior.pr_url is not None or prior.issue_url is not None:
                normalized_candidates.append(prior)
            else:
                normalized_candidates.append(candidate)
            continue
        if candidate.lane is Lane.CODEQL:
            drifted = state_store.drift_match(candidate, current_scan=candidates)
            if drifted is not None and drifted.superseded_by is None:
                state_store.supersede(drifted, candidate)
                candidate = candidate.model_copy(
                    update={
                        "state": drifted.state,
                        "issue_number": drifted.issue_number,
                        "pr_number": drifted.pr_number,
                        "issue_url": drifted.issue_url,
                        "pr_url": drifted.pr_url,
                        "base_sha": drifted.base_sha,
                    }
                )
        normalized_candidates.append(candidate)
    candidates = normalized_candidates
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
    orchestrator = RuntimeOrchestrator(SessionClient(config, transport=session_transport))
    reviewed: list[Candidate] = []
    session_candidates: list[Candidate] = []
    live_client: GitHubClient | None = None
    if config.mode is Mode.LIVE:
        if github_transport is None or head_branch is None:
            raise RunAbort("LIVE publication transport is unavailable")
        base_response = github_transport.get(
            f"/repos/{config.target_owner}/{config.target_repo}/git/ref/heads/{base_branch}"
        )
        resolved_base_sha = base_sha
        if isinstance(base_response, Mapping):
            raw_object = base_response.get("object")
            if isinstance(raw_object, Mapping) and isinstance(raw_object.get("sha"), str):
                resolved_base_sha = str(raw_object["sha"])
        if resolved_base_sha is None:
            raise RunAbort("target base SHA is unavailable")
        live_client = GitHubClient(
            config,
            transport=github_transport,
        )
        for candidate in dispatched:
            if candidate.action is not Action.OPEN_PR:
                reviewed.append(candidate)
                continue
            prepared = _prepare_live_candidate(
                candidate,
                state_store=state_store,
                client=live_client,
                base_sha=resolved_base_sha,
                head_branch=head_branch,
            )
            if prepared.pr_url is not None or prepared.state is CandidateState.DEFERRED:
                reviewed.append(prepared)
            else:
                session_candidates.append(prepared)
    else:
        session_candidates = [
            candidate for candidate in dispatched if candidate.action is Action.OPEN_PR
        ]
        if config.mode is Mode.SIMULATE:
            session_candidates = [
                candidate.model_copy(
                    update={
                        "base_sha": candidate.base_sha or "simulate-base",
                        "head_sha": candidate.head_sha or "simulate-head",
                    }
                )
                for candidate in session_candidates
            ]
        reviewed.extend(
            candidate.model_copy(update={"state": CandidateState.ISSUE_CREATED})
            if candidate.action is Action.OPEN_ISSUE
            and candidate.state is CandidateState.DISPATCHING
            else candidate
            for candidate in dispatched
            if candidate.action is not Action.OPEN_PR
        )
    planner_outputs: dict[str, Mapping[str, object]] = {}
    reviewer_outputs: dict[str, Mapping[str, object]] = {}
    for candidate in session_candidates:
        head_sha_resolver = None
        if (
            config.mode is Mode.LIVE
            and live_client is not None
            and candidate.head_branch is not None
        ):
            branch_for_head = candidate.head_branch
            client_for_head = live_client

            def resolve_head_sha(
                branch: str = branch_for_head,
                client: GitHubClient = client_for_head,
            ) -> str | None:
                return client.branch_sha(branch)

            head_sha_resolver = resolve_head_sha
        target_repo = f"{config.target_owner}/{config.target_repo}"
        prompt_base_sha = candidate.base_sha or base_sha or "unknown"
        prompt_head_branch = candidate.head_branch or head_branch or "candidate"

        def make_prompts(
            planner_output: Mapping[str, object],
            candidate: Candidate = candidate,
            target_repo: str = target_repo,
            prompt_base_sha: str = prompt_base_sha,
            prompt_head_branch: str = prompt_head_branch,
        ) -> tuple[str, str, str]:
            return (
                render_implementer_prompt(
                    candidate,
                    target_repo=target_repo,
                    base_sha=prompt_base_sha,
                    head_branch=prompt_head_branch,
                    planner_output=planner_output,
                ),
                render_reviewer_prompt(
                    candidate,
                    target_repo=target_repo,
                    base_sha=prompt_base_sha,
                    head_branch=prompt_head_branch,
                    planner_output=planner_output,
                ),
                render_reviewer_phase_b_prompt(
                    candidate,
                    target_repo=target_repo,
                    base_sha=prompt_base_sha,
                    head_branch=prompt_head_branch,
                    planner_output=planner_output,
                    committed_diff="",
                ),
            )

        try:
            result = orchestrator.run_candidate(
                candidate.candidate_id,
                render_planner_prompt(
                    candidate,
                    target_repo=target_repo,
                    base_sha=prompt_base_sha,
                    head_branch=prompt_head_branch,
                ),
                "planner context is supplied after the planner session",
                "planner context is supplied after the planner session",
                candidate=candidate,
                head_sha_resolver=head_sha_resolver,
                prompt_factory=make_prompts,
            )
        except (
            PlannerOutputError,
            SessionCeilingError,
            SessionDedupeError,
            RoleCollisionError,
            TimeoutError,
            HttpTransportError,
        ) as exc:
            reason = getattr(exc, "reason", None)
            if not isinstance(reason, ReasonCode):
                reason = (
                    ReasonCode.ROLE_COLLISION
                    if isinstance(exc, RoleCollisionError)
                    else ReasonCode.CAPABILITY_UNAVAILABLE
                )
            latest = state_store.resume(candidate.candidate_id) or candidate
            deferred = latest.model_copy(
                update={
                    "state": CandidateState.DEFERRED,
                    "reason": reason,
                    "reason_detail": str(exc) if isinstance(exc, PlannerOutputError) else None,
                }
            )
            state_store.append(deferred)
            reviewed.append(deferred)
            continue
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
        if (
            config.mode is Mode.LIVE
            and live_client is not None
            and candidate.head_branch is not None
        ):
            resolved_head = live_client.branch_sha(candidate.head_branch)
            if resolved_head is not None:
                reviewed_candidate = reviewed_candidate.model_copy(
                    update={"head_sha": resolved_head}
                )
        if isinstance(reviewer_output, Mapping):
            raw_tests = reviewer_output.get("tests")
            tests = raw_tests if isinstance(raw_tests, list) else []
            test_paths: list[str] = []
            for item in tests:
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    path = str(item["path"])
                    if path not in test_paths:
                        test_paths.append(path)
            reviewed_candidate = reviewed_candidate.model_copy(
                update={
                    "test_added": bool(tests),
                    "test_paths": test_paths,
                    "test_author": result.reviewer.snapshot.session_id,
                }
            )
        if result.review is not None:
            reviewed_candidate = apply_review_result(reviewed_candidate, result.review)
            if result.review.reason is ReasonCode.STALE_SKIP:
                reviewed_candidate = reviewed_candidate.model_copy(
                    update={
                        "test_added": False,
                        "test_exempt_reason": ReasonCode.STALE_SKIP,
                    }
                )
            elif not reviewed_candidate.test_added:
                reviewed_candidate = reviewed_candidate.model_copy(
                    update={"auto_merge_eligible": False}
                )
        else:
            reviewed_candidate = reviewed_candidate.model_copy(
                update={
                    "state": CandidateState.TERMINAL,
                    "reason": ReasonCode.DISAGREEMENT_UNRESOLVED,
                    "action": Action.HUMAN_REVIEW,
                    "auto_merge_eligible": False,
                }
            )
        reviewed.append(reviewed_candidate)

    notes = _capability_notes(
        baseline,
        target_exists=target_exists,
        config=config,
    )
    notes.extend(preflight_notes)
    notes.extend(
        f"enumeration failure: {failure.get('path', '<unknown>')}"
        for failure in [*skipped_failures, *deprecation_failures]
    )
    if state_store.quarantined_rows:
        notes.append(f"state rows quarantined: {state_store.quarantined_rows}")
    if config.mode is Mode.LIVE:
        if preflight is None:
            raise RunAbort("LIVE preflight did not produce a capability result")
        if github_transport is None or head_branch is None:
            raise RunAbort("LIVE publication transport is unavailable")
        try:
            reviewed = _publish_live(
                reviewed,
                config=config,
                output_dir=output_dir,
                repo_path=repo_path,
                planner_outputs=planner_outputs,
                reviewer_outputs=reviewer_outputs,
                base_sha=base_sha,
                head_branch=head_branch,
                base_branch=base_branch,
                transport=github_transport,
                state_store=state_store,
                ci_mode_events=ci_mode_events,
                ci_mode_state=ci_mode_state,
                ci_elapsed_s=ci_elapsed_s,
                run_id=run_id,
            )
        except (
            ArtifactUnavailableError,
            GitHubResponseError,
            StatePreservationError,
            ArtifactValidationError,
            PreflightError,
            HttpTransportError,
            OSError,
        ):
            notes.append("publication capability unavailable; affected candidates deferred")
            recovered: list[Candidate] = []
            for candidate in reviewed:
                latest = state_store.resume(candidate.candidate_id) or candidate
                if latest.state in {
                    CandidateState.TERMINAL,
                    CandidateState.CONVERGED,
                    CandidateState.ISSUE_PATCHED,
                    CandidateState.COMMENT_CREATED,
                }:
                    recovered.append(latest)
                    continue
                deferred = latest.model_copy(
                    update={
                        "state": CandidateState.DEFERRED,
                        "reason": ReasonCode.CAPABILITY_UNAVAILABLE,
                        "auto_merge_eligible": False,
                    }
                )
                state_store.append(deferred)
                recovered.append(deferred)
            reviewed = recovered
        if state_store.marker_search_failed:
            notes.append(
                "marker search failed; no candidate can be dispatched while dedupe "
                "capability is unavailable"
            )
            ci_mode_events.append(
                RunEventRecord(
                    event_type="marker_search_failure",
                    run_id=run_id,
                    transition_reason=ReasonCode.CAPABILITY_UNAVAILABLE,
                    reason_detail="marker_search_failed",
                )
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
        token_login=preflight.token_login if preflight is not None else None,
        token_scopes=preflight.token_scopes if preflight is not None else (),
        run_events=ci_mode_events,
    )
    if config.mode is Mode.LIVE and state_store.marker_search_failed:
        raise RunAbort("capability_unavailable: marker_search_failed")
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
    except (
        ArtifactUnavailableError,
        ConfigError,
        HttpTransportError,
        RunAbort,
        ValueError,
        OSError,
    ) as exc:
        print(f"pipeline run aborted: {exc}", file=sys.stderr)
        return 1
    print(f"run_id={run_id}")
    for path in produced:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
