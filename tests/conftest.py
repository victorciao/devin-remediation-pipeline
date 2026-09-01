"""Shared fixtures and candidate factories for the REPO A test suite."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import PipelineConfig
from pipeline.rubric import RubricTables, load_rubrics

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"
TEMPLATES_DIR = REPO_ROOT / "templates"
CONFIG_DIR = REPO_ROOT / "config"
TEST_DATA_DIR = Path(__file__).resolve().parent / "data"

TARGET_REPO = "victorciao/superset"

# The §14 drift checks and the LANE 2 collectability checks read the target checkout.
TARGET_CHECKOUT = Path(
    os.environ.get("SUPERSET_CHECKOUT", str(REPO_ROOT.parent / "superset"))
).resolve()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to REPO A."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def baseline() -> Mapping[str, Any]:
    """The Phase 0c baseline snapshot (`fixtures/baseline.json`)."""
    with (FIXTURES_DIR / "baseline.json").open(encoding="utf-8") as stream:
        loaded: Mapping[str, Any] = json.load(stream)
    return loaded


@pytest.fixture(scope="session")
def codeql_alerts() -> list[Mapping[str, Any]]:
    """The live CodeQL alert fixture (`fixtures/codeql_alerts.json`)."""
    with (FIXTURES_DIR / "codeql_alerts.json").open(encoding="utf-8") as stream:
        loaded: list[Mapping[str, Any]] = json.load(stream)
    return loaded


RUBRICS_PATH = CONFIG_DIR / "rubrics.yaml"


@pytest.fixture
def simulate_config() -> PipelineConfig:
    """The shipped defaults: SIMULATE mode with local CI evidence."""
    return PipelineConfig(rubrics_path=RUBRICS_PATH, templates_dir=TEMPLATES_DIR)


@pytest.fixture(scope="session")
def rubrics() -> RubricTables:
    """The shipped `config/rubrics.yaml` tables, loaded once."""
    return load_rubrics(RUBRICS_PATH)


@pytest.fixture(scope="session")
def target_checkout() -> Path:
    """The target Superset checkout the §14 drift checks compare against."""
    return TARGET_CHECKOUT
