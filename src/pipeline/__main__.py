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
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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
    auto_merge_eligible,
    reconcile_issue,
    reconcile_pull_request,
    run_live_preflight,
    wait_for_check_runs,
)
from pipeline.http_transport import HttpTransportError, UrllibDevinTransport, UrllibGitHubTransport
from pipeline.lanes.codeql import enumerate_from_config, read_alert_fixture
from pipeline.lanes.deprecations import enumerate_deprecations, is_eol
from pipeline.lanes.skipped_tests import enumerate_skipped_tests
from pipeline.observers import LocalCheckout, PullRequestAlerts
from pipeline.prompts import render_fix_prompt
from pipeline.rubric import load_rubrics
from pipeline.schemas import (
    Action,
    Candidate,
    CandidateState,
    DefinitionKind,
    Lane,
    MergeMode,
    ReasonCode,
    RunEventRecord,
    Tier,
)
from pipeline.score import apply_score
from pipeline.session_client import (
    DevinTransport,
    RuntimeOrchestrator,
    SessionAttempt,
    SessionBlockedError,
    SessionCeilingError,
    SessionClient,
    SessionDedupeError,
    SessionInfeasibleError,
    SessionOutputError,
)
from pipeline.simulation import simulate_run
from pipeline.simulation_fixtures import simulated_observers
from pipeline.state import (
    CandidateStateStore,
    MarkerSearchOutcome,
    StatePreservationError,
    github_marker_search,
)
from pipeline.templates.render import (
    ArtifactValidationError,
    candidate_marker,
    render_issue_body,
    render_issue_title,
    render_pr_body,
    render_pr_title,
    validate_issue_body,
    validate_pr_body,
    validate_pr_title,
)
from pipeline.verify import Observers, post_pr_criterion_pending, verify_candidate


class RunAbort(RuntimeError):
    """Raised when a blocking capability or runtime guard aborts a run."""


_SIGNOFF_TRAILER = re.compile(r"(?im)^Signed-off-by:\s+\S+\s+<[^>\r\n]+>\s*$")
_MAX_SESSION_ATTEMPTS = 2
_PUBLISHING_ACTIONS = frozenset({Action.OPEN_PR, Action.OPEN_ISSUE})
_MARKER_DEFER_REASONS = {
    MarkerSearchOutcome.FAILED: ReasonCode.MARKER_SEARCH_FAILED,
    MarkerSearchOutcome.ORPHANED: ReasonCode.MARKER_SEARCH_FAILED,
    MarkerSearchOutcome.UNCONFIGURED: ReasonCode.MARKER_SEARCH_UNCONFIGURED,
}


@dataclass
class LiveTarget:
    """The live fork, its transport and the base the run pins candidates to."""

    client: GitHubClient
    transport: UrllibGitHubTransport
    base_branch: str
    branch_prefix: str


@dataclass
class CandidateRunner:
    """Drive one candidate through the §6 lifecycle, one write at a time."""

    config: PipelineConfig
    run_id: str
    state_store: CandidateStateStore
    orchestrator: RuntimeOrchestrator
    observers: Observers
    output_dir: Path
    base_sha: str
    live: LiveTarget | None = None
    checkout: LocalCheckout | None = None
    notes: list[str] = field(default_factory=list)
    run_events: list[RunEventRecord] = field(default_factory=list)
    fix_outputs: dict[str, Mapping[str, object]] = field(default_factory=dict)
    ci_mode: CiEvidenceMode = CiEvidenceMode.LOCAL
    ci_elapsed_s: float = 0.0

    # -- persistence -----------------------------------------------------

    def _persist(self, candidate: Candidate, **update: object) -> Candidate:
        """Append one lifecycle row, preserving durable artifact identity."""
        updated = candidate.model_copy(update=update)
        self.state_store.append(updated)
        return updated

    def _terminal(
        self,
        candidate: Candidate,
        reason: ReasonCode,
        detail: str | None = None,
    ) -> Candidate:
        return self._persist(
            candidate,
            state=CandidateState.TERMINAL,
            reason=reason,
            reason_detail=detail,
            auto_merge_eligible=False,
        )

    def _deferred(
        self,
        candidate: Candidate,
        reason: ReasonCode,
        detail: str | None = None,
    ) -> Candidate:
        return self._persist(
            candidate,
            state=CandidateState.DEFERRED,
            reason=reason,
            reason_detail=detail,
            auto_merge_eligible=False,
        )

    # -- lifecycle -------------------------------------------------------

    def process(self, candidate: Candidate) -> Candidate:
        """Run one candidate from `scored` to its terminal or deferred row."""
        if candidate.action not in _PUBLISHING_ACTIONS:
            return candidate
        try:
            reconciled = self._reconcile(candidate)
            if self._settled(reconciled):
                return reconciled
            published = self._publish_issue(reconciled)
            if self._settled(published) or published.action is Action.OPEN_ISSUE:
                return published
            dispatched = self._dispatch(published)
            if self._settled(dispatched):
                return dispatched
            verified = self._verify(dispatched)
            if self._settled(verified):
                return verified
            opened = self._publish_pr(verified)
            if self._settled(opened):
                return opened
            return self._settle_merge(opened)
        except (
            ArtifactUnavailableError,
            GitHubResponseError,
            HttpTransportError,
            StatePreservationError,
            OSError,
        ) as exc:
            self.notes.append(f"{candidate.candidate_id}: capability unavailable, deferred")
            latest = self.state_store.resume(candidate.candidate_id) or candidate
            return self._deferred(latest, ReasonCode.CAPABILITY_UNAVAILABLE, str(exc))

    @staticmethod
    def _settled(candidate: Candidate) -> bool:
        return candidate.state in {
            CandidateState.TERMINAL,
            CandidateState.DEFERRED,
            CandidateState.MERGED,
            CandidateState.AWAITING_HUMAN_MERGE,
        }

    def _reconcile(self, candidate: Candidate) -> Candidate:
        """Adopt persisted and fork-side artifact identity before any write."""
        outcome = self.state_store.marker_search_outcome(candidate.candidate_id)
        candidate = candidate.model_copy(update={"marker_search_outcome": outcome.value})
        if outcome is MarkerSearchOutcome.UNCONFIGURED and self.config.mode is not Mode.LIVE:
            outcome = MarkerSearchOutcome.ABSENT
        defer_reason = _MARKER_DEFER_REASONS.get(outcome)
        if defer_reason is not None:
            return self._deferred(candidate, defer_reason, outcome.value)
        update: dict[str, object] = {"base_sha": candidate.base_sha or self.base_sha}
        persisted = self.state_store.resume(candidate.candidate_id)
        if persisted is not None:
            update.update(
                {
                    "issue_number": persisted.issue_number,
                    "issue_url": persisted.issue_url,
                    "pr_number": persisted.pr_number,
                    "pr_url": persisted.pr_url,
                    "head_branch": persisted.head_branch or candidate.head_branch,
                    "head_sha": persisted.head_sha or candidate.head_sha,
                    "merged_at": persisted.merged_at,
                    "session_id": persisted.session_id or candidate.session_id,
                }
            )
        artifact = self.state_store.marker_artifact(candidate.candidate_id)
        if artifact is not None and not artifact.is_pull_request:
            update["issue_number"] = artifact.number
            update["issue_url"] = artifact.url
        if persisted is not None and persisted.merged_at is not None:
            return self._persist(candidate, state=CandidateState.MERGED, **update)
        return candidate.model_copy(update=update)

    def _issue_template(self, candidate: Candidate) -> str:
        if candidate.lane is Lane.CODEQL:
            return ""
        name = {
            Lane.SKIPPED_TESTS: "issues/bug_report.yml",
            Lane.DEPRECATIONS: "issues/sip.md",
        }[candidate.lane]
        return (self.config.templates_dir / name).read_text(encoding="utf-8")

    def _publish_issue(self, candidate: Candidate) -> Candidate:
        """Write, or adopt, the one tracking issue every published tier gets."""
        summary = (
            f"Remediation tracking for {candidate.stable_locator}."
            if self.config.mode is Mode.LIVE
            else f"SIMULATED remediation for {candidate.candidate_id}."
        )
        not_automated = (
            "Medium tier: the pipeline tracks this candidate without dispatching a session."
            if candidate.tier is Tier.MEDIUM
            else None
        )
        try:
            body = render_issue_body(
                self._issue_template(candidate),
                candidate,
                generated_summary=summary,
                verification=candidate.success_criterion
                or "Remediation status is tracked by the pipeline.",
                not_automated_reason=not_automated,
            )
            validate_issue_body(body, candidate)
        except ArtifactValidationError as exc:
            return self._deferred(candidate, ReasonCode.ARTIFACT_VALIDATION_FAILED, str(exc))
        report = self.output_dir / "reports" / "issues" / f"{candidate.candidate_id}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(body, encoding="utf-8")
        if self.live is None:
            return self._persist(candidate, state=CandidateState.ISSUE_CREATED)
        labels: list[str] = []
        for label in candidate.labels:
            self.live.client.ensure_label(label)
            labels.append(label)
        number, url, adopted = reconcile_issue(
            self.live.client,
            marker=candidate_marker(candidate.candidate_id),
            title=render_issue_title(candidate, render_pr_title(candidate)),
            body=body,
            labels=labels,
            existing_issue_number=candidate.issue_number,
            existing_issue_url=candidate.issue_url,
        )
        if adopted:
            self.notes.append(f"{candidate.candidate_id}: adopted existing issue #{number}")
        return self._persist(
            candidate,
            state=CandidateState.ISSUE_CREATED,
            issue_number=number,
            issue_url=url,
        )

    def _dispatch(self, candidate: Candidate) -> Candidate:
        """Create the candidate branch, then run its one session."""
        prefix = self.live.branch_prefix if self.live is not None else "devin/remediation"
        branch = candidate.head_branch or f"{prefix.rstrip('/')}/{candidate.candidate_id}"
        prepared = self._persist(
            candidate,
            state=CandidateState.DISPATCHING,
            head_branch=branch,
            base_sha=candidate.base_sha or self.base_sha,
        )
        if self.live is not None:
            try:
                self.live.client.create_branch(branch, prepared.base_sha or self.base_sha)
            except HttpTransportError as exc:
                if exc.status_code != 422:
                    raise
                existing = self.live.client.branch_sha(branch)
                if existing is None:
                    return self._deferred(
                        prepared,
                        ReasonCode.CAPABILITY_UNAVAILABLE,
                        "candidate branch exists but its head is unreadable",
                    )
        return self._run_session(prepared)

    def _run_session(self, candidate: Candidate) -> Candidate:
        """Create and poll exactly one session per attempt, up to the ceiling."""
        prompt_branch = candidate.head_branch or "devin/remediation"
        prompt_base = candidate.base_sha or self.base_sha
        criterion = candidate.success_criterion or ""
        last_detail = "session produced no usable output"
        for attempt in range(1, _MAX_SESSION_ATTEMPTS + 1):
            prompt = render_fix_prompt(
                candidate,
                target_repo=f"{self.config.target_owner}/{self.config.target_repo}",
                base_sha=prompt_base,
                head_branch=prompt_branch,
                success_criterion=criterion,
                attempt=attempt,
                suite_scope=candidate.suite_scope,
            )

            def record(evidence: SessionAttempt, row: Candidate = candidate) -> None:
                """Persist the session identity before the session is polled."""
                self.state_store.append(row.model_copy(update={"session_id": evidence.session_id}))

            try:
                run = self.orchestrator.run_candidate(
                    candidate,
                    prompt,
                    attempt=attempt,
                    session_created=record,
                )
            except SessionCeilingError as exc:
                return self._deferred(candidate, ReasonCode.SESSION_CEILING, str(exc))
            except SessionInfeasibleError as exc:
                return self._terminal(
                    candidate.model_copy(update={"session_id": candidate.session_id}),
                    ReasonCode.SESSION_FAILED,
                    exc.output.infeasible_reason or str(exc),
                )
            except SessionDedupeError as exc:
                return self._terminal(candidate, ReasonCode.SESSION_FAILED, str(exc))
            except (SessionBlockedError, SessionOutputError, TimeoutError) as exc:
                last_detail = str(exc)
                continue
            return self._apply_fix_output(candidate, run)
        return self._terminal(candidate, ReasonCode.SESSION_BLOCKED, last_detail)

    def _apply_fix_output(self, candidate: Candidate, run: object) -> Candidate:
        """Record the validated session output as the candidate's session row."""
        from pipeline.session_client import SessionRun

        if not isinstance(run, SessionRun):  # pragma: no cover - defensive
            raise RunAbort("session run result is malformed")
        output = run.output
        head_sha = output.head_sha
        if self.live is not None and candidate.head_branch is not None:
            observed = self.live.client.branch_sha(candidate.head_branch)
            if observed is not None:
                head_sha = observed
        self.fix_outputs[candidate.candidate_id] = {
            "files_changed": list(output.files_changed),
            "verify_command": output.verify_command,
            "testing_notes": output.testing_notes,
            "criterion_notes": output.criterion_notes,
        }
        return self._persist(
            candidate,
            state=CandidateState.SESSION_DONE,
            session_id=run.attempt.session_id,
            head_sha=head_sha,
            test_nodeid=output.test_nodeid or candidate.nodeid,
            test_paths=list(output.test_paths),
            test_added=output.test_nodeid is not None,
            test_author=run.attempt.session_id,
            fix_summary=output.fix_summary,
            suite_scope=list(output.suite_scope) or candidate.suite_scope,
        )

    def _verify(self, candidate: Candidate) -> Candidate:
        """Evaluate the candidate's criterion from the orchestrator's own runs."""
        evidence, baseline = verify_candidate(
            candidate,
            base_sha=candidate.base_sha or self.base_sha,
            head_sha=candidate.head_sha or self.base_sha,
            observers=self.observers,
            config=self.config,
        )
        update: dict[str, object] = {"criterion_evidence": evidence}
        if baseline is not None:
            update["red_baseline"] = baseline
            update["lifted_markers"] = list(baseline.still_skipped_descendants)
        if evidence.reason is ReasonCode.STALE_SKIP:
            update["test_added"] = False
            update["test_exempt_reason"] = ReasonCode.STALE_SKIP
        if evidence.satisfied is False:
            return self._terminal(
                candidate.model_copy(update=update),
                evidence.reason or ReasonCode.CRITERION_NOT_MET,
                "; ".join(evidence.observations) or None,
            )
        return self._persist(candidate, state=CandidateState.VERIFIED, **update)

    def _pr_body(self, candidate: Candidate, commit_message: str | None) -> str:
        template = (self.config.templates_dir / "superset/PULL_REQUEST_TEMPLATE.md").read_text(
            encoding="utf-8"
        )
        fix_output = self.fix_outputs.get(candidate.candidate_id, {})
        metadata: dict[str, object] = {
            "mode": self.config.mode.value,
            "writes_suppressed": self.config.mode is Mode.SIMULATE,
            "artifact_simulated": self.config.mode is Mode.SIMULATE,
            "ci_evidence_mode": self.config.ci_evidence_mode.value,
            "merge_mode": (
                candidate.merge_mode.value if candidate.merge_mode is not None else "n/a"
            ),
            "session_id": candidate.session_id or "n/a",
            "session_verify_command": str(fix_output.get("verify_command", "n/a")),
            "diff_range": f"{candidate.base_sha or 'n/a'}..{candidate.head_sha or 'n/a'}",
        }
        return render_pr_body(
            template,
            candidate,
            automation_metadata=metadata,
            issue_number=candidate.issue_number,
            commit_message=commit_message,
        )

    def _publish_pr(self, candidate: Candidate) -> Candidate:
        """Write, or adopt, the one pull request that closes the tracking issue."""
        title = render_pr_title(candidate)
        regex = (self.config.templates_dir / "superset/pr_title_regex.txt").read_text(
            encoding="utf-8"
        )
        if not validate_pr_title(title, regex):
            return self._deferred(
                candidate,
                ReasonCode.ARTIFACT_VALIDATION_FAILED,
                f"pull-request title violates the fork's pr-lint regex: {title}",
            )
        commit_message: str | None = None
        if self.live is not None:
            messages = self.live.client.commit_messages_between(
                candidate.base_sha or self.base_sha,
                candidate.head_sha or "",
            )
            unsigned = [message for message in messages if not _SIGNOFF_TRAILER.search(message)]
            if not messages or unsigned:
                return self._terminal(
                    candidate,
                    ReasonCode.DCO_TRAILER_MISSING,
                    "the candidate branch carries a commit without a Signed-off-by trailer",
                )
            commit_message = messages[-1]
        try:
            body = self._pr_body(candidate, commit_message)
            validate_pr_body(body, issue_number=candidate.issue_number)
        except ArtifactValidationError as exc:
            return self._deferred(candidate, ReasonCode.ARTIFACT_VALIDATION_FAILED, str(exc))
        report = self.output_dir / "reports" / "prs" / f"{candidate.candidate_id}.md"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(body, encoding="utf-8")
        if self.live is None:
            return self._persist(candidate, state=CandidateState.PR_CREATED)
        head = candidate.head_branch
        if head is None:
            raise RunAbort("candidate branch was not prepared before publication")
        try:
            number, url, adopted = reconcile_pull_request(
                self.live.client,
                title=title,
                body=body,
                head=head,
                base=self.live.base_branch,
                existing_pr_number=candidate.pr_number,
                existing_pr_url=candidate.pr_url,
            )
        except MergedPullRequestError as exc:
            return self._persist(
                candidate,
                state=CandidateState.MERGED,
                pr_number=exc.match.number,
                pr_url=exc.match.url,
                merged_at=exc.match.merged_at,
                merge_verified=True,
            )
        except ClosedPullRequestError as exc:
            update: dict[str, object] = {}
            if exc.match is not None:
                update = {"pr_number": exc.match.number, "pr_url": exc.match.url}
            return self._terminal(
                candidate.model_copy(update=update),
                ReasonCode.CLOSED_PULL_REQUEST,
                str(exc),
            )
        if adopted:
            self.notes.append(f"{candidate.candidate_id}: adopted existing pull request #{number}")
        return self._persist(
            candidate,
            state=CandidateState.PR_CREATED,
            pr_number=number,
            pr_url=url,
        )

    def _post_pr_observers(self, candidate: Candidate) -> Observers:
        """Return the observation seams available once the PR head exists."""
        if self.live is None:
            return self.observers
        observers = Observers(
            run_item=self.observers.run_item,
            run_suite=self.observers.run_suite,
            probe_symbol=self.observers.probe_symbol,
        )
        if candidate.pr_number is not None:
            alerts = PullRequestAlerts(
                config=self.config,
                reader=self.live.transport.get,
                pr_number=candidate.pr_number,
                repo_path=self.checkout.repo_path if self.checkout is not None else None,
            )
            observers.probe_alerts = alerts.probe
        return observers

    def _watch_ci(self, candidate: Candidate) -> CiWaitResult | None:
        """Poll the PR head's check runs, recording every observed conclusion."""
        if self.live is None or candidate.pr_number is None:
            return None
        head_sha, is_fork = self.live.client.pull_request_head_metadata(candidate.pr_number)

        def transitioned(transition: CiModeTransition) -> None:
            self.ci_mode = transition.mode
            self.run_events.append(
                RunEventRecord(
                    event_type="ci_mode_transition",
                    run_id=self.run_id,
                    mode_from=CiEvidenceMode.LOCAL.value,
                    mode_to=transition.mode.value,
                    transition_reason=transition.reason,
                )
            )

        result = wait_for_check_runs(
            self.config,
            client=self.live.transport,
            elapsed_s=self.ci_elapsed_s,
            sha=head_sha or candidate.head_sha or "HEAD",
            on_mode_transition=transitioned,
            ci_mode=self.ci_mode,
            is_fork=is_fork,
        )
        self.ci_mode = result.mode
        return result

    def _settle_merge(self, candidate: Candidate) -> Candidate:
        """Apply the §12 merge gate and settle the candidate's final row."""
        ci_result = self._watch_ci(candidate)
        update: dict[str, object] = {}
        if ci_result is not None:
            update["check_run_conclusions"] = list(ci_result.conclusions)
            update["ci_evidence_mode"] = ci_result.mode.value
        if post_pr_criterion_pending(candidate, self.config) and self.live is not None:
            evidence, _ = verify_candidate(
                candidate,
                base_sha=candidate.base_sha or self.base_sha,
                head_sha=candidate.head_sha or self.base_sha,
                observers=self._post_pr_observers(candidate),
                config=self.config,
                stage="post_pr",
            )
            update["criterion_evidence"] = evidence
            if evidence.satisfied is not True:
                return self._terminal(
                    candidate.model_copy(update=update),
                    evidence.reason or ReasonCode.CRITERION_NOT_MET,
                    "; ".join(evidence.observations) or None,
                )
        if ci_result is not None and ci_result.reason is not None:
            return self._terminal(
                candidate.model_copy(update=update),
                ci_result.reason,
                ci_result.detail,
            )
        candidate = candidate.model_copy(update=update)
        if self.live is not None and auto_merge_eligible(candidate, self.config, ci_result):
            self.live.client.enable_auto_merge(
                candidate.pr_number or 0,
                ci_mode=self.ci_mode,
            )
            merged = self.live.client.pull_request(candidate.pr_number or 0)
            if merged is not None and merged.merged_at is not None:
                return self._persist(
                    candidate,
                    state=CandidateState.MERGED,
                    merged_at=merged.merged_at,
                    merge_verified=True,
                    auto_merge_requested=True,
                )
            return self._persist(
                candidate,
                state=CandidateState.AWAITING_HUMAN_MERGE,
                reason=ReasonCode.CI_EVIDENCE_UNAVAILABLE,
                reason_detail="auto-merge was requested but the fork has not merged it yet",
                auto_merge_requested=True,
            )
        return self._persist(
            candidate,
            state=CandidateState.AWAITING_HUMAN_MERGE,
            reason=(
                ReasonCode.MANUAL_MERGE_REQUIRED
                if candidate.merge_mode is MergeMode.MANUAL
                else None
            ),
        )


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
    from pipeline.verify import declare_success_criterion

    candidate = Candidate(
        candidate_id=f"{lane.value}-{uuid.uuid5(uuid.NAMESPACE_URL, locator).hex}",
        lane=lane,
        repo=repo,
        stable_locator=locator,
        trigger_exists=True,
        verifiability_exists=True,
        success_criterion=declare_success_criterion(lane),
    )
    if lane is Lane.SKIPPED_TESTS:
        nodeid = str(record.get("nodeid", locator))
        return candidate.model_copy(
            update={
                "candidate_id": f"{lane.value}-{uuid.uuid5(uuid.NAMESPACE_URL, nodeid).hex}",
                "stable_locator": nodeid,
                "nodeid": nodeid,
                "suite_scope": [nodeid.split("::", 1)[0]],
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
            "suite_scope": [module.replace(".", "/") + ".py"] if module else [],
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


def _enumerate(
    *,
    config: PipelineConfig,
    baseline: Mapping[str, object],
    repo_path: Path,
    repo_name: str,
    target_exists: bool,
    base_sha: str | None,
    preflight: LivePreflight | None,
    notes: list[str],
) -> list[Candidate]:
    """Enumerate all three lanes for this run, fresh, regardless of trigger."""
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
            candidates.extend(
                enumerate_deprecations(
                    repo_path,
                    current_release_value=release if isinstance(release, str) else None,
                    current_major=current_major,
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
    notes.extend(
        f"enumeration failure: {failure.get('path', '<unknown>')}"
        for failure in [*skipped_failures, *deprecation_failures]
    )
    return candidates


def _normalize(
    candidates: Sequence[Candidate],
    *,
    state_store: CandidateStateStore,
) -> list[Candidate]:
    """Adopt persisted identity and link drifted LANE 1 alerts before gating."""
    prior_rows = state_store.latest()
    normalized: list[Candidate] = []
    for candidate in candidates:
        prior = prior_rows.get(candidate.candidate_id)
        if prior is not None:
            normalized.append(
                prior if prior.pr_url is not None or prior.issue_url is not None else candidate
            )
            continue
        current = candidate
        if candidate.lane is Lane.CODEQL:
            drifted = state_store.drift_match(candidate, current_scan=candidates)
            if drifted is not None and drifted.superseded_by is None:
                state_store.supersede(drifted, candidate)
                current = candidate.model_copy(
                    update={
                        "state": drifted.state,
                        "issue_number": drifted.issue_number,
                        "pr_number": drifted.pr_number,
                        "issue_url": drifted.issue_url,
                        "pr_url": drifted.pr_url,
                        "base_sha": drifted.base_sha,
                        "head_branch": drifted.head_branch,
                        "head_sha": drifted.head_sha,
                    }
                )
        normalized.append(current)
    return normalized


def _select(candidates: Sequence[Candidate], config: PipelineConfig) -> list[Candidate]:
    """Gate, score and dispatch every enumerated candidate deterministically."""
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
    return dispatch_candidates(selected, config)


def _resolve_base_sha(
    transport: UrllibGitHubTransport,
    *,
    config: PipelineConfig,
    base_branch: str,
    fallback: str | None,
) -> str:
    """Read the base branch head the run pins every candidate branch to."""
    response = transport.get(
        f"/repos/{config.target_owner}/{config.target_repo}/git/ref/heads/{base_branch}"
    )
    if isinstance(response, Mapping):
        raw_object = response.get("object")
        if isinstance(raw_object, Mapping) and isinstance(raw_object.get("sha"), str):
            return str(raw_object["sha"])
    if fallback is None:
        raise RunAbort("target base SHA is unavailable")
    return fallback


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
    """Execute the §6 lifecycle end to end for one run, then report on it."""
    run_id = uuid.uuid4().hex
    baseline = _load_baseline(baseline_path)
    if config.mode is Mode.SIMULATE and config.ci_evidence_mode is not CiEvidenceMode.LOCAL:
        config = config.model_copy(
            update={"ci_evidence_mode": CiEvidenceMode.LOCAL, "auto_merge_enabled": False}
        )
    target_exists = repo_path.exists()
    notes: list[str] = []
    run_events: list[RunEventRecord] = []
    preflight: LivePreflight | None = None
    session_transport: DevinTransport | None = None
    github_transport: UrllibGitHubTransport | None = None
    if config.mode is Mode.LIVE:
        if head_branch is None:
            raise RunAbort("LIVE requires --head-branch for candidate branch creation")
        github_transport = UrllibGitHubTransport()
        try:
            preflight = run_live_preflight(config, github_transport)
        except PreflightError as exc:
            raise RunAbort(f"{exc.reason.value}: {exc}") from exc
        config = config.model_copy(
            update={
                "has_issues": preflight.has_issues,
                "ci_evidence_mode": preflight.ci_evidence_mode,
            }
        )
        notes.extend(preflight.notes)
        if not preflight.code_scanning_available:
            config = config.model_copy(update={"alert_source": AlertSource.SARIF_FILE})
        session_transport = UrllibDevinTransport()
    repo_name = f"{config.target_owner}/{config.target_repo}"
    marker_search = None
    if github_transport is not None:
        transport_for_search = github_transport
        marker_search = github_marker_search(
            lambda marker: transport_for_search.get(
                "/search/issues?" + urlencode({"q": f'repo:{repo_name} "{marker}"'})
            )
        )
    state_store = CandidateStateStore(
        output_dir
        / "state"
        / ("candidates-live.jsonl" if config.mode is Mode.LIVE else "candidates.jsonl"),
        marker_search=marker_search,
        require_marker_proof=config.mode is Mode.LIVE,
        artifact_simulated=config.mode is Mode.SIMULATE,
    )
    candidates = _normalize(
        _enumerate(
            config=config,
            baseline=baseline,
            repo_path=repo_path,
            repo_name=repo_name,
            target_exists=target_exists,
            base_sha=base_sha,
            preflight=preflight,
            notes=notes,
        ),
        state_store=state_store,
    )
    dispatched = _select(candidates, config)

    live: LiveTarget | None = None
    resolved_base_sha = base_sha or "0000000"
    if config.mode is Mode.LIVE:
        if github_transport is None or head_branch is None:
            raise RunAbort("LIVE publication transport is unavailable")
        resolved_base_sha = _resolve_base_sha(
            github_transport,
            config=config,
            base_branch=base_branch,
            fallback=base_sha,
        )
        live = LiveTarget(
            client=GitHubClient(config, transport=github_transport),
            transport=github_transport,
            base_branch=base_branch,
            branch_prefix=head_branch,
        )
    checkout: LocalCheckout | None = None
    if target_exists:
        checkout = LocalCheckout(
            repo_path=repo_path,
            worktree_root=output_dir / "worktrees",
        )
    if config.mode is Mode.SIMULATE:
        observers = simulated_observers(base_sha=resolved_base_sha)
    elif checkout is not None:
        observers = Observers(
            run_item=checkout.run_item,
            run_suite=checkout.run_suite,
            probe_symbol=checkout.probe_symbol,
        )
    else:
        observers = Observers()
        notes.append("no target checkout: no criterion can be observed locally")
    runner = CandidateRunner(
        config=config,
        run_id=run_id,
        state_store=state_store,
        orchestrator=RuntimeOrchestrator(SessionClient(config, transport=session_transport)),
        observers=observers,
        output_dir=output_dir,
        base_sha=resolved_base_sha,
        live=live,
        checkout=checkout,
        notes=notes,
        run_events=run_events,
        ci_mode=config.ci_evidence_mode,
    )
    settled = [runner.process(candidate) for candidate in dispatched]

    notes.extend(_capability_notes(baseline, target_exists=target_exists, config=config))
    if state_store.quarantined_rows:
        notes.append(f"state rows quarantined: {state_store.quarantined_rows}")
    if state_store.marker_search_failed:
        notes.append("marker search failed; affected candidates were deferred without a write")
        run_events.append(
            RunEventRecord(
                event_type="marker_search_failure",
                run_id=run_id,
                transition_reason=ReasonCode.MARKER_SEARCH_FAILED,
                reason_detail="marker_search_failed",
            )
        )
    produced = simulate_run(
        settled,
        run_id=run_id,
        output_dir=output_dir,
        baseline=baseline,
        config=config,
        fix_outputs=runner.fix_outputs,
        capability_notes=notes,
        token_login=preflight.token_login if preflight is not None else None,
        token_scopes=preflight.token_scopes if preflight is not None else (),
        run_events=run_events,
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
