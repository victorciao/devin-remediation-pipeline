"""§14.1 identity, digests, marker, drift match and the append-only state store."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from pipeline.schemas import Candidate, CandidateState, Lane
from tests import _api
from tests.factories import TARGET_REPO, codeql_candidate

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
    dedupe = _api.dedupe()

    first = dedupe.compute_candidate_id(Lane.CODEQL, TARGET_REPO, "locator-a")
    again = dedupe.compute_candidate_id(Lane.CODEQL, TARGET_REPO, "locator-a")

    assert first == again
    assert first != dedupe.compute_candidate_id(Lane.CODEQL, TARGET_REPO, "locator-b")
    assert first != dedupe.compute_candidate_id(Lane.SKIPPED_TESTS, TARGET_REPO, "locator-a")
    assert first != dedupe.compute_candidate_id(Lane.CODEQL, "other/repo", "locator-a")
    assert len(first) == 64
    assert int(first, 16) >= 0


def test_position_digest_matches_the_documented_derivation() -> None:
    """§14.1 — `sha256("{start_line}:{start_column}-{end_line}:{end_column}")[:12]`."""
    digest = _api.dedupe().position_digest(LOCATION)

    assert digest == expected_position_digest(LOCATION)
    assert len(digest) == 12


def test_position_digest_separates_columns_on_one_line() -> None:
    dedupe = _api.dedupe()
    digests = {
        dedupe.position_digest({**LOCATION, "start_column": column, "end_column": column + 4})
        for column in (6, 9, 12, 15)
    }

    assert len(digests) == 4


def test_region_digest_is_whitespace_normalized() -> None:
    dedupe = _api.dedupe()

    digest = dedupe.region_digest(REGION_TEXT)

    assert digest == dedupe.region_digest(f"   {REGION_TEXT}\n\t")
    assert digest != dedupe.region_digest("for index in range(0, 10):")
    assert len(digest) == 12


def test_marker_is_the_documented_html_comment() -> None:
    assert _api.dedupe().marker("abc123") == "<!-- devin-remediation-id: abc123 -->"


# -- drift match -------------------------------------------------------------------------


def shifted(candidate_id: str, *, start_line: int) -> Candidate:
    """The same alert after an unrelated edit above it shifted its line."""
    return codeql_candidate(
        candidate_id=candidate_id,
        position_digest=expected_position_digest({**LOCATION, "start_line": start_line}),
        region_digest="regiondigest1",
        symbol_relative_offset=3,
    )


def test_shifted_alert_reuses_prior_candidate() -> None:
    """§14.1 — a pure line shift links to the prior row on the two-condition match."""
    prior = shifted("codeql-prior", start_line=55)
    prior = prior.model_copy(update={"state": CandidateState.PR_CREATED})
    current = shifted("codeql-shifted", start_line=61)

    match = _api.dedupe().drift_match(current, scan=[current], state_rows=[prior])

    assert match.linked is True
    assert match.prior_candidate_id == "codeql-prior"


def test_drift_match_requires_region_digest() -> None:
    """§14.1 condition 2 — an unambiguous weak key with a different region does not link."""
    prior = shifted("codeql-prior", start_line=55)
    current = shifted("codeql-shifted", start_line=61).model_copy(
        update={"region_digest": "regiondigest2", "symbol_relative_offset": 9}
    )

    match = _api.dedupe().drift_match(current, scan=[current], state_rows=[prior])

    assert match.linked is False
    assert match.prior_candidate_id is None


def test_drift_match_refuses_colocated_weak_key_multiplicity() -> None:
    """§14.1 condition 1 — multiplicity > 1 on either side disables the drift path."""
    scan = [
        shifted(f"codeql-{column}", start_line=61).model_copy(
            update={"position_digest": expected_position_digest({**LOCATION, "start_column": c})}
        )
        for column, c in ((6, 6), (9, 9), (12, 12), (15, 15))
    ]
    prior = shifted("codeql-prior", start_line=55)

    match = _api.dedupe().drift_match(scan[0], scan=scan, state_rows=[prior])

    assert match.linked is False


def test_drift_survives_repeated_shifts() -> None:
    """§14.1 — superseded state rows are inactive, so a second shift still links."""
    first = shifted("codeql-first", start_line=55).model_copy(
        update={"superseded_by": "codeql-second"}
    )
    second = shifted("codeql-second", start_line=61)
    third = shifted("codeql-third", start_line=70)

    match = _api.dedupe().drift_match(third, scan=[third], state_rows=[first, second])

    assert match.linked is True
    assert match.prior_candidate_id == "codeql-second"


# -- state store -------------------------------------------------------------------------


def test_state_store_is_append_only_last_write_wins(tmp_path: Path) -> None:
    """§14.1 — `state/candidates.jsonl` is append-only; reads collapse by `candidate_id`."""
    dedupe = _api.dedupe()
    path = tmp_path / "candidates.jsonl"
    candidate = codeql_candidate(candidate_id="codeql-1")

    dedupe.append_state(path, candidate)
    dedupe.append_state(path, candidate.model_copy(update={"state": CandidateState.PR_CREATED}))
    dedupe.append_state(path, codeql_candidate(candidate_id="codeql-2"))

    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 3
    rows = {row.candidate_id: row for row in dedupe.load_state(path)}
    assert set(rows) == {"codeql-1", "codeql-2"}
    assert rows["codeql-1"].state == CandidateState.PR_CREATED


def test_load_state_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert _api.dedupe().load_state(tmp_path / "candidates.jsonl") == []


@pytest.mark.parametrize(
    "state", [CandidateState.BLOCKED_BY_ENCLOSING_SKIP, CandidateState.DEFERRED]
)
def test_non_terminal_states_round_trip_through_the_store(
    tmp_path: Path, state: CandidateState
) -> None:
    """§9.2 — blocked/suppressed/deferred rows must survive to be re-evaluated next run."""
    dedupe = _api.dedupe()
    path = tmp_path / "candidates.jsonl"

    dedupe.append_state(path, codeql_candidate(candidate_id="codeql-3", state=state))

    assert dedupe.load_state(path)[0].state == state
