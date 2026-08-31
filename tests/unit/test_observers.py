"""Tests for orchestrator-owned local observation parsing."""

from pathlib import Path

import pytest

from pipeline.observers import LocalCheckout, parse_item_outcomes
from pipeline.schemas import ItemOutcome
from tests.factories import lane3_candidate


def test_skip_summary_resolves_to_a_real_nodeid() -> None:
    """A pytest path:line skip summary is mapped to its collected nodeid."""
    outcomes = parse_item_outcomes(
        "SKIPPED [1] tests/x.py:6: broken",
        resolve_skip_nodeid=lambda path, line: f"{path}::test_broken_{line}",
    )

    assert len(outcomes) == 1
    assert outcomes[0].nodeid == "tests/x.py::test_broken_6"
    assert outcomes[0].outcome is ItemOutcome.SKIPPED


def test_probe_symbol_fails_closed_when_revision_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = LocalCheckout(
        repo_path=tmp_path / "source",
        worktree_root=tmp_path / "worktrees",
    )

    def unavailable(_sha: str) -> Path:
        raise RuntimeError("worktree failed")

    monkeypatch.setattr(instance, "revision", unavailable)

    observed = instance.probe_symbol(lane3_candidate(), "missing")

    assert observed.available is False
    assert observed.resolves is False
    assert "worktree failed" in (observed.detail or "")


def test_probe_symbol_fails_closed_when_module_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    instance = LocalCheckout(repo_path=source, worktree_root=tmp_path / "worktrees")
    monkeypatch.setattr(instance, "revision", lambda _sha: source)

    observed = instance.probe_symbol(lane3_candidate(), "head")

    assert observed.available is False
    assert observed.resolves is False
    assert "module source file is missing" in (observed.detail or "")


def test_probe_symbol_fails_closed_on_parse_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    (source / "superset" / "db_engine_specs").mkdir(parents=True)
    module = source / "superset" / "db_engine_specs" / "base.py"
    module.write_text("def broken(:\n", encoding="utf-8")
    instance = LocalCheckout(repo_path=source, worktree_root=tmp_path / "worktrees")
    monkeypatch.setattr(instance, "revision", lambda _sha: source)

    observed = instance.probe_symbol(lane3_candidate(), "head")

    assert observed.available is False
    assert observed.resolves is False
    assert "could not parse module source" in (observed.detail or "")
