"""Tests for orchestrator-owned local observation parsing."""

from pipeline.observers import parse_item_outcomes
from pipeline.schemas import ItemOutcome


def test_skip_summary_resolves_to_a_real_nodeid() -> None:
    """A pytest path:line skip summary is mapped to its collected nodeid."""
    outcomes = parse_item_outcomes(
        "SKIPPED [1] tests/x.py:6: broken",
        resolve_skip_nodeid=lambda path, line: f"{path}::test_broken_{line}",
    )

    assert len(outcomes) == 1
    assert outcomes[0].nodeid == "tests/x.py::test_broken_6"
    assert outcomes[0].outcome is ItemOutcome.SKIPPED
