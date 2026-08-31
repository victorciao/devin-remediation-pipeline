"""Candidate deduplication and drift matching."""

from __future__ import annotations

from collections import Counter
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
    if previous.region_digest is not None:
        return previous.region_digest == current.region_digest
    return (
        previous.symbol_relative_offset is not None
        and previous.symbol_relative_offset == current.symbol_relative_offset
    )


def find_drift_match(
    previous: Sequence[Candidate],
    current: Candidate,
    *,
    current_scan: Sequence[Candidate] | None = None,
) -> Candidate | None:
    """Find a unique prior LANE 1 candidate with an equivalent drift anchor.

    The weak key is eligible only when it occurs once in the current scan and once
    among active persisted rows.  Callers that have the complete scan should pass
    it as ``current_scan``; the single-candidate form remains useful for callers
    that have already established current-scan uniqueness.
    """
    active = [candidate for candidate in previous if candidate.superseded_by is None]
    key = weak_key(current)
    if key is None:
        return None
    scan = (current,) if current_scan is None else current_scan
    current_counts = Counter(
        candidate_key for candidate in scan if (candidate_key := weak_key(candidate)) is not None
    )
    if current_counts[key] != 1:
        return None
    active_counts = Counter(
        candidate_key for candidate in active if (candidate_key := weak_key(candidate)) is not None
    )
    if active_counts[key] != 1:
        return None
    matches = [candidate for candidate in active if can_link_drift(candidate, current)]
    return matches[0] if len(matches) == 1 else None


__all__ = ["can_link_drift", "find_drift_match", "weak_key"]
