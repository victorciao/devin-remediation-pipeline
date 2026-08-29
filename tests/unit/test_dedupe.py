"""§14.1 identity, digests, marker, drift match and the append-only state store."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipeline.dedupe import can_link_drift, find_drift_match, weak_key
from pipeline.lanes.codeql import candidate_id, position_digest
from pipeline.schemas import Candidate, CandidateState, Lane
from pipeline.state import CandidateStateStore, repository_marker_search
from pipeline.templates.render import candidate_marker
from tests.factories import TARGET_REPO, codeql_candidate, lane2_candidate

LOCATION = {"start_line": 55, "start_column": 6, "end_line": 55, "end_column": 10}
REGION_TEXT = "for index in range(0, 1000000):"


def expected_position_digest(location: dict[str, int]) -> str:
    raw = (
        f"{location['start_line']}:{location['start_column']}"
        f"-{location['end_line']}:{location['end_column']}"
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


# -- identity ----------------------------------------------------------------------------


def test_candidate_id_is_a_stable_hash_of_lane_repo_and_locator() -> None:
    """§14.1 — the identity is `sha256(lane|repo|stable_locator)`."""
    first = candidate_id(TARGET_REPO, "locator-a")
    again = candidate_id(TARGET_REPO, "locator-a")

    assert first == again
    assert first != candidate_id(TARGET_REPO, "locator-b")
    assert first != candidate_id("other/repo", "locator-a")
    assert (
        first == hashlib.sha256(f"{Lane.CODEQL.value}|{TARGET_REPO}|locator-a".encode()).hexdigest()
    )
    assert len(first) == 64


def test_position_digest_matches_the_documented_derivation() -> None:
    """§14.1 — `sha256("{start_line}:{start_column}-{end_line}:{end_column}")[:12]`."""
    digest = position_digest(LOCATION)

    assert digest == expected_position_digest(LOCATION)
    assert len(digest) == 12


def test_position_digest_separates_columns_on_one_line() -> None:
    """The four co-located alerts differ only by column and must not collide."""
    digests = {
        position_digest({**LOCATION, "start_column": column, "end_column": column + 4})
        for column in (6, 9, 12, 15)
    }

    assert len(digests) == 4


def test_marker_is_the_documented_html_comment() -> None:
    assert candidate_marker("abc123") == "<!-- devin-remediation-id: abc123 -->"


# -- weak key ----------------------------------------------------------------------------


def test_weak_key_is_lane_one_only() -> None:
    """§14.1 — the weak-key drift fallback exists for LANE 1 alerts only."""
    assert weak_key(codeql_candidate()) is not None
    assert weak_key(lane2_candidate()) is None


def test_weak_key_is_rule_file_and_symbol() -> None:
    candidate = codeql_candidate()

    assert weak_key(candidate) == (
        candidate.rule_id,
        candidate.file_path,
        candidate.normalized_symbol,
    )


# -- drift match -------------------------------------------------------------------------


def shifted(candidate_id_value: str, *, start_line: int) -> Candidate:
    """The same alert after an unrelated edit above it shifted its line."""
    return codeql_candidate(
        candidate_id=candidate_id_value,
        position_digest=expected_position_digest({**LOCATION, "start_line": start_line}),
        region_digest="regiondigest1",
        region_source="source_region",
        symbol_relative_offset=3,
    )


def test_shifted_alert_reuses_prior_candidate() -> None:
    """§14.1 — a pure line shift links to the prior row on the two-condition match."""
    prior = shifted("codeql-prior", start_line=55).model_copy(
        update={"state": CandidateState.PR_CREATED}
    )
    current = shifted("codeql-shifted", start_line=61)

    match = find_drift_match([prior], current, current_scan=[current])

    assert match is not None
    assert match.candidate_id == "codeql-prior"


def test_drift_match_requires_region_digest() -> None:
    """§14.1 condition 2 — an unambiguous weak key with a different region does not link."""
    prior = shifted("codeql-prior", start_line=55)
    current = shifted("codeql-shifted", start_line=61).model_copy(
        update={"region_digest": "regiondigest2", "symbol_relative_offset": 9}
    )

    assert can_link_drift(prior, current) is False
    assert find_drift_match([prior], current, current_scan=[current]) is None


def test_drift_match_refuses_colocated_weak_key_multiplicity() -> None:
    """§14.1 condition 1 — multiplicity > 1 on either side disables the drift path."""
    scan = [
        shifted(f"codeql-{column}", start_line=61).model_copy(
            update={
                "position_digest": expected_position_digest({**LOCATION, "start_column": column})
            }
        )
        for column in (6, 9, 12, 15)
    ]
    prior = shifted("codeql-prior", start_line=55)

    assert find_drift_match([prior], scan[0], current_scan=scan) is None


def test_drift_match_refuses_ambiguous_prior_rows() -> None:
    """Multiplicity on the persisted side is equally disqualifying."""
    priors = [
        shifted("codeql-prior-a", start_line=55),
        shifted("codeql-prior-b", start_line=57),
    ]
    current = shifted("codeql-shifted", start_line=61)

    assert find_drift_match(priors, current, current_scan=[current]) is None


def test_drift_survives_repeated_shifts() -> None:
    """§14.1 — superseded state rows are inactive, so a second shift still links."""
    first = shifted("codeql-first", start_line=55).model_copy(
        update={"superseded_by": "codeql-second"}
    )
    second = shifted("codeql-second", start_line=61)
    third = shifted("codeql-third", start_line=70)

    match = find_drift_match([first, second], third, current_scan=[third])

    assert match is not None
    assert match.candidate_id == "codeql-second"


def test_drift_refuses_to_link_across_a_changed_region_source() -> None:
    """§14.1 — an alert-message anchor is not comparable to a source-region anchor."""
    prior = shifted("codeql-prior", start_line=55).model_copy(
        update={"region_source": "alert_message"}
    )
    current = shifted("codeql-shifted", start_line=61)

    assert can_link_drift(prior, current) is False


# -- state store -------------------------------------------------------------------------


def test_state_store_is_append_only_last_write_wins(tmp_path: Path) -> None:
    """§14.1 — `state/candidates.jsonl` is append-only; reads collapse by `candidate_id`."""
    path = tmp_path / "candidates.jsonl"
    store = CandidateStateStore(path)
    candidate = codeql_candidate(candidate_id="codeql-1")

    store.append(candidate)
    store.append(candidate.model_copy(update={"state": CandidateState.PR_CREATED}))
    store.append(codeql_candidate(candidate_id="codeql-2"))

    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 3
    latest = store.latest()
    assert set(latest) == {"codeql-1", "codeql-2"}
    assert latest["codeql-1"].state == CandidateState.PR_CREATED
    assert len(store.rows()) == 3


def test_load_state_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert CandidateStateStore(tmp_path / "candidates.jsonl").rows() == []


@pytest.mark.parametrize(
    "state", [CandidateState.BLOCKED_BY_ENCLOSING_SKIP, CandidateState.DEFERRED]
)
def test_non_terminal_states_round_trip_through_the_store(
    tmp_path: Path, state: CandidateState
) -> None:
    """§9.2 — blocked/suppressed/deferred rows must survive to be re-evaluated next run."""
    store = CandidateStateStore(tmp_path / "candidates.jsonl")

    store.append(codeql_candidate(candidate_id="codeql-3", state=state))

    assert store.rows()[0].state == state


def test_supersede_records_both_sides_of_a_drift_link(tmp_path: Path) -> None:
    """§14.1 — supersession is two immutable rows, never an in-place mutation."""
    store = CandidateStateStore(tmp_path / "candidates.jsonl")
    previous = shifted("codeql-prior", start_line=55)
    current = shifted("codeql-shifted", start_line=61)
    store.append(previous)

    store.supersede(previous, current)

    latest = store.latest()
    assert latest["codeql-prior"].superseded_by == "codeql-shifted"
    assert latest["codeql-shifted"].supersedes == "codeql-prior"
    assert len(store.rows()) == 3


def test_existing_artifact_blocks_a_duplicate_write(tmp_path: Path) -> None:
    """§14.1 — a resumed candidate with an artifact is never written a second time."""
    store = CandidateStateStore(tmp_path / "candidates.jsonl")
    candidate = codeql_candidate(
        candidate_id="codeql-4",
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://github.com/victorciao/superset/issues/1",
    )
    store.append(candidate)

    assert store.existing_artifact("codeql-4") is True
    assert store.append_if_new_artifact(candidate) is False
    assert len(store.rows()) == 1


def test_marker_search_finds_an_existing_target_artifact(tmp_path: Path) -> None:
    """§14.1 — the pre-write check also searches the target repository for the marker."""
    repository = tmp_path / "superset"
    (repository / "docs").mkdir(parents=True)
    (repository / "docs" / "note.md").write_text(
        candidate_marker("codeql-5"),
        encoding="utf-8",
    )
    store = CandidateStateStore(
        tmp_path / "candidates.jsonl",
        marker_search=repository_marker_search(repository),
    )

    assert store.marker_exists("codeql-5") is True
    assert store.marker_exists("codeql-6") is False
    assert store.append_if_new_artifact(codeql_candidate(candidate_id="codeql-5")) is False


def test_state_store_resume_returns_the_last_row(tmp_path: Path) -> None:
    """§14.2 — resume reads the last-write-wins row for one candidate."""
    store = CandidateStateStore(tmp_path / "candidates.jsonl")
    store.append(codeql_candidate(candidate_id="codeql-7"))
    store.append(codeql_candidate(candidate_id="codeql-7", state=CandidateState.ISSUE_CREATED))

    resumed = store.resume("codeql-7")

    assert resumed is not None
    assert resumed.state is CandidateState.ISSUE_CREATED
    assert store.resume("codeql-absent") is None
