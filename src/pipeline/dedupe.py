"""Candidate deduplication and drift matching."""

from __future__ import annotations

from collections.abc import Sequence

from pipeline.schemas import Candidate, Lane


def weak_key(candidate: Candidate) -> tuple[str, str, str] | None:
    """Return the LANE 1 weak key used for line-shift drift matching."""
    if candidate.lane is not Lane.CODEQL:
        return None
    if (
        candidate.rule_id is None
        or candidate.file_path is None
        or candidate.normalized_symbol is None
    ):
        return None
    return candidate.rule_id, candidate.file_path, candidate.normalized_symbol


def can_link_drift(previous: Candidate, current: Candidate) -> bool:
    """Reject drift links when the positional anchor source changed."""
    if previous.superseded_by is not None:
        return False
    if previous.region_source != current.region_source:
        return False
    if weak_key(previous) != weak_key(current):
        return False
    if previous.region_digest is not None and previous.region_digest == current.region_digest:
        return True
    return (
        previous.symbol_relative_offset is not None
        and previous.symbol_relative_offset == current.symbol_relative_offset
    )


def find_drift_match(
    previous: Sequence[Candidate],
    current: Candidate,
) -> Candidate | None:
    """Find a unique prior LANE 1 candidate with an equivalent drift anchor."""
    matches = [candidate for candidate in previous if can_link_drift(candidate, current)]
    return matches[0] if len(matches) == 1 else None


__all__ = ["can_link_drift", "find_drift_match", "weak_key"]
