"""Focused source-observation tests for LANE 2 skip markers."""

from __future__ import annotations

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
