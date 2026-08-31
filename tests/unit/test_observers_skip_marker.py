"""Focused source-observation tests for LANE 2 skip markers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from pipeline.observers import LocalCheckout
from tests.factories import lane2_candidate


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalCheckout, Path]:
    source = tmp_path / "source"
    source.mkdir()
    instance = LocalCheckout(repo_path=source, worktree_root=tmp_path / "worktrees")
    monkeypatch.setattr(instance, "revision", lambda _sha: source)
    return instance, source


def test_function_skip_decorator_is_observed(checkout: tuple[LocalCheckout, Path]) -> None:
    instance, source = checkout
    (source / "test_source.py").write_text(
        "import pytest\n\n@pytest.mark.skip(reason='broken')\ndef test_value():\n    pass\n",
        encoding="utf-8",
    )
    observed = instance.probe_skip_marker(
        lane2_candidate(nodeid="test_source.py::test_value"),
        "head",
    )
    assert observed.available is True
    assert observed.present is True
    assert "pytest.mark.skip" in observed.markers[0]


def test_enclosing_class_skip_decorator_is_observed(checkout: tuple[LocalCheckout, Path]) -> None:
    instance, source = checkout
    (source / "test_source.py").write_text(
        "import pytest\n\n@pytest.mark.skipif(True)\nclass TestValues:\n"
        "    def test_value(self):\n        pass\n",
        encoding="utf-8",
    )
    observed = instance.probe_skip_marker(
        lane2_candidate(nodeid="test_source.py::TestValues::test_value"),
        "head",
    )
    assert observed.present is True
    assert any("class TestValues" in marker for marker in observed.markers)


def test_in_body_pytest_skip_is_observed(checkout: tuple[LocalCheckout, Path]) -> None:
    instance, source = checkout
    (source / "test_source.py").write_text(
        "import pytest\n\ndef test_value():\n    pytest.skip('later')\n",
        encoding="utf-8",
    )
    observed = instance.probe_skip_marker(
        lane2_candidate(nodeid="test_source.py::test_value"),
        "head",
    )
    assert observed.present is True
    assert any("in-body call" in marker for marker in observed.markers)


def test_clean_source_has_no_skip_marker(checkout: tuple[LocalCheckout, Path]) -> None:
    instance, source = checkout
    (source / "test_source.py").write_text(
        "def test_value():\n    pass\n",
        encoding="utf-8",
    )
    observed = instance.probe_skip_marker(
        lane2_candidate(nodeid="test_source.py::test_value"),
        "head",
    )
    assert observed.available is True
    assert observed.present is False


def test_parse_error_is_unavailable(checkout: tuple[LocalCheckout, Path]) -> None:
    instance, source = checkout
    (source / "test_source.py").write_text("def test_value(:\n", encoding="utf-8")
    observed = instance.probe_skip_marker(
        lane2_candidate(nodeid="test_source.py::test_value"),
        "head",
    )
    assert observed.available is False
    assert observed.present is False
    assert "could not parse" in (observed.detail or "")


def test_revision_present_commit_does_not_fetch(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def runner(command: Sequence[str], _cwd: Path) -> tuple[int, str]:
        calls.append(command)
        return 0, ""

    instance = LocalCheckout(
        repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        runner=runner,
    )
    instance.repo_path.mkdir()
    instance.revision("present")

    assert any("cat-file" in command for command in calls)
    assert not any("fetch" in command for command in calls)


def test_revision_fetches_missing_commit_by_sha(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def runner(command: Sequence[str], _cwd: Path) -> tuple[int, str]:
        calls.append(command)
        if "cat-file" in command:
            return (1, "") if len([item for item in calls if "cat-file" in item]) == 1 else (0, "")
        return 0, ""

    instance = LocalCheckout(
        repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        runner=runner,
    )
    instance.repo_path.mkdir()
    instance.revision("missing")

    assert any(command[-2:] == ("origin", "missing") for command in calls)
    assert not any(command[-1] == "origin" for command in calls)
    assert any("worktree" in command for command in calls)


def test_revision_falls_back_to_full_fetch_after_sha_fetch_failure(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def runner(command: Sequence[str], _cwd: Path) -> tuple[int, str]:
        calls.append(command)
        if "cat-file" in command:
            return (1, "") if len([item for item in calls if "cat-file" in item]) == 1 else (0, "")
        if command[-2:] == ("origin", "missing"):
            return 1, "not found"
        return 0, ""

    instance = LocalCheckout(
        repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
        runner=runner,
    )
    instance.repo_path.mkdir()
    instance.revision("missing")

    assert any(command[-2:] == ("origin", "missing") for command in calls)
    assert any(command[-1] == "origin" for command in calls)
    assert sum("cat-file" in command for command in calls) == 2
    assert any("worktree" in command for command in calls)


def test_probe_skip_marker_fails_closed_when_fetches_cannot_materialize_revision(
    tmp_path: Path,
) -> None:
    calls: list[Sequence[str]] = []

    def runner(command: Sequence[str], _cwd: Path) -> tuple[int, str]:
        calls.append(command)
        return 1, "fetch failed"

    source = tmp_path / "source"
    source.mkdir()
    instance = LocalCheckout(
        repo_path=source,
        worktree_root=tmp_path / "worktrees",
        runner=runner,
    )
    candidate = lane2_candidate(nodeid="test_source.py::test_value")
    observed = instance.probe_skip_marker(candidate, "missing")

    assert observed.available is False
    assert observed.present is False
    assert "missing" in (observed.detail or "")
    assert any(command[-2:] == ("origin", "missing") for command in calls)
    assert any(command[-1] == "origin" for command in calls)
