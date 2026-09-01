"""§14.1/§14.2 resume: one pure decision function and one store entry point."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pipeline.http_transport import HttpTransportError
from pipeline.schemas import Candidate, CandidateState, ReasonCode
from pipeline.state import (
    CandidateStateStore,
    MarkerArtifact,
    MarkerSearchOutcome,
    ResumeAction,
    StatePreservationError,
    build_marker_index,
    decide_resume,
    github_marker_search,
    has_local_artifact,
)
from tests.deadline import within_deadline
from tests.factories import codeql_candidate
from tests.known_defects import local_resume_lookup

ISSUE_URL = "https://github.test/victorciao/superset/issues/1"
PR_URL = "https://github.test/victorciao/superset/pull/2"
ISSUE_ARTIFACT = MarkerArtifact(number=1, url=ISSUE_URL, is_pull_request=False)
PR_ARTIFACT = MarkerArtifact(number=2, url=PR_URL, is_pull_request=True)
COMMENT_URL = "https://github.test/victorciao/superset/issues/1#issuecomment-3"
ARTIFACT_STATES = (
    CandidateState.ISSUE_CREATED,
    CandidateState.PR_CREATED,
)
ARTIFACT_STATE_LINKS = (
    (CandidateState.ISSUE_CREATED, "issue_url"),
    (CandidateState.PR_CREATED, "pr_url"),
)
COMPLETED_STATES = (
    CandidateState.AWAITING_HUMAN_MERGE,
    CandidateState.MERGED,
    CandidateState.TERMINAL,
)
PRE_ARTIFACT_STATES = (
    CandidateState.ENUMERATED,
    CandidateState.GATED,
    CandidateState.SCORED,
    CandidateState.DISPATCHING,
    CandidateState.DEFERRED,
)


def persisted(state: CandidateState, **fields: Any) -> Candidate:  # noqa: ANN401
    """One persisted lifecycle row."""
    return codeql_candidate(state=state, **fields)


def link_value(field: str) -> str:
    """The URL a given link field carries."""
    return {"issue_url": ISSUE_URL, "pr_url": PR_URL}[field]


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


def test_marker_index_assembles_issue_markers_across_pages() -> None:
    """§14.1 — one bounded issue search yields all candidate artifacts."""
    pages = {
        1: {
            "items": [
                {
                    "number": 1,
                    "html_url": ISSUE_URL,
                    "body": "<!-- devin-remediation-id: codeql-0 -->",
                },
                {"number": 2, "html_url": ISSUE_URL, "body": "no marker"},
                {
                    "number": 3,
                    "html_url": PR_URL,
                    "pull_request": {"url": PR_URL},
                    "body": "<!-- devin-remediation-id: codeql-pr -->",
                },
                {
                    "number": 5,
                    "html_url": "https://github.test/victorciao/superset/pull/5",
                    "body": "<!-- devin-remediation-id: codeql-pr-url -->",
                },
            ]
            + [{}] * 100,
        },
        2: {
            "items": [
                {
                    "number": 4,
                    "html_url": "https://github.test/victorciao/superset/issues/4",
                    "body": "<!-- devin-remediation-id: skipped-1 -->",
                }
            ]
        },
    }
    seen: list[int] = []

    def search(page: int) -> object:
        seen.append(page)
        return pages[page]

    assert build_marker_index(search) == {
        "codeql-0": ISSUE_ARTIFACT,
        "skipped-1": MarkerArtifact(
            number=4,
            url="https://github.test/victorciao/superset/issues/4",
            is_pull_request=False,
        ),
    }
    assert seen == [1, 2]


def test_marker_index_duplicate_issue_orphans_only_that_candidate(tmp_path: Path) -> None:
    """§14.1 — duplicate issue markers quarantine one candidate, not the run."""
    pages = [
        {
            "items": [
                {
                    "number": 1,
                    "html_url": ISSUE_URL,
                    "body": (
                        "<!-- devin-remediation-id: duplicate -->"
                        "<!-- devin-remediation-id: unique -->"
                    ),
                },
                {
                    "number": 2,
                    "html_url": "https://github.test/victorciao/superset/issues/2",
                    "body": "<!-- devin-remediation-id: duplicate -->",
                },
            ]
        }
    ]
    store = store_for(tmp_path, marker_index_search=lambda _page: pages[0])

    assert store.marker_artifact("duplicate") is None
    assert store.marker_search_orphaned("duplicate") is True
    assert store.marker_artifact("unique") == ISSUE_ARTIFACT
    assert store.marker_search_failed is False


def test_marker_index_is_built_once_for_many_candidate_lookups(tmp_path: Path) -> None:
    """§14.1 — the batched search is cached for the store lifetime."""
    calls: list[int] = []

    def search(page: int) -> object:
        calls.append(page)
        return {
            "items": [
                {
                    "number": 1,
                    "html_url": ISSUE_URL,
                    "body": "<!-- devin-remediation-id: codeql-0 -->",
                }
            ]
        }

    store = store_for(tmp_path, marker_index_search=search)

    store.marker_artifact("codeql-0")
    store.marker_artifact("missing")
    store.marker_artifact("codeql-0")

    assert calls == [1]


def test_marker_index_failure_sets_search_detail(tmp_path: Path) -> None:
    """§14.1 — an unavailable batched search fails closed with its cause."""

    def search(_page: int) -> object:
        raise HttpTransportError("GitHub request failed with HTTP 403", status_code=403)

    store = store_for(tmp_path, marker_index_search=search)

    assert store.marker_artifact("codeql-0") is None
    assert store.marker_search_failed is True
    assert store.marker_search_failure_detail == "HTTP 403: GitHub request failed with HTTP 403"


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


@pytest.mark.parametrize(
    "states",
    [
        [CandidateState.TERMINAL, CandidateState.AWAITING_HUMAN_MERGE],
        [CandidateState.AWAITING_HUMAN_MERGE, CandidateState.TERMINAL],
    ],
)
def test_artifact_backed_settlement_is_preserved_across_arbitrary_appends(
    tmp_path: Path,
    states: list[CandidateState],
) -> None:
    """A settled artifact row cannot be moved back into resumable lifecycle states."""
    store = store_for(tmp_path)
    current = persisted(
        CandidateState.AWAITING_HUMAN_MERGE,
        pr_number=2,
        pr_url=PR_URL,
        issue_number=1,
        issue_url=ISSUE_URL,
    )
    store.append(current)

    for state in states:
        current = current.model_copy(update={"state": state})
        store.append(current)

    for state in (CandidateState.DEFERRED, CandidateState.PR_CREATED):
        with pytest.raises(StatePreservationError, match="attempted transition"):
            store.append(current.model_copy(update={"state": state}))
        latest = store.resume(current.candidate_id)
        assert latest is not None
        assert latest.pr_url == PR_URL
        assert latest.pr_number == 2
        assert latest.issue_url == ISSUE_URL


def test_merged_artifact_row_rejects_weaker_state_and_merge_evidence(
    tmp_path: Path,
) -> None:
    """A merged row cannot regress state or verified merge evidence."""
    store = store_for(tmp_path)
    current = persisted(
        CandidateState.MERGED,
        pr_number=2,
        pr_url=PR_URL,
        issue_number=1,
        issue_url=ISSUE_URL,
        merged_at="2026-09-01T00:00:00Z",
        merge_verified=True,
    )
    store.append(current)

    with pytest.raises(StatePreservationError, match="attempted transition"):
        store.append(current.model_copy(update={"state": CandidateState.TERMINAL}))
    with pytest.raises(StatePreservationError, match="merge_verified"):
        store.append(current.model_copy(update={"merge_verified": False}))


# -- durable identity ----------------------------------------------------------------------
