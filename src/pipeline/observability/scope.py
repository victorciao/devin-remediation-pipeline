"""Shared attribution scoping for persisted observability rows."""

from __future__ import annotations

from collections.abc import Collection, Iterable

from pipeline.schemas import Candidate

__all__ = ["written_by_run"]


def written_by_run(
    candidates: Iterable[Candidate], run_ids: Collection[str]
) -> tuple[Candidate, ...]:
    """Keep the rows attributed to one of these runs.

    Rows carrying no attribution are kept: historical state predates run
    stamping. When no run id is supplied, every row is kept, so cumulative
    callers are unaffected.
    """
    if not run_ids:
        return tuple(candidates)
    return tuple(
        candidate
        for candidate in candidates
        if candidate.run_id is None or candidate.run_id in run_ids
    )
