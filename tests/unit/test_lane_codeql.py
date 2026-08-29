"""§5 LANE 1 — enumeration from the captured alert fixture, scope gating and co-location."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from pipeline.config import AlertSource, PipelineConfig
from pipeline.gate import evaluate_gates
from pipeline.schemas import Candidate, GateName, Lane, ReasonCode
from tests import _api
from tests.conftest import FIXTURES_DIR, RUBRICS_PATH, TEMPLATES_DIR

COLOCATED_PATH = "superset/mcp_service/dashboard/tool/add_chart_to_existing_dashboard.py"
FRONTEND_RULES = {"js/xss", "js/xss-through-exception", "js/clear-text-storage-of-sensitive-data"}


@pytest.fixture
def fixture_config() -> PipelineConfig:
    """SIMULATE with the lane reading the committed alert fixture instead of the API."""
    return PipelineConfig(
        alert_source=AlertSource.SARIF_FILE,
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
    )


def enumerate_from_fixture(
    alerts: list[Mapping[str, Any]], config: PipelineConfig
) -> list[Candidate]:
    return _api.codeql_lane().enumerate_candidates(alerts, config)


def test_load_alerts_reads_the_captured_fixture() -> None:
    alerts = _api.codeql_lane().load_alerts(FIXTURES_DIR / "codeql_alerts.json")

    assert len(alerts) == 11
    assert all(alert["state"] == "open" for alert in alerts)


def test_every_alert_becomes_a_candidate_in_the_codeql_lane(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)

    assert len(candidates) == len(codeql_alerts)
    assert {candidate.lane for candidate in candidates} == {Lane.CODEQL}
    assert len({candidate.candidate_id for candidate in candidates}) == len(codeql_alerts)


def test_locator_never_uses_the_unstable_alert_number(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§14.1 — the locator is rule + path + symbol + position digest, never `alert.number`."""
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)

    for candidate in candidates:
        assert candidate.rule_id is not None
        assert candidate.file_path is not None
        assert candidate.position_digest is not None
        assert candidate.stable_locator.startswith(candidate.rule_id)
        assert candidate.file_path in candidate.stable_locator
        assert candidate.position_digest in candidate.stable_locator
        assert str(candidate.alert_number) not in candidate.stable_locator.split("|")


def test_freshness_signal_is_the_alert_updated_at(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§5 — the scheduled cron is not a trigger anchor; `updated_at` is."""
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)

    assert all(candidate.updated_at is not None for candidate in candidates)


def test_codeql_locator_separates_colocated_alerts(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§17 — the four `py/overly-large-range` alerts on line 55 yield four distinct ids."""
    candidates = [
        candidate
        for candidate in enumerate_from_fixture(codeql_alerts, fixture_config)
        if candidate.rule_id == "py/overly-large-range"
    ]

    assert len(candidates) == 4
    assert {candidate.file_path for candidate in candidates} == {COLOCATED_PATH}
    assert len({candidate.candidate_id for candidate in candidates}) == 4
    assert len({candidate.position_digest for candidate in candidates}) == 4
    assert len({candidate.stable_locator for candidate in candidates}) == 4


def test_colocated_alerts_all_dispatched(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§17 — none of the four is suppressed as a drift match; their weak key multiplicity is 4."""
    dedupe = _api.dedupe()
    scan = enumerate_from_fixture(codeql_alerts, fixture_config)
    colocated = [row for row in scan if row.rule_id == "py/overly-large-range"]
    prior = colocated[0].model_copy(update={"candidate_id": "codeql-prior"})

    weak_keys = [(row.rule_id, row.file_path, row.normalized_symbol) for row in colocated]
    assert len(set(weak_keys)) == 1
    assert weak_keys.count(weak_keys[0]) == 4

    for row in colocated:
        match = dedupe.drift_match(row, scan=scan, state_rows=[prior])
        assert match.linked is False, row.candidate_id


def test_frontend_alerts_gated_out(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§5/§17 — the three JS/TS alerts fail `verifiability_exists` as `out_of_scope_frontend`."""
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)
    frontend = [row for row in candidates if row.rule_id in FRONTEND_RULES]

    assert len(frontend) == 3
    for row in frontend:
        evaluation = evaluate_gates(row, fixture_config)
        assert evaluation.gate_passed is False
        assert evaluation.failed_gate is GateName.VERIFIABILITY_EXISTS
        result = evaluation.gate_results[GateName.VERIFIABILITY_EXISTS]
        assert result.passed is False
        assert result.reason is ReasonCode.OUT_OF_SCOPE_FRONTEND


def test_python_alerts_stay_in_scope(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)
    python_rows = [row for row in candidates if row.rule_id not in FRONTEND_RULES]

    assert len(python_rows) == 8
    for row in python_rows:
        assert row.file_path is not None and row.file_path.endswith(".py")
        evaluation = evaluate_gates(row, fixture_config)
        assert evaluation.failed_gate is not GateName.VERIFIABILITY_EXISTS
        assert ReasonCode.OUT_OF_SCOPE_FRONTEND not in {
            result.reason for result in evaluation.gate_results.values()
        }


def test_business_impact_comes_from_security_severity(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§4.1 — the CodeQL rubric anchors `business_impact` on the alert severity."""
    candidates = enumerate_from_fixture(codeql_alerts, fixture_config)

    for candidate in candidates:
        assert candidate.security_severity_level is not None
        assert candidate.business_impact is not None
        assert 1 <= candidate.business_impact <= 5
