"""§4.2/§5 LANE 3 — EOL derivation, the version source, and caller/override gating."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from pipeline.config import ConfigError, PipelineConfig
from pipeline.dispatch import DROPPED_REASONS
from pipeline.gate import evaluate_gates
from pipeline.lanes.deprecations import (
    VERSION_SOURCE,
    collect_deprecations,
    current_release,
    enumerate_deprecations,
    is_eol,
)
from pipeline.rubric import resolve_factors
from pipeline.schemas import Candidate, GateName, Lane, ReasonCode
from tests.conftest import RUBRICS_PATH, TARGET_CHECKOUT, TEMPLATES_DIR

BUG_REPORT_FORM = """\
name: Bug report
body:
  - type: dropdown
    id: superset-version
    attributes:
      label: Superset version
      options:
        - master / latest-dev
        - "6.1.0"
        - "6.0.0"
        - "5.0.0"
  - type: input
    id: unrelated
"""

FORM_WITHOUT_A_RELEASE = """\
name: Bug report
body:
  - type: dropdown
    id: superset-version
    attributes:
      label: Superset version
      options:
        - master / latest-dev
        - not applicable
  - type: input
    id: unrelated
"""

MODULE_SOURCE = """\
from superset.utils.deprecation import deprecated


class BaseEngineSpec:
    @deprecated(deprecated_in="3.0")
    def normalize_indexes(self, indexes):
        return indexes

    @deprecated(deprecated_in="6.0.0")
    def get_url_for_impersonation(self, url):
        return url
"""


def mini_repo(tmp_path: Path, *, form: str = BUG_REPORT_FORM, source: str = MODULE_SOURCE) -> Path:
    """Stage the two files the LANE 3 enumerator reads: the version form and a module."""
    repo = tmp_path / "superset-repo"
    (repo / VERSION_SOURCE).parent.mkdir(parents=True)
    (repo / VERSION_SOURCE).write_text(form, encoding="utf-8")
    (repo / "superset" / "db_engine_specs").mkdir(parents=True)
    (repo / "superset" / "db_engine_specs" / "base.py").write_text(source, encoding="utf-8")
    return repo


def by_qualname(candidates: list[Candidate]) -> dict[str, Candidate]:
    return {candidate.qualname: candidate for candidate in candidates if candidate.qualname}


def test_current_major_from_version_source(tmp_path: Path) -> None:
    """§17 — `6.1.0 -> 6`, and a form with no concrete release is a startup config error."""
    assert current_release(mini_repo(tmp_path)) == ("6.1.0", 6)

    with pytest.raises(ConfigError):
        current_release(mini_repo(tmp_path / "drifted", form=FORM_WITHOUT_A_RELEASE))


def test_baseline_records_the_resolved_version(baseline: Mapping[str, Any]) -> None:
    assert baseline["current_release"] == "6.1.0"
    assert baseline["current_major"] == 6
    assert baseline["eol_threshold_major"] == 4
    assert baseline["version_source"] == VERSION_SOURCE == ".github/ISSUE_TEMPLATE/bug-report.yml"


def test_version_source_matches_the_live_target_form() -> None:
    """§13 — the version source is drift-tested against the target checkout."""
    assert (TARGET_CHECKOUT / VERSION_SOURCE).is_file()

    assert current_release(TARGET_CHECKOUT) == ("6.1.0", 6)


def test_eol_rule_is_the_major_lag_threshold() -> None:
    """§4.2 — `current_major = 6` with `eol_major_lag = 2` means `major <= 4` is EOL."""
    assert is_eol("3.0", 6) is True
    assert is_eol("4.0", 6) is True
    assert is_eol("5.0", 6) is False
    assert is_eol("6.0.0", 6) is False
    assert is_eol("5.0", 6, eol_major_lag=1) is True


def test_removed_in_at_or_below_current_version_is_eol() -> None:
    """§4.2 — an explicit `removed_in` short-circuits the major-lag rule."""
    assert is_eol("6.0.0", 6, removed_in="6.1.0", current_release="6.1.0") is True
    assert is_eol("6.0.0", 6, removed_in="7.0.0", current_release="6.1.0") is False


def test_only_eol_passed_sites_become_candidates(tmp_path: Path) -> None:
    """§4.2 — non-EOL sites carry `not_eol` and are dropped, never dispatched."""
    candidates = enumerate_deprecations(mini_repo(tmp_path))
    rows = by_qualname(candidates)

    assert set(rows) == {
        "BaseEngineSpec.normalize_indexes",
        "BaseEngineSpec.get_url_for_impersonation",
    }
    assert rows["BaseEngineSpec.normalize_indexes"].reason is None
    assert rows["BaseEngineSpec.get_url_for_impersonation"].reason is ReasonCode.NOT_EOL
    assert {candidate.lane for candidate in candidates} == {Lane.DEPRECATIONS}


def test_get_url_for_impersonation_is_dropped_for_not_eol(tmp_path: Path) -> None:
    """§17 — deprecated in 6.0.0 with `current_major = 6`, so its outcome is the `not_eol` drop."""
    rows = by_qualname(enumerate_deprecations(mini_repo(tmp_path)))

    dropped = rows["BaseEngineSpec.get_url_for_impersonation"]

    assert dropped.reason is ReasonCode.NOT_EOL
    assert ReasonCode.NOT_EOL in DROPPED_REASONS


def test_not_eol_is_a_distinct_reason_from_the_caller_gate(tmp_path: Path) -> None:
    """§4.2 — the EOL drop and the human-routed caller gate are separate outcomes."""
    reasons = {
        candidate.reason
        for candidate in enumerate_deprecations(mini_repo(tmp_path))
        if candidate.reason is not None
    }

    assert reasons == {ReasonCode.NOT_EOL}
    assert ReasonCode.NOT_EOL.value == "not_eol"
    assert ReasonCode.INTERNAL_CALLER not in DROPPED_REASONS


def test_eol_major_lag_knob_moves_the_threshold(tmp_path: Path) -> None:
    """§13 — the EOL lag is a knob; raising the threshold admits the 6.0.0 deprecation."""
    candidates = enumerate_deprecations(mini_repo(tmp_path), eol_major_lag=0)

    assert all(candidate.reason is None for candidate in candidates)


def test_locator_is_module_colon_qualname(tmp_path: Path) -> None:
    for candidate in enumerate_deprecations(mini_repo(tmp_path)):
        assert candidate.stable_locator == f"{candidate.module}:{candidate.qualname}"
        assert candidate.module == "superset.db_engine_specs.base"


def test_internal_caller_is_gated_not_dropped(
    tmp_path: Path, simulate_config: PipelineConfig
) -> None:
    """§4.2 — an EOL-passed site with live callers reaches the gate and fails automatability."""
    source = (
        MODULE_SOURCE
        + """

def caller():
    return BaseEngineSpec().normalize_indexes([])
"""
    )
    candidates = enumerate_deprecations(mini_repo(tmp_path, source=source))
    candidate = by_qualname(candidates)["BaseEngineSpec.normalize_indexes"]

    assert candidate.internal_caller is True
    assert candidate.reason is not ReasonCode.NOT_EOL

    factors = resolve_factors(candidate, simulate_config)
    evaluation = evaluate_gates(candidate, simulate_config, resolved_factors=factors)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.INTERNAL_CALLER


def test_public_rest_endpoint_is_human_routed(
    tmp_path: Path, simulate_config: PipelineConfig
) -> None:
    """§4.2 — an EOL-passed but `@expose`d endpoint fails automatability as a public surface."""
    source = """\
from flask_appbuilder.api import expose

from superset.utils.deprecation import deprecated


class DatabaseRestApi:
    @expose("/<int:pk>/table_extra/", methods=("GET",))
    @deprecated(deprecated_in="4.0")
    def table_extra_metadata_deprecated(self, pk):
        return {}
"""
    candidates = enumerate_deprecations(mini_repo(tmp_path, source=source))
    candidate = by_qualname(candidates)["DatabaseRestApi.table_extra_metadata_deprecated"]

    assert candidate.public_api_surface is True

    factors = resolve_factors(candidate, simulate_config)
    evaluation = evaluate_gates(candidate, simulate_config, resolved_factors=factors)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.PUBLIC_API_SURFACE


def test_collect_deprecations_reproduces_the_baseline_rows(baseline: Mapping[str, Any]) -> None:
    """§17 drift check — re-walking the target checkout reproduces the recorded rows."""
    records: list[Mapping[str, Any]] = list(baseline["deprecations"])

    assert len(records) == 4
    assert (TARGET_CHECKOUT / "superset").is_dir()

    collected = collect_deprecations(
        TARGET_CHECKOUT, current_major=6, current_release_value="6.1.0"
    )

    assert {str(row["locator"]) for row in collected} == {str(row["locator"]) for row in records}
    for row in collected:
        expected = next(r for r in records if r["locator"] == row["locator"])
        assert row["line"] == expected["line"]
        assert row["decorator_line"] == expected["decorator_line"]
        assert row["deprecated_in"] == expected["deprecated_in"]


def test_eol_passed_total_matches_the_baseline(baseline: Mapping[str, Any]) -> None:
    """§4.2 — exactly two of the four recorded deprecations are EOL at `current_major = 6`."""
    eol_passed = [
        row
        for row in baseline["deprecations"]
        if is_eol(
            str(row["deprecated_in"]),
            6,
            removed_in=row["removed_in"],
            current_release="6.1.0",
        )
    ]

    assert len(eol_passed) == baseline["totals"]["eol_passed_deprecations"] == 2
    assert {str(row["qualname"]) for row in eol_passed} == {
        "BaseEngineSpec.normalize_indexes",
        "DatabaseRestApi.table_extra_metadata_deprecated",
    }


def test_rubrics_path_config_is_the_single_rubric_owner(simulate_config: PipelineConfig) -> None:
    """§4.1 — LANE 3 factors are resolved from `config/rubrics.yaml`, never pre-set on the row."""
    assert simulate_config.rubrics_path == RUBRICS_PATH
    assert simulate_config.templates_dir == TEMPLATES_DIR
