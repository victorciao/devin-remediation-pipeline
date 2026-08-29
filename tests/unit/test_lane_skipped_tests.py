"""§5 LANE 2 — enumerator classification, scope limits and fully qualified nodeids."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pipeline.config import PipelineConfig
from pipeline.schemas import DefinitionKind, Lane, ReasonCode
from tests import _api
from tests.conftest import TEST_DATA_DIR

BASIC_TREE = TEST_DATA_DIR / "skip_tree" / "basic"
SCOPE_TREE = TEST_DATA_DIR / "skip_tree" / "scope_limits"
COLLECT_ARGS = ["-o", "python_files=skipmod_*.py", "-o", "addopts=", "--collect-only", "-q"]


def collect(nodeid: str, *, rootdir: Path) -> list[str]:
    """Collected item ids for ``nodeid``, using a pytest run that sees the mini-tree."""
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *COLLECT_ARGS, nodeid],
        cwd=rootdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return [
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", "<", "no tests ran"))
    ]


def test_enumerator_classifies_skip_fixture_tree() -> None:
    """§17 — 2 included / 2 excluded with `conditional_environment_guard` / `xfail` reasons."""
    enumeration = _api.skipped_tests_lane().enumerate_tree(BASIC_TREE)

    assert len(enumeration.included) == 2
    assert len(enumeration.excluded) == 2
    assert {record.resolved_decorator for record in enumeration.included} == {
        "unittest.skip",
        "pytest.mark.skip",
    }
    assert {record.excluded_reason for record in enumeration.excluded} == {
        ReasonCode.CONDITIONAL_ENVIRONMENT_GUARD,
        ReasonCode.EXPECTED_FAILURE_XFAIL,
    }


def test_skip_unless_yields_zero_candidates() -> None:
    """§17 — a `skipUnless` guard is an exclusion, never a candidate."""
    enumeration = _api.skipped_tests_lane().enumerate_tree(BASIC_TREE)
    guards = [
        record
        for record in enumeration.excluded
        if record.excluded_reason == ReasonCode.CONDITIONAL_ENVIRONMENT_GUARD
    ]

    assert len(guards) == 1
    assert guards[0].symbol == "test_conditional_guard"
    assert all(record.symbol != "test_conditional_guard" for record in enumeration.included)


def test_enumerator_scope_limits() -> None:
    """§17 — `pytestmark`, imperative skips and mark aliases yield neither rows nor exclusions."""
    enumeration = _api.skipped_tests_lane().enumerate_tree(SCOPE_TREE)

    assert list(enumeration.included) == []
    assert list(enumeration.excluded) == []


def test_enumerated_nodeids_are_fully_qualified() -> None:
    """§14.1 — a nodeid that omits its enclosing class does not collect."""
    enumeration = _api.skipped_tests_lane().enumerate_tree(BASIC_TREE)

    for record in enumeration.included:
        assert record.nodeid is not None
        assert record.class_scope is not None
        assert f"::{record.class_scope}::" in record.nodeid
        assert record.nodeid.endswith(f"::{record.symbol}")


def test_lane2_nodeids_are_collectable(repo_root: Path) -> None:
    """§17 — every emitted nodeid resolves to at least one collected item."""
    enumeration = _api.skipped_tests_lane().enumerate_tree(BASIC_TREE)

    for record in enumeration.included:
        assert record.nodeid is not None
        relative = (BASIC_TREE / Path(record.path).name).relative_to(repo_root)
        nodeid = "::".join([str(relative), *record.nodeid.split("::")[1:]])
        assert collect(nodeid, rootdir=repo_root), nodeid


def test_baseline_nodeids_carry_their_class_scope(baseline: Mapping[str, Any]) -> None:
    """§17 — every live row with a `class_scope` contains the `::<Class>::` segment."""
    rows: list[Mapping[str, Any]] = list(baseline["skipped_tests"])

    assert len(rows) == 35
    for row in rows:
        if row["class_scope"] is None:
            continue
        if row["kind"] == "class":
            assert row["nodeid"].endswith(f"::{row['class_scope']}")
        else:
            assert f"::{row['class_scope']}::" in row["nodeid"]


def test_multi_item_rows_declare_their_breadth(baseline: Mapping[str, Any]) -> None:
    """§17 — one-to-one is not asserted: multi-item rows carry the breadth evidence instead."""
    rows: list[Mapping[str, Any]] = list(baseline["skipped_tests"])
    multi_item = [row for row in rows if not row["collects_single_item"]]

    assert len(multi_item) == baseline["totals"]["multi_item_skipped_tests"] == 6
    for row in multi_item:
        assert row["enclosed_tests"] or row["parametrized"], row["nodeid"]


def test_nested_rows_name_their_enclosing_skip(baseline: Mapping[str, Any]) -> None:
    rows: list[Mapping[str, Any]] = list(baseline["skipped_tests"])
    nested = [row for row in rows if row["enclosing_skip_nodeid"]]

    assert len(nested) == baseline["totals"]["nested_under_skipped_class"] == 2
    nodeids = {row["nodeid"] for row in rows}
    for row in nested:
        assert row["enclosing_skip_nodeid"] in nodeids
        assert row["nodeid"].startswith(f"{row['enclosing_skip_nodeid']}::")


def test_to_candidates_uses_the_nodeid_as_locator(simulate_config: PipelineConfig) -> None:
    """§14.1 — the LANE 2 locator is the fully qualified nodeid."""
    lane = _api.skipped_tests_lane()
    enumeration = lane.enumerate_tree(BASIC_TREE)

    candidates = lane.to_candidates(enumeration, simulate_config)

    assert len(candidates) == 2
    assert {candidate.lane for candidate in candidates} == {Lane.SKIPPED_TESTS}
    for candidate in candidates:
        assert candidate.stable_locator == candidate.nodeid
        assert candidate.kind in (DefinitionKind.FUNCTION, DefinitionKind.CLASS)
    assert len({candidate.candidate_id for candidate in candidates}) == 2


def test_enumerator_matches_baseline_totals(baseline: Mapping[str, Any]) -> None:
    """§17 drift check — the recorded rows and the recorded totals must agree.

    When the target checkout is available (``SUPERSET_CHECKOUT``) the enumerator is re-run
    over it and must reproduce the same totals; otherwise the committed baseline is the
    subject, so the check runs unconditionally and never silently skips.
    """
    assert len(baseline["skipped_tests"]) == baseline["totals"]["skipped_tests"] == 35
    assert baseline["totals"]["excluded_conditional_skips"] == 33

    checkout = Path(os.environ.get("SUPERSET_CHECKOUT", "/nonexistent"))
    if not checkout.is_dir():
        return

    enumeration = _api.skipped_tests_lane().enumerate_tree(checkout / "tests")

    assert len(enumeration.included) == baseline["totals"]["skipped_tests"]
    assert len(enumeration.excluded) == baseline["totals"]["excluded_conditional_skips"]
