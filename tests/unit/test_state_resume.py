"""§14.1/§14.2 resume: one pure decision function and one store entry point.

Resume was previously derived independently at two call sites and the two drifted, which is
why the decision table is pinned directly here *and* asserted to be reached through
`CandidateStateStore.resume_decision` — the only thing `_publish_live` and
`_prepare_live_candidate` are allowed to consult.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from pipeline import __main__ as entrypoint
from pipeline.schemas import Candidate, CandidateState
from pipeline.state import (
    CandidateStateStore,
    ResumeAction,
    decide_resume,
    github_marker_search,
    has_local_artifact,
)
from tests.factories import codeql_candidate

ISSUE_URL = "https://github.test/victorciao/superset/issues/1"
PR_URL = "https://github.test/victorciao/superset/pull/2"
ARTIFACT_STATES = (
    CandidateState.ISSUE_CREATED,
    CandidateState.PR_CREATED,
    CandidateState.ISSUE_PATCHED,
    CandidateState.COMMENT_CREATED,
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


# -- `has_local_artifact` ----------------------------------------------------------------


def test_no_persisted_row_has_no_local_artifact() -> None:
    """§14.1 — absence of state proves nothing exists locally."""
    assert has_local_artifact(None) is False


@pytest.mark.parametrize("state", ARTIFACT_STATES)
def test_artifact_states_prove_a_local_artifact(state: CandidateState) -> None:
    """§14.1 — the four artifact-bearing states each prove a durable write happened."""
    assert has_local_artifact(persisted(state)) is True


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


def test_unseen_candidate_with_a_known_artifact_still_publishes_idempotently() -> None:
    """§14.1 — a found marker with no local row is resumed, so the run can cross-link it."""
    decision = decide_resume(None, artifacts_present=True, marker_search_available=False)

    assert decision.action is ResumeAction.RESUME_AT_STEP


@pytest.mark.parametrize("state", COMPLETED_STATES)
@pytest.mark.parametrize("artifacts_present", [False, True])
def test_completed_lifecycles_are_skipped(state: CandidateState, artifacts_present: bool) -> None:
    """§14.1 — a completed candidate is never republished, whatever the marker says."""
    decision = decide_resume(
        persisted(state),
        artifacts_present=artifacts_present,
        marker_search_available=True,
    )

    assert decision.action is ResumeAction.SKIP


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


@pytest.mark.parametrize("state", [CandidateState.ISSUE_CREATED, CandidateState.PR_CREATED])
@pytest.mark.parametrize("artifacts_present", [False, True])
@pytest.mark.parametrize("marker_search_available", [False, True])
def test_a_partial_publication_always_resumes_at_publication(
    state: CandidateState,
    artifacts_present: bool,
    marker_search_available: bool,
) -> None:
    """§14.1 — the crash windows resume; a half-published candidate is never abandoned."""
    decision = decide_resume(
        persisted(state),
        artifacts_present=artifacts_present,
        marker_search_available=marker_search_available,
    )

    assert decision.action is ResumeAction.RESUME_AT_STEP
    assert decision.step == "publication"


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
    store = store_for(tmp_path, marker_search=lambda _marker: False)

    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP


def test_a_remote_marker_with_no_local_row_blocks_the_reservation(tmp_path: Path) -> None:
    """§14.1 — a marker in the target repository is a duplicate artifact: no second write.

    The decision resumes so the run can adopt the artifact, and the atomic reservation is what
    refuses; the observable contract is that no durable row and no first write follow.
    """
    store = store_for(tmp_path, marker_search=lambda _marker: True)

    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP
    assert store.existing_artifact("codeql-0") is True
    assert store.append_if_new_artifact(codeql_candidate()) is False
    assert store.rows() == []


def test_resume_decision_defers_when_marker_search_fails(tmp_path: Path) -> None:
    """§14.1 — a *failed* search is unknown, not absent, so no first write is attempted."""

    def failing(_marker: str) -> bool:
        raise OSError("search unavailable")

    store = store_for(tmp_path, marker_search=failing)

    assert store.resume_decision("codeql-0").action is ResumeAction.DEFER
    assert store.marker_search_failed is True


def test_resume_decision_skips_a_completed_row_without_searching(tmp_path: Path) -> None:
    """§14.1 — a completed row needs no remote lookup; resume is decided locally."""
    searches: list[str] = []

    def counting(marker: str) -> bool:
        searches.append(marker)
        return False

    store = store_for(tmp_path, marker_search=counting)
    store.append(persisted(CandidateState.ISSUE_PATCHED, issue_url=ISSUE_URL, pr_url=PR_URL))

    assert store.resume_decision("codeql-0").action is ResumeAction.SKIP
    assert searches == []


def test_resume_decision_resumes_a_pr_created_row_without_searching(tmp_path: Path) -> None:
    """§14.1 — the `pr_created` crash window resumes from local evidence alone."""
    searches: list[str] = []

    def counting(marker: str) -> bool:
        searches.append(marker)
        return True

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

    def counting(marker: str) -> bool:
        searches.append(marker)
        return False

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

    def failing(_marker: str) -> bool:
        raise OSError("search unavailable")

    store = store_for(tmp_path, marker_search=failing)

    assert store.append_if_new_artifact(codeql_candidate()) is False
    assert store.rows() == []


def test_an_unconfigured_marker_search_is_not_a_failed_search(tmp_path: Path) -> None:
    """§14.1 — SIMULATE and local runs have no search to fail; they are not fail-closed."""
    store = store_for(tmp_path)

    assert store.marker_search_unavailable("codeql-0") is False
    assert store.resume_decision("codeql-0").action is ResumeAction.RESUME_AT_STEP
    assert store.append_if_new_artifact(codeql_candidate()) is True


def test_a_reserved_candidate_cannot_be_reserved_twice(tmp_path: Path) -> None:
    """§14.1 — the reservation is atomic: exactly one durable row per first write."""
    store = store_for(tmp_path, marker_search=lambda _marker: False)
    candidate = codeql_candidate(state=CandidateState.ISSUE_CREATED, issue_url=ISSUE_URL)

    assert store.append_if_new_artifact(candidate) is True
    assert store.append_if_new_artifact(candidate) is False
    assert len(store.rows()) == 1


def test_github_marker_search_reads_the_search_total() -> None:
    """§14.1 — the LIVE marker lookup is GitHub search, and only a hit counts as present."""
    hit = github_marker_search(lambda _marker: {"total_count": 1})
    miss = github_marker_search(lambda _marker: {"total_count": 0})
    malformed = github_marker_search(lambda _marker: "unexpected")

    assert hit("marker") is True
    assert miss("marker") is False
    assert malformed("marker") is False


# -- both call sites derive resume the same way ------------------------------------------


@pytest.mark.parametrize("function", ["_publish_live", "_prepare_live_candidate"])
def test_both_live_call_sites_resume_through_the_store_only(function: str) -> None:
    """§14.1 — resume is derived once, in the store; neither call site may re-derive it."""
    source = inspect.getsource(getattr(entrypoint, function))

    assert "state_store.resume_decision(candidate.candidate_id)" in source
    assert "decide_resume(" not in source
    assert "marker_search_unavailable" not in source
    assert "existing_artifact" not in source
