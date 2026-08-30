"""§14.1 end-to-end publication over a fake transport: the path SIMULATE cannot reach.

Four of the six majors in `docs/reviews/2026-08-29-iteration-4.md` lived in `_publish_live`, and
resume from the `pr_created` crash window has been the headline finding for five iterations, so
these tests drive the real entrypoint function — not helpers — and assert on the durable rows and
the recorded write order that a human reviewer would check on the target repository.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline import __main__ as entrypoint
from pipeline.config import CiEvidenceMode, IssueSink, Mode, PipelineConfig
from pipeline.http_transport import HttpTransportError
from pipeline.schemas import Action, Candidate, CandidateState, ReasonCode, RunEventRecord, Tier
from pipeline.state import CandidateStateStore, MarkerArtifact
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR
from tests.factories import codeql_candidate
from tests.fakes import (
    BASE_SHA,
    HEAD_SHA,
    MULTIWORD_SIGNED_COMMIT,
    FakeGitHubTransport,
    TransportInterrupted,
    WriteRecord,
    all_contexts,
)

PLANNER = {"criteria": [{"id": "AC-1", "statement": "Bound the range to the collection length."}]}
REVIEWER = {
    "tests": [
        {
            "path": "tests/unit_tests/mcp_service/test_add_chart.py",
            "nodeid": "tests/unit_tests/mcp_service/test_add_chart.py::test_range_is_bounded",
            "criterion_id": "AC-1",
        }
    ],
    "commands_run": ["pytest tests/unit_tests/mcp_service/test_add_chart.py"],
    "red_baseline": {"status": "valid", "signature": "AssertionError: index out of range"},
    "green_result": {"status": "pass"},
}
IMPLEMENTER = {
    "commands_run": ["pytest tests/unit_tests/mcp_service/test_add_chart.py"],
    "committed_diff": "diff --git a/superset/x.py b/superset/x.py\n",
}
ISSUES_PATH = "/repos/victorciao/superset/issues"
PULLS_PATH = "/repos/victorciao/superset/pulls"
ISSUE_PATCH = f"{ISSUES_PATH}/1"
PR_BODY_PATCH = f"{PULLS_PATH}/2"
PR_LABELS_PATH = f"{ISSUES_PATH}/2/labels"
AUTO_MERGE_PATH = f"{PULLS_PATH}/2/auto-merge"
PUBLICATION_ORDER = [ISSUES_PATH, PULLS_PATH, PR_BODY_PATCH, ISSUE_PATCH]
ARTIFACT_PATHS = frozenset(PUBLICATION_ORDER)


def live_config(**fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A LIVE config pointed at the shipped templates and rubrics, with a 1s CI budget."""
    return PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-token"),
        devin_api_key=SecretStr("placeholder-key"),
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        **{"ci_wait_timeout_s": 1, **fields},
    )


def publishable(**fields: Any) -> Candidate:  # noqa: ANN401
    """A reviewed HIGH-tier candidate whose branch has already been prepared."""
    return codeql_candidate(
        tier=fields.pop("tier", Tier.HIGH),
        action=fields.pop("action", Action.OPEN_PR),
        score=fields.pop("score", 128.0),
        gate_passed=fields.pop("gate_passed", True),
        head_branch=fields.pop("head_branch", "devin/codeql-0"),
        base_sha=fields.pop("base_sha", BASE_SHA),
        test_added=fields.pop("test_added", True),
        **fields,
    )


class Harness:
    """Drive `_publish_live` over a fake transport and inspect what it made durable."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        config: PipelineConfig | None = None,
        transport: FakeGitHubTransport | None = None,
        marker_search: Any = None,  # noqa: ANN401
    ) -> None:
        self.config = config or live_config()
        self.transport = transport or FakeGitHubTransport()
        self.output_dir = tmp_path / "out"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.store = CandidateStateStore(
            tmp_path / "candidates.jsonl",
            marker_search=marker_search or (lambda _marker: None),
        )
        self.ci_mode_events: list[RunEventRecord] = []
        self.ci_mode_state = [CiEvidenceMode(self.config.ci_evidence_mode.value)]
        self.ci_elapsed_s = [0.0]

    def publish(self, *candidates: Candidate) -> list[Candidate]:
        """Run one publication pass over the given candidates."""
        return entrypoint._publish_live(
            list(candidates),
            config=self.config,
            output_dir=self.output_dir,
            repo_path=Path.cwd(),
            planner_outputs={candidate.candidate_id: PLANNER for candidate in candidates},
            implementer_outputs={candidate.candidate_id: IMPLEMENTER for candidate in candidates},
            reviewer_outputs={candidate.candidate_id: REVIEWER for candidate in candidates},
            base_sha=BASE_SHA,
            head_branch="devin",
            base_branch="master",
            transport=self.transport,  # type: ignore[arg-type]
            state_store=self.store,
            ci_mode_events=self.ci_mode_events,
            ci_mode_state=self.ci_mode_state,
            ci_elapsed_s=self.ci_elapsed_s,
            run_id="run-1",
        )

    @property
    def artifact_writes(self) -> list[str]:
        """Write paths limited to issue and pull-request artifact writes, in order."""
        return [
            path
            for path in self.transport.write_paths
            if path in {ISSUES_PATH, PULLS_PATH}
            or re.fullmatch(rf"({re.escape(ISSUES_PATH)}|{re.escape(PULLS_PATH)})/\d+", path)
        ]

    def states(self, candidate_id: str = "codeql-0") -> list[CandidateState]:
        """Every durable lifecycle state written for one candidate, in write order."""
        return [row.state for row in self.store.rows() if row.candidate_id == candidate_id]

    def latest(self, candidate_id: str = "codeql-0") -> Candidate:
        """The last-write-wins row for one candidate."""
        row = self.store.resume(candidate_id)
        assert row is not None
        return row


def green_transport(**fields: Any) -> FakeGitHubTransport:  # noqa: ANN401
    """A transport whose PR reports all 13 required contexts as `success`."""
    return FakeGitHubTransport(**{"context_states": all_contexts(), **fields})


# -- §14.1 the mandated write order ------------------------------------------------------


def test_publication_writes_issue_then_pr_then_pr_body_then_issue_patch(tmp_path: Path) -> None:
    """§14.1 — issue → PR → PR-body patch → issue patch, in that order, once each."""
    harness = Harness(tmp_path, transport=green_transport())

    published = harness.publish(publishable())

    assert harness.artifact_writes == PUBLICATION_ORDER
    assert published[0].state is CandidateState.ISSUE_PATCHED
    assert published[0].reason is None
    assert published[0].issue_number == 1
    assert published[0].pr_number == 2


def test_each_completed_write_lands_exactly_one_durable_row(tmp_path: Path) -> None:
    """§14.1 — one append per completed remote write, and no duplicate final row."""
    harness = Harness(tmp_path, transport=green_transport())

    harness.publish(publishable())

    states = harness.states()
    assert states[:4] == [
        CandidateState.DISPATCHING,
        CandidateState.ISSUE_CREATED,
        CandidateState.PR_CREATED,
        CandidateState.ISSUE_PATCHED,
    ]
    assert states.count(CandidateState.ISSUE_CREATED) == 1
    assert states.count(CandidateState.PR_CREATED) == 1
    rows = harness.store.rows()
    assert all(row.issue_number == 1 for row in rows[1:])
    assert all(row.pr_number == 2 for row in rows[2:])


def test_the_issue_cross_link_survives_the_pr_body_patch(tmp_path: Path) -> None:
    """§14.1 — `Closes #<issue>` is written by the PR and preserved by the body patch."""
    harness = Harness(tmp_path, transport=green_transport())

    harness.publish(publishable())

    created = str(harness.transport.payload_for(PULLS_PATH)["body"])
    patched = str(harness.transport.payload_for(PR_BODY_PATCH)["body"])
    assert "Closes #1" in created
    assert "Closes #1" in patched
    assert harness.latest().pr_url is not None
    assert str(harness.transport.payload_for(ISSUE_PATCH)["body"]).count("PR: ") == 1


def test_the_candidate_marker_is_written_into_the_issue(tmp_path: Path) -> None:
    """§14.1 — the stable marker is what a later run's idempotency search finds."""
    harness = Harness(tmp_path, transport=green_transport())

    harness.publish(publishable())

    assert "<!-- devin-remediation-id: codeql-0 -->" in str(
        harness.transport.payload_for(ISSUES_PATH)["body"]
    )


def test_a_second_run_over_a_completed_candidate_writes_nothing(tmp_path: Path) -> None:
    """§14.1 — idempotency: a completed candidate is skipped, not republished."""
    harness = Harness(tmp_path, transport=green_transport())
    harness.publish(publishable())
    writes_after_first_run = len(harness.transport.writes)
    rows_after_first_run = len(harness.store.rows())

    published = harness.publish(publishable())

    assert len(harness.transport.writes) == writes_after_first_run
    assert len(harness.store.rows()) == rows_after_first_run
    assert published[0].state is CandidateState.ISSUE_PATCHED


# -- §14.1 resume from every lifecycle stage ---------------------------------------------


@pytest.mark.parametrize(
    ("state", "persisted_fields", "expected_kinds"),
    [
        pytest.param(
            CandidateState.DISPATCHING,
            {},
            ["issue", "pr", "pr_body", "issue_patch"],
        ),
        (
            CandidateState.DEFERRED,
            {"issue_number": 1, "issue_url": "https://github.test/issues/1"},
            ["pr", "pr_body", "issue_patch"],
        ),
        (
            CandidateState.ISSUE_CREATED,
            {"issue_number": 1, "issue_url": "https://github.test/issues/1"},
            ["pr", "pr_body", "issue_patch"],
        ),
        (
            CandidateState.PR_CREATED,
            {
                "issue_number": 1,
                "issue_url": "https://github.test/issues/1",
                "pr_number": 2,
                "pr_url": "https://github.test/pull/2",
                "head_sha": HEAD_SHA,
            },
            ["pr_body", "issue_patch"],
        ),
    ],
)
def test_resume_completes_the_remaining_writes_only(
    tmp_path: Path,
    state: CandidateState,
    persisted_fields: Mapping[str, Any],
    expected_kinds: list[str],
) -> None:
    """§14.1 — resume from a persisted row performs exactly the writes still owed."""
    harness = Harness(tmp_path, transport=green_transport())
    harness.store.append(publishable(state=state, **dict(persisted_fields)))

    published = harness.publish(publishable())

    issue_number, pr_number = published[0].issue_number, published[0].pr_number
    paths = {
        "issue": ISSUES_PATH,
        "pr": PULLS_PATH,
        "pr_body": f"{PULLS_PATH}/{pr_number}",
        "issue_patch": f"{ISSUES_PATH}/{issue_number}",
    }
    assert harness.artifact_writes == [paths[kind] for kind in expected_kinds]
    assert published[0].state is CandidateState.ISSUE_PATCHED
    assert published[0].reason is None


def test_resume_from_pr_created_keeps_artifact_identity_and_opens_nothing_new(
    tmp_path: Path,
) -> None:
    """§14.1 — the `pr_created` crash window: no second issue, no second PR, identity intact.

    A fresh enumeration produces a candidate with no artifact fields, so publication must take
    `issue_number`/`pr_number`/`pr_url`/`head_sha` from the persisted row alone.
    """
    harness = Harness(tmp_path, transport=green_transport())
    harness.store.append(
        publishable(
            state=CandidateState.PR_CREATED,
            issue_number=1,
            issue_url="https://github.test/issues/1",
            pr_number=2,
            pr_url="https://github.test/pull/2",
            head_sha=HEAD_SHA,
        )
    )

    published = harness.publish(publishable())

    assert ISSUES_PATH not in harness.transport.write_paths
    assert PULLS_PATH not in harness.transport.write_paths
    assert harness.artifact_writes == [PR_BODY_PATCH, ISSUE_PATCH]
    assert published[0].state is CandidateState.ISSUE_PATCHED
    assert published[0].reason is None
    assert (published[0].issue_number, published[0].pr_number) == (1, 2)
    assert published[0].pr_url == "https://github.test/pull/2"
    assert published[0].head_sha == HEAD_SHA
    assert published[0].issue_url == "https://github.test/issues/1"


def test_a_crash_between_the_pr_and_the_issue_patch_is_resumable(tmp_path: Path) -> None:
    """§14.1 — a process death after the PR write leaves durable state a later run completes."""
    crashing = green_transport()

    def die_on_the_body_patch(record: WriteRecord) -> None:
        if record.method == "patch":
            raise TransportInterrupted(record.path)

    crashing.before_write = die_on_the_body_patch
    harness = Harness(tmp_path, transport=crashing)

    with pytest.raises(TransportInterrupted):
        harness.publish(publishable())

    assert harness.states() == [
        CandidateState.DISPATCHING,
        CandidateState.ISSUE_CREATED,
        CandidateState.PR_CREATED,
    ]
    interrupted = harness.latest()
    assert (interrupted.issue_number, interrupted.pr_number) == (1, 2)

    resumed = Harness(tmp_path, transport=green_transport())
    resumed.store = harness.store
    published = resumed.publish(publishable())

    assert resumed.artifact_writes == [PR_BODY_PATCH, ISSUE_PATCH]
    assert published[0].state is CandidateState.ISSUE_PATCHED
    assert published[0].issue_number == 1


def test_a_deferred_row_from_an_earlier_run_is_retried(tmp_path: Path) -> None:
    """§9.2 — a deferral is not terminal: the next run re-attempts the candidate."""
    harness = Harness(tmp_path, transport=green_transport())
    harness.store.append(
        publishable(state=CandidateState.DEFERRED, reason=ReasonCode.CAPABILITY_UNAVAILABLE)
    )

    published = harness.publish(publishable())

    assert harness.artifact_writes == PUBLICATION_ORDER
    assert published[0].state is CandidateState.ISSUE_PATCHED


def test_a_marker_hit_without_local_state_defers_instead_of_duplicating(tmp_path: Path) -> None:
    """§14.1 — an artifact found in the target repository is never published a second time."""
    harness = Harness(
        tmp_path,
        transport=green_transport(marker_hits=1),
        marker_search=lambda _marker: MarkerArtifact(
            number=1,
            url="https://example.invalid/issues/1",
            is_pull_request=False,
        ),
    )

    published = harness.publish(publishable())

    assert harness.transport.writes == []
    assert published[0].state is CandidateState.DEFERRED
    assert published[0].reason is ReasonCode.CAPABILITY_UNAVAILABLE


def test_an_unavailable_marker_search_defers_before_the_first_write(tmp_path: Path) -> None:
    """§14.1 — fail closed: an unverifiable candidate performs no first remote write."""

    def failing(_marker: str) -> MarkerArtifact | None:
        raise OSError("search unavailable")

    harness = Harness(tmp_path, transport=green_transport(), marker_search=failing)

    published = harness.publish(publishable())

    assert harness.transport.writes == []
    assert published[0].state is CandidateState.DEFERRED
    assert published[0].reason is ReasonCode.CAPABILITY_UNAVAILABLE


# -- §11 merged and closed classification ------------------------------------------------


def test_an_externally_merged_pull_request_is_unverified_and_cross_linked(tmp_path: Path) -> None:
    """§11 — a merge the pipeline did not request is terminal, unverified, and cross-linked."""
    transport = green_transport(pr_merged_at="2026-08-29T12:00:00Z", pr_state="closed")
    harness = Harness(tmp_path, transport=transport)
    harness.store.append(
        publishable(
            state=CandidateState.PR_CREATED,
            issue_number=1,
            issue_url="https://github.test/issues/1",
            pr_number=2,
            pr_url="https://github.test/pull/2",
        )
    )

    published = harness.publish(publishable())

    assert published[0].state is CandidateState.TERMINAL
    assert published[0].reason is ReasonCode.MERGED_EXTERNALLY_UNVERIFIED
    assert published[0].merge_verified is False
    assert published[0].merged_at == "2026-08-29T12:00:00Z"
    assert ISSUE_PATCH in harness.transport.write_paths


@pytest.mark.parametrize(
    "persisted_fields",
    [
        {"auto_merge_requested": True},
        {"auto_merge_requested": False},
        {"auto_merge_requested": True, "reason": ReasonCode.CI_EVIDENCE_UNAVAILABLE},
        {"auto_merge_requested": True, "pr_number": 99},
    ],
)
def test_a_merge_without_the_full_verification_conjunction_is_external(
    tmp_path: Path,
    persisted_fields: Mapping[str, Any],
) -> None:
    """§11 — anything short of the four-part conjunction is external and unverified."""
    transport = green_transport(pr_merged_at="2026-08-29T12:00:00Z", pr_state="closed")
    harness = Harness(tmp_path, transport=transport)
    harness.store.append(
        publishable(
            state=CandidateState.PR_CREATED,
            issue_number=1,
            issue_url="https://github.test/issues/1",
            pr_url="https://github.test/pull/2",
            **{"pr_number": 2, **dict(persisted_fields)},
        )
    )

    published = harness.publish(publishable())

    assert published[0].state is CandidateState.TERMINAL
    assert published[0].merge_verified is False
    assert published[0].reason is ReasonCode.MERGED_EXTERNALLY_UNVERIFIED


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan §11: a merge the pipeline requested and later discovers must count as "
        "`merged_clean` (`merge_verified`). The verification conjunction requires the persisted "
        "state to be `issue_patched`, but `resume_decision` SKIPs exactly that state before "
        "publication can rediscover the merge, so `merge_verified` is unreachable and "
        "`merge_rate` can never leave 0."
    ),
)
def test_a_merge_discovered_after_a_requested_auto_merge_is_pipeline_verified(
    tmp_path: Path,
) -> None:
    """§11 — the run that discovers a merge it requested records it as verified."""
    transport = green_transport(pr_merged_at="2026-08-29T12:00:00Z", pr_state="closed")
    harness = Harness(tmp_path, transport=transport)
    harness.store.append(
        publishable(
            state=CandidateState.ISSUE_PATCHED,
            issue_number=1,
            issue_url="https://github.test/issues/1",
            pr_number=2,
            pr_url="https://github.test/pull/2",
            auto_merge_requested=True,
        )
    )

    published = harness.publish(publishable())

    assert published[0].state is CandidateState.TERMINAL
    assert published[0].merge_verified is True
    assert published[0].reason is None


def test_a_closed_unmerged_pull_request_becomes_human_review(tmp_path: Path) -> None:
    """§11 — a closed, unmerged PR is terminal `closed_pull_request` for a human."""
    transport = green_transport(
        pr_state="closed",
        create_pr_error=HttpTransportError("already exists", status_code=422),
        existing_pull_requests=[
            {
                "number": 2,
                "html_url": "https://github.test/pull/2",
                "state": "closed",
                "merged_at": None,
            }
        ],
    )
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(publishable())

    assert published[0].state is CandidateState.TERMINAL
    assert published[0].reason is ReasonCode.CLOSED_PULL_REQUEST
    assert published[0].action is Action.HUMAN_REVIEW


# -- §10.1 CI evidence, the run-scoped upgrade, and auto-merge ----------------------------


def test_a_run_that_upgrades_evidence_emits_exactly_one_transition_event(tmp_path: Path) -> None:
    """§10.1 — the evidence mode is run-scoped: one Layer 1 event, not one per candidate."""
    harness = Harness(tmp_path, transport=green_transport())

    harness.publish(
        publishable(candidate_id="codeql-0", head_branch="devin/codeql-0"),
        publishable(candidate_id="codeql-1", head_branch="devin/codeql-1"),
    )

    assert [event.event_type for event in harness.ci_mode_events] == ["ci_mode_transition"]
    event = harness.ci_mode_events[0]
    assert (event.mode_from, event.mode_to) == ("local", "github")
    assert harness.ci_mode_state[0] is CiEvidenceMode.GITHUB


def test_the_resolved_evidence_mode_is_stamped_on_each_candidate_row(tmp_path: Path) -> None:
    """§10.1 — every published row records the evidence its verification actually had."""
    harness = Harness(tmp_path, transport=green_transport())

    published = harness.publish(publishable())

    assert published[0].ci_evidence_mode == "github"


def test_a_held_fork_workflow_records_awaiting_workflow_approval(tmp_path: Path) -> None:
    """§10.1 — a held workflow is `ci_evidence_unavailable: awaiting_workflow_approval`."""
    transport = green_transport(
        context_states={**all_contexts(), "unit-tests-required": "action_required"}
    )
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(publishable())

    assert published[0].reason is ReasonCode.CI_EVIDENCE_UNAVAILABLE
    assert published[0].reason_detail == "awaiting_workflow_approval"
    assert published[0].auto_merge_eligible is not True
    assert AUTO_MERGE_PATH not in harness.transport.write_paths


def test_a_ci_failure_labels_the_candidate_for_human_review(tmp_path: Path) -> None:
    """§10.1 — recorded CI trouble adds `needs-human-review` and blocks auto-merge."""
    transport = green_transport(
        context_states={**all_contexts(), "test-sqlite": "failure"},
    )
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(publishable())

    assert "needs-human-review" in harness.transport.labels_for(2)
    assert published[0].reason is ReasonCode.CI_CHECK_FAILED
    assert AUTO_MERGE_PATH not in harness.transport.write_paths


def test_a_missing_signoff_trailer_blocks_auto_merge(tmp_path: Path) -> None:
    """§9.2 — DCO evidence is read from the actual commits, never assumed."""
    transport = green_transport(commit_messages=["fix: bound the range\n"])
    harness = Harness(
        tmp_path,
        config=live_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
        transport=transport,
    )

    published = harness.publish(publishable())

    assert published[0].reason is ReasonCode.DCO_TRAILER_MISSING
    assert AUTO_MERGE_PATH not in harness.transport.write_paths


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan §9.2: every commit is made with `git commit -s`, whose trailer carries the "
        "configured `user.name` verbatim. The DCO trailer pattern accepts only a single-token "
        "name before the address, so a real multi-word sign-off is read as missing and the "
        "candidate is deferred `dco_trailer_missing`."
    ),
)
def test_a_multiword_signoff_trailer_is_accepted(tmp_path: Path) -> None:
    """§9.2 — `Signed-off-by: Devin Remediation <devin@...>` is a valid DCO trailer."""
    harness = Harness(
        tmp_path,
        transport=green_transport(commit_messages=[MULTIWORD_SIGNED_COMMIT]),
    )

    published = harness.publish(publishable())

    assert published[0].reason is None


def test_auto_merge_is_requested_only_on_the_full_conjunction(tmp_path: Path) -> None:
    """§13 — github evidence, 13 green contexts, DCO, test evidence, and both knobs on."""
    harness = Harness(
        tmp_path,
        config=live_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
        transport=green_transport(),
    )

    published = harness.publish(publishable(auto_merge_eligible=True))

    assert AUTO_MERGE_PATH in harness.transport.write_paths
    assert published[0].auto_merge_requested is True
    assert published[0].state is CandidateState.ISSUE_PATCHED


def test_a_requested_auto_merge_is_not_a_verified_merge_in_the_same_run(tmp_path: Path) -> None:
    """§11 — the merge is reconciled by a later run; this run claims nothing."""
    harness = Harness(
        tmp_path,
        config=live_config(ci_evidence_mode=CiEvidenceMode.GITHUB, auto_merge_enabled=True),
        transport=green_transport(),
    )

    published = harness.publish(publishable(auto_merge_eligible=True))

    assert published[0].merge_verified is False
    assert published[0].merged_at is None
    assert published[0].state is not CandidateState.TERMINAL


@pytest.mark.parametrize("state", ["queued", "pending", "failure", "skipped", "neutral"])
def test_local_evidence_never_reaches_auto_merge(tmp_path: Path, state: str) -> None:
    """§13 — without 13 literal `success` contexts nothing may request auto-merge."""
    harness = Harness(
        tmp_path,
        config=live_config(
            ci_evidence_mode=CiEvidenceMode.GITHUB,
            auto_merge_enabled=True,
            ci_wait_timeout_s=1,
        ),
        transport=green_transport(context_states={**all_contexts(), "unit-tests-required": state}),
    )

    published = harness.publish(publishable(auto_merge_eligible=True))

    assert AUTO_MERGE_PATH not in harness.transport.write_paths
    assert published[0].auto_merge_requested is False


def test_local_ci_evidence_labels_every_candidate_for_human_review(tmp_path: Path) -> None:
    """§10.1 — under `local` evidence a human must review, so the label is mandatory."""
    harness = Harness(tmp_path, transport=FakeGitHubTransport(context_states={}))

    harness.publish(publishable())

    assert "needs-human-review" in harness.transport.labels_for(2)
    assert AUTO_MERGE_PATH not in harness.transport.write_paths


# -- §14.1 containment and the degraded sink ---------------------------------------------


def test_one_candidate_failing_does_not_stop_the_others(tmp_path: Path) -> None:
    """§14.1 — publication failures are contained per candidate; the run continues."""
    transport = green_transport()

    def fail_the_first_pr(record: WriteRecord) -> None:
        if record.path == PULLS_PATH and record.payload.get("head") == "devin/codeql-0":
            raise OSError("transport unavailable")

    transport.before_write = fail_the_first_pr
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(
        publishable(candidate_id="codeql-0", head_branch="devin/codeql-0"),
        publishable(candidate_id="codeql-1", head_branch="devin/codeql-1"),
    )

    assert published[0].state is CandidateState.DEFERRED
    assert published[0].reason is ReasonCode.CAPABILITY_UNAVAILABLE
    assert published[0].auto_merge_eligible is False
    assert published[1].state is CandidateState.ISSUE_PATCHED


def test_a_deferred_candidate_keeps_its_artifact_identity(tmp_path: Path) -> None:
    """§14.1 — a deferral row may never discard the artifacts already created."""
    transport = green_transport()

    def fail_the_body_patch(record: WriteRecord) -> None:
        if record.path == PR_BODY_PATCH:
            raise OSError("transport unavailable")

    transport.before_write = fail_the_body_patch
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(publishable())

    assert published[0].state is CandidateState.DEFERRED
    assert (published[0].issue_number, published[0].pr_number) == (1, 2)
    assert published[0].pr_url is not None
    assert harness.latest().pr_number == 2


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan §14.1: a deferral row keeps full artifact identity. `after_pr_created` rebuilds the "
        "row from the freshly enumerated candidate and records `issue_number` without "
        "`issue_url` or the observed `head_sha`, so a candidate deferred after the PR write loses "
        "the issue URL that `has_local_artifact` and the manager-facing report read."
    ),
)
def test_a_deferral_after_the_pr_write_keeps_the_issue_url_and_head_sha(tmp_path: Path) -> None:
    """§14.1 — artifact identity on a deferral row includes the issue URL and head SHA."""
    transport = green_transport()

    def fail_the_body_patch(record: WriteRecord) -> None:
        if record.path == PR_BODY_PATCH:
            raise OSError("transport unavailable")

    transport.before_write = fail_the_body_patch
    harness = Harness(tmp_path, transport=transport)

    published = harness.publish(publishable())

    assert published[0].issue_url is not None
    assert published[0].head_sha == HEAD_SHA


@pytest.mark.xfail(
    strict=True,
    reason=(
        "plan §14.1: the degraded `pr_comment` sink's tracking comment is the issue surrogate, so "
        "its URL must persist. The final publication row overwrites `comment_url` with the "
        "enumerated candidate's `None`, discarding the URL the `after_comment_created` row held."
    ),
)
def test_the_degraded_sink_keeps_the_comment_url_on_the_final_row(tmp_path: Path) -> None:
    """§14.1 — the comment URL survives as the candidate's durable cross-link."""
    harness = Harness(
        tmp_path,
        config=live_config(has_issues=False, issue_sink=IssueSink.PR_COMMENT),
        transport=green_transport(),
    )

    published = harness.publish(publishable())

    assert published[0].comment_url is not None
    assert harness.latest().comment_url is not None


def test_the_degraded_sink_publishes_a_pr_and_one_comment(tmp_path: Path) -> None:
    """§14.1 — with issues unavailable the `pr_comment` sink carries the tracking body."""
    harness = Harness(
        tmp_path,
        config=live_config(has_issues=False, issue_sink=IssueSink.PR_COMMENT),
        transport=green_transport(),
    )

    published = harness.publish(publishable())

    assert harness.artifact_writes == [PULLS_PATH]
    assert f"{ISSUES_PATH}/1/comments" in harness.transport.write_paths
    assert published[0].state is CandidateState.COMMENT_CREATED
    assert published[0].artifact_degraded is True
    assert published[0].pr_number == 1
    assert ISSUES_PATH not in harness.transport.write_paths
    comment_body = str(harness.transport.payload_for(f"{ISSUES_PATH}/1/comments")["body"])
    assert "<!-- devin-remediation-id: codeql-0 -->" in comment_body


def test_an_issue_only_candidate_never_opens_a_pull_request(tmp_path: Path) -> None:
    """§6 — a MEDIUM-tier candidate is tracked by an issue alone."""
    harness = Harness(tmp_path, transport=green_transport())

    published = harness.publish(publishable(tier=Tier.MEDIUM, action=Action.OPEN_ISSUE))

    assert harness.artifact_writes == [ISSUES_PATH]
    assert published[0].state is CandidateState.ISSUE_CREATED


def test_log_only_candidates_are_passed_through_untouched(tmp_path: Path) -> None:
    """§6 — LOW-tier candidates are logged, and publication writes nothing for them."""
    harness = Harness(tmp_path, transport=green_transport())

    published = harness.publish(publishable(tier=Tier.LOW, action=Action.LOG_ONLY))

    assert harness.transport.writes == []
    assert harness.store.rows() == []
    assert published[0].action is Action.LOG_ONLY
