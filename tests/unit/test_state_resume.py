"""§14.1/§14.2 resume: one pure decision function and one store entry point.

Resume was previously derived independently at two call sites and the two drifted, which is
why the decision table is pinned directly here *and* asserted to be reached through
`CandidateStateStore.resume_decision` — the only thing `_publish_live` and
`_prepare_live_candidate` are allowed to consult.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline import __main__ as entrypoint
from pipeline.__main__ import _prepare_live_candidate as prepare_live_candidate
from pipeline.config import Mode, PipelineConfig
from pipeline.github_client import GitHubClient
from pipeline.schemas import Candidate, CandidateState, ReasonCode
from pipeline.state import (
    CandidateStateStore,
    MarkerArtifact,
    MarkerSearchOutcome,
    ResumeAction,
    StatePreservationError,
    decide_resume,
    github_marker_search,
    has_local_artifact,
)
from tests.deadline import within_deadline
from tests.factories import codeql_candidate
from tests.fakes import BASE_SHA, HEAD_SHA, FakeGitHubTransport
from tests.known_defects import local_resume_lookup

ISSUE_URL = "https://github.test/victorciao/superset/issues/1"
PR_URL = "https://github.test/victorciao/superset/pull/2"
ISSUE_ARTIFACT = MarkerArtifact(number=1, url=ISSUE_URL, is_pull_request=False)
PR_ARTIFACT = MarkerArtifact(number=2, url=PR_URL, is_pull_request=True)
COMMENT_URL = "https://github.test/victorciao/superset/issues/1#issuecomment-3"
ARTIFACT_STATES = (
    CandidateState.ISSUE_CREATED,
    CandidateState.PR_CREATED,
    CandidateState.ISSUE_PATCHED,
    CandidateState.COMMENT_CREATED,
)
ARTIFACT_STATE_LINKS = (
    (CandidateState.ISSUE_CREATED, "issue_url"),
    (CandidateState.PR_CREATED, "pr_url"),
    (CandidateState.ISSUE_PATCHED, "issue_url"),
    (CandidateState.COMMENT_CREATED, "comment_url"),
)
PRE_ARTIFACT_STATES = (
    CandidateState.ENUMERATED,
    CandidateState.GATED,
    CandidateState.SCORED,
    CandidateState.DISPATCHING,
    CandidateState.DEFERRED,
)
COMPLETED_STATES = (
    CandidateState.ISSUE_PATCHED,
    CandidateState.COMMENT_CREATED,
    CandidateState.TERMINAL,
)


def persisted(state: CandidateState, **fields: Any) -> Candidate:  # noqa: ANN401
    """One persisted lifecycle row."""
    return codeql_candidate(state=state, **fields)


def link_value(field: str) -> str:
    """The URL a given link field carries."""
    return {"issue_url": ISSUE_URL, "pr_url": PR_URL, "comment_url": COMMENT_URL}[field]


def no_marker(_marker: str) -> MarkerArtifact | None:
    """A marker search that verifies nothing exists on the target repository."""
    return None


def issue_marker(_marker: str) -> MarkerArtifact | None:
    """A marker search that finds one existing tracking issue."""
    return ISSUE_ARTIFACT


def failing_marker(_marker: str) -> MarkerArtifact | None:
    """A marker search whose transport call fails outright."""
    raise RuntimeError("HTTP 422")


def ambiguous_marker(_marker: str) -> MarkerArtifact | None:
    """A marker search whose result is non-unique."""
    raise ValueError("non-unique marker result")


# -- `has_local_artifact` ----------------------------------------------------------------


def test_no_persisted_row_has_no_local_artifact() -> None:
    """§14.1 — absence of state proves nothing exists locally."""
    assert has_local_artifact(None) is False


@pytest.mark.parametrize(("state", "link"), ARTIFACT_STATE_LINKS)
def test_an_artifact_state_with_its_link_proves_a_local_artifact(
    state: CandidateState,
    link: str,
) -> None:
    """§14.1 — the recorded link is the proof; the state only says which link to expect."""
    assert has_local_artifact(persisted(state, **{link: link_value(link)})) is True


@pytest.mark.parametrize("state", ARTIFACT_STATES)
def test_an_artifact_state_without_any_link_proves_nothing(state: CandidateState) -> None:
    """§14.1 — a lifecycle state is not evidence of a remote artifact.

    A SIMULATE row or a run that crashed between stamping the state and recording the URL
    leaves `issue_created` with no link at all; reading that as an existing artifact made a
    later LIVE run skip the write that had never happened.
    """
    assert has_local_artifact(persisted(state)) is False


@pytest.mark.parametrize("state", PRE_ARTIFACT_STATES)
def test_pre_artifact_states_without_urls_prove_nothing(state: CandidateState) -> None:
    """§14.1 — a pre-publication row is not evidence of a remote artifact."""
    assert has_local_artifact(persisted(state)) is False


@pytest.mark.parametrize("field", ["issue_url", "pr_url"])
def test_a_recorded_url_proves_a_local_artifact_whatever_the_state(field: str) -> None:
    """§14.1 — a recorded URL is durable evidence even on an otherwise early row."""
    assert has_local_artifact(persisted(CandidateState.DISPATCHING, **{field: ISSUE_URL})) is True


# -- the decision table ------------------------------------------------------------------


@pytest.mark.parametrize("artifacts_present", [False, True])
def test_unseen_candidate_with_marker_search_available_publishes(artifacts_present: bool) -> None:
    """§14.1 — with a working marker search a fresh candidate proceeds to publication."""
    decision = decide_resume(
        None,
        artifacts_present=artifacts_present,
        marker_search_available=True,
    )

    assert decision.action is ResumeAction.RESUME_AT_STEP
    assert decision.step == "publication"


def test_unseen_candidate_defers_when_marker_search_is_unavailable() -> None:
    """§14.1 — fail closed: an unverifiable first write is deferred, never attempted."""
    decision = decide_resume(None, artifacts_present=False, marker_search_available=False)

    assert decision.action is ResumeAction.DEFER
    assert decision.step is None


def test_unseen_candidate_defers_when_marker_search_is_ambiguous() -> None:
    """§14.1 — an ambiguous marker hit is an orphaned artifact: never write a duplicate."""
    decision = decide_resume(
        None,
        artifacts_present=False,
        marker_search_available=True,
        marker_search_orphaned=True,
    )

    assert decision.action is ResumeAction.DEFER


@pytest.mark.parametrize("state", PRE_ARTIFACT_STATES)
def test_a_pre_artifact_row_defers_on_an_ambiguous_marker(state: CandidateState) -> None:
    """§14.1 — orphaned markers fail closed exactly as an unavailable search does."""
    decision = decide_resume(
        persisted(state),
        artifacts_present=False,
        marker_search_orphaned=True,
    )

    assert decision.action is ResumeAction.DEFER


def test_unseen_candidate_with_a_known_artifact_still_publishes_idempotently() -> None:
    """§14.1 — a found marker with no local row is resumed, so the run can cross-link it."""
    decision = decide_resume(None, artifacts_present=True, marker_search_available=False)

    assert decision.action is ResumeAction.RESUME_AT_STEP


@pytest.mark.parametrize("state", COMPLETED_STATES)
@pytest.mark.parametrize("artifacts_present", [False, True])
def test_a_completed_lifecycle_is_skipped_only_on_artifact_proof(
    state: CandidateState, artifacts_present: bool
) -> None:
    """§14.1 (l.912-916) — skipping needs proof: a link on the row, or a marker hit.

    The pipeline writes the state value itself before any artifact exists, so a crashed or
    simulated run can leave a `terminal` row for a candidate that was never published. Treating
    that value as completion silently drops the routed candidate.
    """
    proven = decide_resume(
        persisted(state, issue_url=ISSUE_URL),
        artifacts_present=artifacts_present,
        marker_search_available=True,
    )
    linkless = decide_resume(
        persisted(state),
        artifacts_present=artifacts_present,
        marker_search_available=True,
    )

    assert proven.action is ResumeAction.SKIP
    assert linkless.action is (
        ResumeAction.SKIP if artifacts_present else ResumeAction.RESUME_AT_STEP
    )
    if not artifacts_present:
        assert linkless.step == "publication"


@pytest.mark.parametrize("state", COMPLETED_STATES)
def test_artifact_proof_resume_cannot_loop(state: CandidateState) -> None:
    """§14.1 (l.912-916) — the resume is bounded by the identity publication persists.

    A linkless completed row resumes once; the publication callback writes the artifact link, and
    every later run skips. Without that second half the candidate would republish every run.
    """
    first = decide_resume(persisted(state), artifacts_present=False)
    after_publication = decide_resume(
        persisted(state, issue_url=ISSUE_URL),
        artifacts_present=False,
    )

    assert first.action is ResumeAction.RESUME_AT_STEP
    assert after_publication.action is ResumeAction.SKIP


def test_converged_with_artifacts_is_skipped_but_without_them_is_resumed() -> None:
    """§14.1 — `converged` means the review finished; publication may still be owed."""
    with_artifacts = decide_resume(
        persisted(CandidateState.CONVERGED, issue_url=ISSUE_URL),
        artifacts_present=True,
    )
    without_artifacts = decide_resume(
        persisted(CandidateState.CONVERGED, issue_url=ISSUE_URL),
        artifacts_present=False,
    )

    assert with_artifacts.action is ResumeAction.SKIP
    assert without_artifacts.action is ResumeAction.RESUME_AT_STEP


@pytest.mark.parametrize(
    ("state", "link"),
    [(CandidateState.ISSUE_CREATED, "issue_url"), (CandidateState.PR_CREATED, "pr_url")],
)
@pytest.mark.parametrize("artifacts_present", [False, True])
@pytest.mark.parametrize("marker_search_available", [False, True])
def test_a_partial_publication_always_resumes_at_publication(
    state: CandidateState,
    link: str,
    artifacts_present: bool,
    marker_search_available: bool,
) -> None:
    """§14.1 — the crash windows resume; a half-published candidate is never abandoned.

    The row carries the link its state implies, which is what proves the partial publication
    happened: resume is unconditional from there, whatever the marker search can see.
    """
    decision = decide_resume(
        persisted(state, **{link: link_value(link)}),
        artifacts_present=artifacts_present,
        marker_search_available=marker_search_available,
    )

    assert decision.action is ResumeAction.RESUME_AT_STEP
    assert decision.step == "publication"


@pytest.mark.parametrize("state", [CandidateState.ISSUE_CREATED, CandidateState.PR_CREATED])
@pytest.mark.parametrize(
    ("artifacts_present", "marker_search_available"),
    [(True, True), (False, False)],
)
def test_a_linkless_artifact_state_defers_instead_of_resuming(
    state: CandidateState,
    artifacts_present: bool,
    marker_search_available: bool,
) -> None:
    """§14.1 — with no link there is no partial publication to resume, so fail closed.

    Either a marker says some other run owns the artifact, or the search cannot say; both
    make a write unverifiable, and the state alone is no longer evidence to write against.
    """
    decision = decide_resume(
        persisted(state),
        artifacts_present=artifacts_present,
        marker_search_available=marker_search_available,
    )

    assert decision.action is ResumeAction.DEFER


@pytest.mark.parametrize("state", PRE_ARTIFACT_STATES)
def test_a_pre_artifact_row_defers_when_a_remote_artifact_already_exists(
    state: CandidateState,
) -> None:
    """§14.1 — a marker hit with no local artifact means another run owns it: defer."""
    decision = decide_resume(persisted(state), artifacts_present=True)

    assert decision.action is ResumeAction.DEFER


@pytest.mark.parametrize("state", PRE_ARTIFACT_STATES)
def test_a_pre_artifact_row_defers_when_marker_search_is_unavailable(
    state: CandidateState,
) -> None:
    """§14.1 — fail closed on an unverifiable first write even with a persisted row."""
    decision = decide_resume(
        persisted(state),
        artifacts_present=False,
        marker_search_available=False,
    )

    assert decision.action is ResumeAction.DEFER


@pytest.mark.parametrize("state", PRE_ARTIFACT_STATES)
def test_a_pre_artifact_row_publishes_when_nothing_exists_yet(state: CandidateState) -> None:
    """§14.1 — a verified-absent artifact is the one case where a first write is allowed."""
    decision = decide_resume(persisted(state), artifacts_present=False)

    assert decision.action is ResumeAction.RESUME_AT_STEP


# -- the store entry point ---------------------------------------------------------------


def store_for(tmp_path: Path, **fields: object) -> CandidateStateStore:
    """A state store over a temporary append-only log."""
    return CandidateStateStore(tmp_path / "candidates.jsonl", **fields)  # type: ignore[arg-type]


def test_resume_decision_of_an_unknown_candidate_publishes(tmp_path: Path) -> None:
    """§14.1 — no row and no marker hit is a clean first publication."""
    store = store_for(tmp_path, marker_search=no_marker)

    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP


def test_a_remote_marker_with_no_local_row_blocks_the_reservation(tmp_path: Path) -> None:
    """§14.1 — a marker in the target repository is a duplicate artifact: no second write.

    The decision resumes so the run can adopt the artifact, and the atomic reservation is what
    refuses; the observable contract is that no durable row and no first write follow.
    """
    store = store_for(tmp_path, marker_search=issue_marker)

    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP
    assert store.marker_artifact("codeql-0") == ISSUE_ARTIFACT
    assert store.existing_artifact("codeql-0") is True
    assert store.append_if_new_artifact(codeql_candidate()) is False
    assert store.rows() == []


def test_resume_decision_defers_when_marker_search_fails(tmp_path: Path) -> None:
    """§14.1 — a *failed* search is unknown, not absent, so no first write is attempted."""

    def failing(_marker: str) -> MarkerArtifact | None:
        raise OSError("search unavailable")

    store = store_for(tmp_path, marker_search=failing)

    assert store.resume_decision("codeql-0").action is ResumeAction.DEFER
    assert store.marker_search_failed is True


def test_resume_decision_defers_when_marker_search_is_ambiguous(tmp_path: Path) -> None:
    """§14.1 — a non-unique search result is orphaned, not absent: defer, never create.

    An ambiguous result on the user's fork means duplicate artifacts already carry this
    candidate's marker; a first write would add a third.
    """

    def ambiguous(_marker: str) -> MarkerArtifact | None:
        raise ValueError("marker search did not return a unique artifact")

    store = store_for(tmp_path, marker_search=ambiguous)

    assert store.resume_decision("codeql-0").action is ResumeAction.DEFER
    assert store.marker_search_orphaned("codeql-0") is True
    assert store.marker_search_unavailable("codeql-0") is False
    assert store.marker_search_failed is False
    assert store.append_if_new_artifact(codeql_candidate()) is False
    assert store.rows() == []


@local_resume_lookup
def test_resume_decision_skips_a_completed_row_without_searching(tmp_path: Path) -> None:
    """§14.1 — a completed row needs no remote lookup; resume is decided locally."""
    searches: list[str] = []

    def counting(marker: str) -> MarkerArtifact | None:
        searches.append(marker)
        return None

    store = store_for(tmp_path, marker_search=counting)
    store.append(persisted(CandidateState.ISSUE_PATCHED, issue_url=ISSUE_URL, pr_url=PR_URL))

    assert store.resume_decision("codeql-0").action is ResumeAction.SKIP
    assert searches == []


@local_resume_lookup
def test_resume_decision_resumes_a_pr_created_row_without_searching(tmp_path: Path) -> None:
    """§14.1 — the `pr_created` crash window resumes from local evidence alone."""
    searches: list[str] = []

    def counting(marker: str) -> MarkerArtifact | None:
        searches.append(marker)
        return ISSUE_ARTIFACT

    store = store_for(tmp_path, marker_search=counting)
    store.append(
        persisted(
            CandidateState.PR_CREATED,
            issue_url=ISSUE_URL,
            issue_number=1,
            pr_url=PR_URL,
            pr_number=2,
        )
    )

    decision = store.resume_decision("codeql-0")

    assert decision.action is ResumeAction.RESUME_AT_STEP
    assert decision.step == "publication"
    assert searches == []


def test_marker_search_is_memoized_per_candidate_per_run(tmp_path: Path) -> None:
    """§14.1 — one marker lookup per candidate per run, not one per consulting call site."""
    searches: list[str] = []

    def counting(marker: str) -> MarkerArtifact | None:
        searches.append(marker)
        return None

    store = store_for(tmp_path, marker_search=counting)

    store.resume_decision("codeql-0")
    store.resume_decision("codeql-0")
    store.existing_artifact("codeql-0")
    store.marker_exists("codeql-0")
    store.resume_decision("codeql-1")

    assert searches == [
        "<!-- devin-remediation-id: codeql-0 -->",
        "<!-- devin-remediation-id: codeql-1 -->",
    ]


def test_a_failed_marker_search_blocks_the_first_durable_reservation(tmp_path: Path) -> None:
    """§14.1 — fail closed: an unverifiable candidate performs no first write at all."""

    def failing(_marker: str) -> MarkerArtifact | None:
        raise OSError("search unavailable")

    store = store_for(tmp_path, marker_search=failing)

    assert store.append_if_new_artifact(codeql_candidate()) is False
    assert store.rows() == []


def test_an_unconfigured_marker_search_never_looked_so_it_fails_closed(tmp_path: Path) -> None:
    """§14.1 — "never looked" is not "proven absent", so no remote write is reserved.

    The distinct outcomes are what keeps the polarity right: `absent` is the one state that
    licences a first write, and for a store that publishes remotely `unconfigured` is as
    unverifiable as a search that raised.
    """
    store = store_for(tmp_path, require_marker_proof=True)
    candidate = codeql_candidate()

    assert store.marker_search_outcome("codeql-0") is MarkerSearchOutcome.UNCONFIGURED
    assert store.marker_search_unavailable("codeql-0") is True
    assert store.marker_search_failed is False
    assert store.resume_decision("codeql-0").action is ResumeAction.DEFER
    assert within_deadline(lambda: store.append_if_new_artifact(candidate)) is False
    assert store.rows() == []


def test_a_store_that_publishes_nothing_needs_no_marker_proof(tmp_path: Path) -> None:
    """§14.1 — SIMULATE writes to no remote, so an unconfigured search deduplicates nothing.

    Demanding proof there would defer every simulated candidate and make the dry run report
    a dedupe outage it could not have had.
    """
    store = store_for(tmp_path)
    candidate = codeql_candidate()

    assert store.marker_search_outcome("codeql-0") is MarkerSearchOutcome.UNCONFIGURED
    assert store.marker_search_unavailable("codeql-0") is False
    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP
    assert within_deadline(lambda: store.append_if_new_artifact(candidate)) is True
    assert [row.candidate_id for row in store.rows()] == [candidate.candidate_id]


@pytest.mark.parametrize(
    ("marker_search", "outcome"),
    [
        (no_marker, MarkerSearchOutcome.ABSENT),
        (issue_marker, MarkerSearchOutcome.FOUND),
    ],
)
def test_each_marker_lookup_records_its_own_outcome(
    tmp_path: Path,
    marker_search: Callable[[str], MarkerArtifact | None],
    outcome: MarkerSearchOutcome,
) -> None:
    """§14.1 — a successful lookup that found nothing is `absent`, never `failed`."""
    store = store_for(tmp_path, marker_search=marker_search)

    assert store.marker_search_outcome("codeql-0") is outcome
    assert store.marker_search_unavailable("codeql-0") is False


def test_a_reservation_completes_when_the_marker_is_proven_absent(tmp_path: Path) -> None:
    """§14.1 — the atomic reservation returns rather than blocking on its own lock."""
    store = store_for(tmp_path, marker_search=no_marker)

    assert within_deadline(lambda: store.append_if_new_artifact(codeql_candidate())) is True
    assert len(store.rows()) == 1


def test_a_reserved_candidate_cannot_be_reserved_twice(tmp_path: Path) -> None:
    """§14.1 — the reservation is atomic: exactly one durable row per first write."""
    store = store_for(tmp_path, marker_search=no_marker)
    candidate = codeql_candidate(state=CandidateState.ISSUE_CREATED, issue_url=ISSUE_URL)

    assert store.append_if_new_artifact(candidate) is True
    assert store.append_if_new_artifact(candidate) is False
    assert len(store.rows()) == 1


def search_response(*items: dict[str, object]) -> dict[str, object]:
    """One GitHub issue-search response body."""
    return {"total_count": len(items), "items": list(items)}


def test_github_marker_search_returns_the_unique_issue_it_found() -> None:
    """§14.1 — the LIVE marker lookup identifies the artifact, not merely its existence."""
    find = github_marker_search(
        lambda _marker: search_response({"number": 7, "html_url": ISSUE_URL})
    )

    assert find("marker") == MarkerArtifact(number=7, url=ISSUE_URL, is_pull_request=False)


def test_github_marker_search_identifies_a_pull_request_as_a_pull_request() -> None:
    """§14.1 — a found PR must be adopted as a PR; adopting it as an issue duplicates it."""
    from_payload = github_marker_search(
        lambda _marker: search_response(
            {"number": 9, "html_url": ISSUE_URL, "pull_request": {"url": PR_URL}}
        )
    )
    from_url = github_marker_search(
        lambda _marker: search_response({"number": 9, "html_url": PR_URL})
    )

    found_from_payload = from_payload("marker")
    found_from_url = from_url("marker")

    assert found_from_payload is not None and found_from_payload.is_pull_request is True
    assert found_from_url is not None and found_from_url.is_pull_request is True


def test_github_marker_search_reports_an_empty_result_as_absent() -> None:
    """§14.1 — a verified-absent marker is the one case allowing a first write."""
    miss = github_marker_search(lambda _marker: search_response())

    assert miss("marker") is None


@pytest.mark.parametrize(
    "payload",
    [
        "unexpected",
        {"total_count": 1},
        {"items": [{"number": 1, "html_url": ISSUE_URL}]},
        {"total_count": 2, "items": [{"number": 1, "html_url": ISSUE_URL}]},
        {
            "total_count": 2,
            "items": [
                {"number": 1, "html_url": ISSUE_URL},
                {"number": 2, "html_url": PR_URL},
            ],
        },
        {"total_count": 1, "items": [{"html_url": ISSUE_URL}]},
        {"total_count": 1, "items": [{"number": 1}]},
        {"total_count": 1, "items": [{"number": 1, "html_url": ""}]},
    ],
)
def test_github_marker_search_refuses_a_non_unique_or_malformed_result(payload: object) -> None:
    """§14.1 — ambiguity is raised, never silently read as absent or as a single hit."""
    find = github_marker_search(lambda _marker: payload)

    with pytest.raises(ValueError):
        find("marker")


# -- both call sites derive resume the same way ------------------------------------------


@pytest.mark.parametrize("function", ["_publish_live", "_prepare_live_candidate"])
def test_both_live_call_sites_resume_through_the_store_only(function: str) -> None:
    """§14.1 — resume is derived once, in the store; neither call site may re-derive it."""
    source = inspect.getsource(getattr(entrypoint, function))

    assert "state_store.resume_decision(candidate.candidate_id)" in source
    assert "decide_resume(" not in source
    assert "marker_search_unavailable" not in source
    assert "existing_artifact" not in source


# -- identical-row append suppression ----------------------------------------------------


def test_an_identical_repeated_row_is_not_appended(tmp_path: Path) -> None:
    """§14.1 — a row byte-identical to the latest one carries no new information.

    A LIVE run wrote three identical `deferred/capability_unavailable` rows for one
    candidate; the log records transitions, not retries of the same transition.
    """
    store = store_for(tmp_path)
    deferred = persisted(CandidateState.DEFERRED, reason=ReasonCode.CAPABILITY_UNAVAILABLE)

    store.append(deferred)
    store.append(deferred)
    store.append(deferred)

    assert len(store.rows()) == 1
    assert store.resume(deferred.candidate_id) == deferred


def test_an_identical_row_written_from_a_reparsed_copy_is_still_suppressed(
    tmp_path: Path,
) -> None:
    """§14.1 — equality is over the serialised fields, not object identity."""
    store = store_for(tmp_path)
    deferred = persisted(CandidateState.DEFERRED, reason=ReasonCode.CAPABILITY_UNAVAILABLE)
    store.append(deferred)
    reread = store_for(tmp_path)
    persisted_row = reread.resume(deferred.candidate_id)
    assert persisted_row is not None

    reread.append(persisted_row)

    assert len(reread.rows()) == 1


@pytest.mark.parametrize(
    "difference",
    [
        {"state": CandidateState.DISPATCHING},
        {"reason": ReasonCode.BUDGET_OVERFLOW},
        {"reason_detail": "marker_search_failed"},
        {"pr_number": 2},
        {"pr_url": PR_URL},
        {"issue_url": ISSUE_URL},
        {"auto_merge_requested": True},
        {"ci_evidence_mode": "github"},
        {"merge_verified": True},
    ],
)
def test_any_differing_field_still_appends(tmp_path: Path, difference: dict[str, Any]) -> None:
    """§14.1 — suppression is exact: one changed field is a new durable row."""
    store = store_for(tmp_path)
    first = persisted(CandidateState.DEFERRED, reason=ReasonCode.CAPABILITY_UNAVAILABLE)
    store.append(first)

    store.append(first.model_copy(update=difference))

    assert len(store.rows()) == 2


def test_suppression_leaves_last_write_wins_resume_intact(tmp_path: Path) -> None:
    """§14.2 — resume still reads the latest row after a suppressed duplicate."""
    store = store_for(tmp_path)
    deferred = persisted(CandidateState.DEFERRED, reason=ReasonCode.CAPABILITY_UNAVAILABLE)
    published = persisted(CandidateState.PR_CREATED, pr_number=2, pr_url=PR_URL)

    store.append(deferred)
    store.append(deferred)
    store.append(published)
    store.append(published)

    assert len(store.rows()) == 2
    assert store.resume(published.candidate_id) == published
    assert store.resume_decision(published.candidate_id).action is ResumeAction.RESUME_AT_STEP


def test_suppression_is_per_candidate(tmp_path: Path) -> None:
    """§14.1 — suppression compares a candidate's own latest row, not the log's last line."""
    store = store_for(tmp_path)
    first = persisted(CandidateState.DEFERRED, candidate_id="codeql-1")
    second = persisted(CandidateState.DEFERRED, candidate_id="codeql-2")

    store.append(first)
    store.append(second)
    store.append(first)
    store.append(second.model_copy(update={"state": CandidateState.DISPATCHING}))

    assert [row.candidate_id for row in store.rows()] == ["codeql-1", "codeql-2", "codeql-2"]


def test_suppression_does_not_weaken_the_artifact_reservation(tmp_path: Path) -> None:
    """§14.1 — `append_if_new_artifact` still refuses a second reservation."""
    store = store_for(tmp_path, marker_search=no_marker)
    candidate = persisted(CandidateState.ISSUE_CREATED, issue_url=ISSUE_URL)

    assert store.append_if_new_artifact(candidate) is True
    assert store.append_if_new_artifact(candidate) is False
    assert store.append_if_new_artifact(candidate.model_copy(update={"pr_url": PR_URL})) is False
    assert len(store.rows()) == 1


# -- §14.1 adopting an artifact the marker search found ----------------------------------


def prepare(
    store: CandidateStateStore,
    candidate: Candidate,
    *,
    transport: FakeGitHubTransport | None = None,
    run_id: str = "run-1",
) -> tuple[Candidate, FakeGitHubTransport]:
    """Run live candidate preparation over a recording transport, and return both."""
    fake = transport or FakeGitHubTransport()
    config = PipelineConfig(
        mode=Mode.LIVE,
        github_token=SecretStr("placeholder-github-token"),
        devin_api_key=SecretStr("placeholder-devin-key"),
    )
    prepared = prepare_live_candidate(
        candidate,
        state_store=store,
        client=GitHubClient(config, transport=fake),
        base_sha=BASE_SHA,
        head_branch="devin",
        run_id=run_id,
    )
    return prepared, fake


def test_a_unique_issue_marker_is_adopted_into_the_durable_row(tmp_path: Path) -> None:
    """§14.1 — an artifact bearing this candidate's marker is this candidate's artifact.

    Adoption is what lets publication resume instead of duplicating: the run that crashed
    before writing its row left the issue behind, and the marker is the only evidence of it.
    """
    store = store_for(tmp_path, marker_search=issue_marker)

    prepared, transport = prepare(store, codeql_candidate())

    adopted = store.resume(prepared.candidate_id)
    assert adopted is not None
    assert adopted.state is CandidateState.ISSUE_CREATED
    assert (adopted.issue_number, adopted.issue_url) == (1, ISSUE_URL)
    assert (adopted.pr_number, adopted.pr_url) == (None, None)
    assert prepared.issue_url == ISSUE_URL
    assert [write.path for write in transport.writes if "/issues" in write.path] == []


def test_a_unique_pull_request_marker_is_adopted_as_a_pull_request(tmp_path: Path) -> None:
    """§14.1 — a found PR adopted as an issue would open a second PR for the same fix."""
    store = store_for(tmp_path, marker_search=lambda _marker: PR_ARTIFACT)

    prepared, transport = prepare(store, codeql_candidate())

    assert prepared.state is CandidateState.PR_CREATED
    assert (prepared.pr_number, prepared.pr_url) == (2, PR_URL)
    assert (prepared.issue_number, prepared.issue_url) == (None, None)
    assert transport.writes == []


def test_an_ambiguous_marker_defers_orphaned_and_creates_nothing(tmp_path: Path) -> None:
    """§14.1 — duplicate markers on the fork mean a create would add a third artifact.

    This is the adversarial case for the duplicate-artifact guard: the search cannot say
    which artifact is this candidate's, so the only safe action is to write nothing at all.
    """

    def ambiguous(_marker: str) -> MarkerArtifact | None:
        raise ValueError("marker search did not return a unique artifact")

    store = store_for(tmp_path, marker_search=ambiguous)

    prepared, transport = prepare(store, codeql_candidate())

    assert prepared.state is CandidateState.DEFERRED
    assert prepared.reason is ReasonCode.ARTIFACT_ORPHANED
    assert prepared.reason_detail is not None
    assert transport.writes == []
    assert [row.state for row in store.rows()] == [CandidateState.DEFERRED]


def test_an_ambiguous_marker_defers_a_candidate_that_never_reached_publication(
    tmp_path: Path,
) -> None:
    """§14.1 — the guard runs before branch creation, so no side effect precedes it."""

    def ambiguous(_marker: str) -> MarkerArtifact | None:
        raise ValueError("marker search did not return a unique artifact")

    store = store_for(tmp_path, marker_search=ambiguous)

    _, transport = prepare(store, codeql_candidate())

    assert [read for read in transport.reads if "/git/ref" in read] == []


# -- the reservation lease ------------------------------------------------------------------


def test_a_live_claim_from_another_run_blocks_the_reservation(tmp_path: Path) -> None:
    """§17 — a reservation is a lease, so a second run must not reserve a claimed candidate."""
    store = store_for(tmp_path, marker_search=no_marker, reservation_lease_s=3600.0)

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is True
    held = store.get("codeql-0")
    assert held is not None
    assert held.state is CandidateState.DISPATCHING
    assert held.reserved_by_run_id == "run-a"
    assert held.reserved_at is not None

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-b") is False
    assert store.reservation_reason == "reservation_held"
    assert len(store.rows()) == 1


def test_a_run_refreshes_its_own_claim_instead_of_blocking_itself(tmp_path: Path) -> None:
    """§17 — the lease guards other runs; a run must never deadlock against its own claim."""
    store = store_for(tmp_path, marker_search=no_marker, reservation_lease_s=3600.0)

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is True
    first = store.get("codeql-0")
    assert first is not None and first.reserved_at is not None

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is True
    refreshed = store.get("codeql-0")
    assert refreshed is not None and refreshed.reserved_at is not None
    assert refreshed.reserved_at >= first.reserved_at
    assert store.reservation_reason is None


def test_an_expired_lease_is_reclaimable_by_a_later_run(tmp_path: Path) -> None:
    """§17 — a crashed run must not strand a candidate forever; the lease has an expiry."""
    store = store_for(tmp_path, marker_search=no_marker, reservation_lease_s=1.0)
    stale = codeql_candidate(
        state=CandidateState.DISPATCHING,
        reserved_at=time.time() - 600.0,
        reserved_by_run_id="run-crashed",
    )
    store.append(stale)

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-b") is True
    reclaimed = store.get("codeql-0")
    assert reclaimed is not None
    assert reclaimed.reserved_by_run_id == "run-b"


def test_a_future_dated_claim_is_treated_as_unexpired(tmp_path: Path) -> None:
    """§17 — clock skew must fail closed: an unreadable lease age blocks, it does not reclaim."""
    store = store_for(tmp_path, marker_search=no_marker, reservation_lease_s=1.0)
    store.append(
        codeql_candidate(
            state=CandidateState.DISPATCHING,
            reserved_at=time.time() + 600.0,
            reserved_by_run_id="run-skewed",
        )
    )

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-b") is False
    assert store.reservation_reason == "reservation_held"


def test_the_absence_proof_is_retaken_inside_the_reservation(tmp_path: Path) -> None:
    """§17 — a marker result cached before the role sessions ran must not be trusted.

    The duplicate-artifact window is exactly the interval between the pre-dispatch lookup and the
    first write, so the reservation re-takes the search under its own lock and refuses the write
    when the artifact appeared in between.
    """
    results: list[MarkerArtifact | None] = [None, ISSUE_ARTIFACT]
    lookups: list[str] = []

    def racing_search(marker: str) -> MarkerArtifact | None:
        lookups.append(marker)
        return results[min(len(lookups), len(results)) - 1]

    store = store_for(tmp_path, marker_search=racing_search)

    assert store.marker_exists("codeql-0") is False
    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is False
    assert len(lookups) == 2
    assert store.rows() == []


@pytest.mark.parametrize(
    ("marker_search", "require_marker_proof", "reason"),
    [
        (failing_marker, False, MarkerSearchOutcome.FAILED.value),
        (ambiguous_marker, False, MarkerSearchOutcome.ORPHANED.value),
        (None, True, MarkerSearchOutcome.UNCONFIGURED.value),
    ],
)
def test_an_unproven_absence_never_reserves(
    tmp_path: Path,
    marker_search: Callable[[str], MarkerArtifact | None] | None,
    require_marker_proof: bool,
    reason: str,
) -> None:
    """§17 — `failed`, `orphaned` and LIVE `unconfigured` all fail closed with their own reason."""
    store = store_for(
        tmp_path,
        marker_search=marker_search,
        require_marker_proof=require_marker_proof,
    )

    assert within_deadline(lambda: store.append_if_new_artifact(codeql_candidate())) is False
    assert store.reservation_reason == reason
    assert store.rows() == []


def test_the_resume_path_re_proves_absence_before_a_second_write(tmp_path: Path) -> None:
    """§17 — the duplicate-artifact window lives on resume, so the claim is re-earned there."""
    lookups: list[str] = []

    def appearing_search(marker: str) -> MarkerArtifact | None:
        lookups.append(marker)
        return ISSUE_ARTIFACT if len(lookups) > 1 else None

    store = store_for(tmp_path, marker_search=appearing_search)
    store.append(codeql_candidate(state=CandidateState.DEFERRED))

    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is True
    assert store.append_if_new_artifact(codeql_candidate(), run_id="run-a") is False
    assert len(lookups) == 2


# -- durable identity ----------------------------------------------------------------------


@pytest.mark.parametrize("field", ["head_sha", "reviewed_head_sha"])
def test_an_append_may_never_discard_a_persisted_head_identity(tmp_path: Path, field: str) -> None:
    """§17 — the reviewed identity is written once and never lost to a later partial row.

    Local CI evidence is a `base..head` claim about a specific commit; a later row that blanked
    the head would leave a published PR body whose evidence range cannot be reconstructed.
    """
    store = store_for(tmp_path)
    identified: dict[str, Any] = {field: HEAD_SHA}
    blanked: dict[str, Any] = {field: None}
    store.append(codeql_candidate(state=CandidateState.PR_CREATED, **identified))

    with pytest.raises(StatePreservationError, match=field):
        store.append(codeql_candidate(state=CandidateState.ISSUE_PATCHED, **blanked))

    persisted = store.get("codeql-0")
    assert persisted is not None
    assert getattr(persisted, field) == HEAD_SHA
