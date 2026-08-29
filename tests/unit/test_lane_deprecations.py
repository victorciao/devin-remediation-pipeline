"""§4.2/§5 LANE 3 — EOL derivation, the version source, and caller/override gating."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pipeline.config import ConfigError, PipelineConfig
from pipeline.gate import evaluate_gates
from pipeline.schemas import GateName, Lane, ReasonCode
from tests import _api
from tests.conftest import RUBRICS_PATH, TEMPLATES_DIR

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
"""


def test_current_major_from_version_source() -> None:
    """§17 — `6.1.0 -> 6`, and a form with no concrete release is a startup config error."""
    lane = _api.deprecations_lane()

    assert lane.resolve_current_major(BUG_REPORT_FORM) == 6

    with pytest.raises(ConfigError):
        lane.resolve_current_major(FORM_WITHOUT_A_RELEASE)


def test_baseline_records_the_resolved_version(baseline: Mapping[str, Any]) -> None:
    assert baseline["current_release"] == "6.1.0"
    assert baseline["current_major"] == 6
    assert baseline["eol_threshold_major"] == 4
    assert baseline["version_source"] == ".github/ISSUE_TEMPLATE/bug-report.yml"
    assert (
        _api.deprecations_lane().resolve_current_major(BUG_REPORT_FORM)
        == (baseline["current_major"])
    )


def test_only_eol_passed_sites_become_candidates(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§4.2 — with `current_major = 6` and `eol_major_lag = 2` the threshold is `major <= 4`."""
    records: list[Mapping[str, Any]] = list(baseline["deprecations"])

    candidates = _api.deprecations_lane().enumerate_candidates(
        records, simulate_config, current_major=6
    )

    assert len(records) == 4
    assert len(candidates) == baseline["totals"]["eol_passed_deprecations"] == 2
    assert {candidate.lane for candidate in candidates} == {Lane.DEPRECATIONS}
    assert {candidate.qualname for candidate in candidates} == {
        "BaseEngineSpec.normalize_indexes",
        "DatabaseRestApi.table_extra_metadata_deprecated",
    }


def test_locator_is_module_colon_qualname(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    candidates = _api.deprecations_lane().enumerate_candidates(
        list(baseline["deprecations"]), simulate_config, current_major=6
    )

    for candidate in candidates:
        assert candidate.stable_locator == f"{candidate.module}:{candidate.qualname}"


def test_removed_in_at_or_below_current_version_is_eol(simulate_config: PipelineConfig) -> None:
    """§4.2 — an explicit `removed_in` short-circuits the major-lag rule."""
    records = [
        {
            "locator": "superset.x:Y.z",
            "path": "superset/x.py",
            "qualname": "Y.z",
            "deprecated_in": "6.0.0",
            "removed_in": "6.1.0",
            "line": 10,
            "decorator_line": 9,
        }
    ]

    candidates = _api.deprecations_lane().enumerate_candidates(
        records, simulate_config, current_major=6
    )

    assert len(candidates) == 1


def test_eol_major_lag_knob_moves_the_threshold(baseline: Mapping[str, Any]) -> None:
    """§13 — the EOL lag is a knob; raising the threshold admits the 4.0 deprecation."""
    config = PipelineConfig(eol_major_lag=1, rubrics_path=RUBRICS_PATH, templates_dir=TEMPLATES_DIR)

    candidates = _api.deprecations_lane().enumerate_candidates(
        list(baseline["deprecations"]), config, current_major=6
    )

    assert {candidate.deprecated_in for candidate in candidates} == {"3.0", "4.0"}
    assert len(candidates) == 2


def test_get_url_for_impersonation_is_dropped_for_not_eol(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§4.2 — deprecated in 6.0.0 with `current_major = 6`, so it is dropped as `not_eol`."""
    lane = _api.deprecations_lane()
    records = list(baseline["deprecations"])

    candidates = lane.enumerate_candidates(records, simulate_config, current_major=6)
    dropped = lane.dropped_candidates(records, simulate_config, current_major=6)

    assert "BaseEngineSpec.get_url_for_impersonation" not in {
        candidate.qualname for candidate in candidates
    }
    reasons = {
        candidate.qualname: candidate.reason
        for candidate in dropped
        if candidate.qualname is not None
    }
    assert reasons["BaseEngineSpec.get_url_for_impersonation"] is ReasonCode.NOT_EOL
    assert reasons["BaseEngineSpec.update_impersonation_config"] is ReasonCode.NOT_EOL


def test_not_eol_is_a_distinct_reason_from_the_caller_gate(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§4.2 — the EOL drop and the human-routed caller gate are separate outcomes.

    Nothing may be dropped as `internal_caller`: the caller gate routes to a human.
    """
    lane = _api.deprecations_lane()
    records = list(baseline["deprecations"])

    dropped = lane.dropped_candidates(records, simulate_config, current_major=6)

    assert {candidate.reason for candidate in dropped} == {ReasonCode.NOT_EOL}
    assert ReasonCode.NOT_EOL.value == "not_eol"


def test_internal_caller_is_gated_not_dropped(simulate_config: PipelineConfig) -> None:
    """§4.2 — an EOL-passed site with live callers reaches the gate and fails automatability."""
    records = [
        {
            "locator": "superset.db_engine_specs.base:BaseEngineSpec.legacy_helper",
            "path": "superset/db_engine_specs/base.py",
            "qualname": "BaseEngineSpec.legacy_helper",
            "deprecated_in": "3.0",
            "removed_in": None,
            "line": 100,
            "decorator_line": 99,
            "caller_count": 4,
            "internal_caller": True,
        }
    ]

    candidates = _api.deprecations_lane().enumerate_candidates(
        records, simulate_config, current_major=6
    )
    evaluation = evaluate_gates(candidates[0], simulate_config)

    assert candidates[0].reason is not ReasonCode.NOT_EOL
    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.INTERNAL_CALLER


def test_public_rest_endpoint_is_human_routed(
    baseline: Mapping[str, Any], simulate_config: PipelineConfig
) -> None:
    """§4.2 — `table_extra_metadata_deprecated` is EOL-passed but a public API surface."""
    candidates = _api.deprecations_lane().enumerate_candidates(
        list(baseline["deprecations"]), simulate_config, current_major=6
    )
    endpoint = next(
        candidate
        for candidate in candidates
        if candidate.qualname == "DatabaseRestApi.table_extra_metadata_deprecated"
    )

    evaluation = evaluate_gates(endpoint, simulate_config)

    assert evaluation.gate_passed is False
    assert evaluation.failed_gate is GateName.AUTOMATABILITY
    assert evaluation.gate_results[GateName.AUTOMATABILITY].reason is ReasonCode.PUBLIC_API_SURFACE
