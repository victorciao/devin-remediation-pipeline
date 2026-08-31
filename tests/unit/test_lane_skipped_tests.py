"""§5 LANE 2 — enumerator classification, scope limits and fully qualified nodeids."""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pipeline.lanes.skipped_tests import collect_skipped_tests, enumerate_skipped_tests
from pipeline.schemas import Candidate, DefinitionKind, Lane
from tests.conftest import TARGET_CHECKOUT, TARGET_REPO, TEST_DATA_DIR

BASIC_TREE = TEST_DATA_DIR / "skip_tree" / "basic"
SCOPE_TREE = TEST_DATA_DIR / "skip_tree" / "scope_limits"
COLLECT_ARGS = ["-o", "python_files=skipmod_*.py", "-o", "addopts=", "--collect-only", "-q"]


def mini_repo(tmp_path: Path, tree: Path) -> Path:
    """Stage a fixture tree as ``<repo>/tests`` — the layout the enumerator walks."""
    repo = tmp_path / "repo"
    shutil.copytree(tree, repo / "tests")
    return repo


def enumerate_tree(tmp_path: Path, tree: Path) -> tuple[list[Candidate], list[dict[str, Any]]]:
    candidates, excluded = enumerate_skipped_tests(mini_repo(tmp_path, tree))
    return candidates, [dict(record) for record in excluded]


def collect(nodeid: str, *, rootdir: Path) -> list[str]:
    """Collected item ids for ``nodeid``, from a pytest run that sees the mini-tree."""
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


def test_enumerator_classifies_skip_fixture_tree(tmp_path: Path) -> None:
    """§17 — 2 included / 2 excluded with `conditional_environment_guard` / `xfail` reasons."""
    candidates, excluded = enumerate_tree(tmp_path, BASIC_TREE)

    assert len(candidates) == 2
    assert len(excluded) == 2
    assert {candidate.resolved_decorator for candidate in candidates} == {
        "unittest.skip",
        "pytest.mark.skip",
    }
    assert {record["excluded_reason"] for record in excluded} == {
        "conditional_environment_guard",
        "expected_failure_xfail",
    }


def test_skip_unless_yields_zero_candidates(tmp_path: Path) -> None:
    """§17 — a `skipUnless` guard is an exclusion, never a candidate."""
    candidates, excluded = enumerate_tree(tmp_path, BASIC_TREE)
    guards = [
        record
        for record in excluded
        if record["excluded_reason"] == "conditional_environment_guard"
    ]

    assert len(guards) == 1
    assert guards[0]["symbol"] == "test_conditional_guard"
    assert all("test_conditional_guard" not in str(c.nodeid) for c in candidates)


def test_enumerator_scope_limits(tmp_path: Path) -> None:
    """§17 — `pytestmark`, imperative skips and mark aliases yield neither rows nor exclusions."""
    candidates, excluded = enumerate_tree(tmp_path, SCOPE_TREE)

    assert candidates == []
    assert excluded == []


def test_enumerated_nodeids_are_fully_qualified(tmp_path: Path) -> None:
    """§14.1 — a nodeid that omits its enclosing class does not collect."""
    candidates, _ = enumerate_tree(tmp_path, BASIC_TREE)

    for candidate in candidates:
        assert candidate.nodeid is not None
        assert candidate.class_scope is not None
        assert f"::{candidate.class_scope}::" in candidate.nodeid
        assert candidate.nodeid.count("::") == 2


def test_lane2_nodeids_are_collectable(tmp_path: Path) -> None:
    """§17 — every emitted nodeid resolves to at least one collected item."""
    repo = mini_repo(tmp_path, BASIC_TREE)
    candidates, _ = enumerate_skipped_tests(repo)

    assert candidates
    for candidate in candidates:
        assert candidate.nodeid is not None
        assert collect(candidate.nodeid, rootdir=repo), candidate.nodeid


def test_lane2_nodeid_without_its_class_does_not_collect(tmp_path: Path) -> None:
    """§14.1 — the two review rounds spent on nodeids exist because of exactly this case."""
    repo = mini_repo(tmp_path, BASIC_TREE)
    candidates, _ = enumerate_skipped_tests(repo)

    for candidate in candidates:
        assert candidate.nodeid is not None
        path, _class, method = candidate.nodeid.split("::")
        assert collect(f"{path}::{method}", rootdir=repo) == []


def test_to_candidates_uses_the_nodeid_as_locator(tmp_path: Path) -> None:
    """§14.1 — the LANE 2 locator is the fully qualified nodeid."""
    candidates, _ = enumerate_skipped_tests(mini_repo(tmp_path, BASIC_TREE), repo_name=TARGET_REPO)

    assert len(candidates) == 2
    assert {candidate.lane for candidate in candidates} == {Lane.SKIPPED_TESTS}
    for candidate in candidates:
        assert candidate.stable_locator == candidate.nodeid
        assert candidate.repo == TARGET_REPO
        assert candidate.kind in (DefinitionKind.FUNCTION, DefinitionKind.CLASS)
    assert len({candidate.candidate_id for candidate in candidates}) == 2


def test_class_scope_breadth_is_carried_on_the_candidate(tmp_path: Path) -> None:
    """§4.2 — the gate needs `enclosed_tests` / `collects_single_item` from the enumerator."""
    candidates, _ = enumerate_skipped_tests(mini_repo(tmp_path, BASIC_TREE))

    for candidate in candidates:
        assert candidate.kind is DefinitionKind.FUNCTION
        assert candidate.enclosed_tests == 0
        assert candidate.collects_single_item is True
        assert candidate.parametrized is False
        assert candidate.enclosing_skip_nodeid is None


def test_live_enclosed_tests_come_from_the_injected_provider(tmp_path: Path) -> None:
    """§4.2 — `class_breadth_unknown` needs a live count, so the provider must reach the row."""
    seen: list[str] = []

    def provider(nodeid: str) -> int | None:
        seen.append(nodeid)
        return 4

    candidates, _ = enumerate_skipped_tests(
        mini_repo(tmp_path, BASIC_TREE), live_count_provider=provider
    )

    assert len(seen) == 2
    assert {candidate.live_enclosed_tests for candidate in candidates} == {4}


def test_collect_skipped_tests_is_the_single_implementation(tmp_path: Path) -> None:
    """§5 — the baseline records and the lane candidates come from the same walk."""
    repo = mini_repo(tmp_path, BASIC_TREE)

    included, excluded = collect_skipped_tests(repo)
    candidates, lane_excluded = enumerate_skipped_tests(repo)

    assert [record["nodeid"] for record in included] == [c.nodeid for c in candidates]
    assert excluded == lane_excluded


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


def test_enumerator_matches_baseline_totals(baseline: Mapping[str, Any]) -> None:
    """§17 drift check — re-walking the target checkout must reproduce the recorded totals."""
    assert len(baseline["skipped_tests"]) == baseline["totals"]["skipped_tests"] == 35
    assert baseline["totals"]["excluded_conditional_skips"] == 33
    assert (TARGET_CHECKOUT / "tests").is_dir(), f"target checkout required at {TARGET_CHECKOUT}"

    candidates, excluded = enumerate_skipped_tests(TARGET_CHECKOUT, repo_name=TARGET_REPO)

    assert len(candidates) == baseline["totals"]["skipped_tests"]
    assert len(excluded) == baseline["totals"]["excluded_conditional_skips"]
    assert {c.nodeid for c in candidates} == {row["nodeid"] for row in baseline["skipped_tests"]}
