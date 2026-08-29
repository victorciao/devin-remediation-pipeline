"""§5 LANE 1 — enumeration from the captured alert fixture, scope gating and co-location."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from pipeline.config import AlertSource, PipelineConfig
from pipeline.dedupe import find_drift_match, weak_key
from pipeline.gate import evaluate_gates
from pipeline.lanes.codeql import (
    candidate_id,
    enumerate_codeql_candidates,
    enumerate_from_config,
    position_digest,
    read_alert_fixture,
)
from pipeline.rubric import resolve_factors
from pipeline.schemas import Candidate, GateName, Lane, ReasonCode
from tests.conftest import FIXTURES_DIR, RUBRICS_PATH, TARGET_CHECKOUT, TARGET_REPO, TEMPLATES_DIR

COLOCATED_PATH = "superset/mcp_service/dashboard/tool/add_chart_to_existing_dashboard.py"
FRONTEND_RULES = {"js/xss", "js/xss-through-exception", "js/clear-text-storage-of-sensitive-data"}
ALERT_FIXTURE = FIXTURES_DIR / "codeql_alerts.json"


@pytest.fixture
def fixture_config() -> PipelineConfig:
    """SIMULATE with the lane reading the committed alert fixture instead of the API."""
    return PipelineConfig(
        alert_source=AlertSource.SARIF_FILE,
        alert_fixture_path=ALERT_FIXTURE,
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
    )


def enumerate_fixture(alerts: object, *, repo_path: Path | None = None) -> list[Candidate]:
    return enumerate_codeql_candidates(alerts, TARGET_REPO, repo_path=repo_path)


def test_load_alerts_reads_the_captured_fixture() -> None:
    alerts = cast(Sequence[Mapping[str, Any]], read_alert_fixture(ALERT_FIXTURE))

    assert len(alerts) == 11
    assert all(alert["state"] == "open" for alert in alerts)


def test_every_alert_becomes_a_candidate_in_the_codeql_lane(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    candidates = enumerate_fixture(codeql_alerts)

    assert len(candidates) == len(codeql_alerts)
    assert {candidate.lane for candidate in candidates} == {Lane.CODEQL}
    assert len({candidate.candidate_id for candidate in candidates}) == len(codeql_alerts)
    assert all(candidate.trigger_exists is True for candidate in candidates)


def test_locator_never_uses_the_unstable_alert_number(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    """§14.1 — the locator is rule + path + symbol + position digest, never `alert.number`."""
    candidates = enumerate_fixture(codeql_alerts)

    for candidate in candidates:
        assert candidate.rule_id is not None
        assert candidate.file_path is not None
        assert candidate.position_digest is not None
        assert candidate.stable_locator.split("|") == [
            candidate.rule_id,
            candidate.file_path,
            candidate.normalized_symbol,
            candidate.position_digest,
        ]
        assert str(candidate.alert_number) not in candidate.stable_locator.split("|")
        assert candidate.candidate_id == candidate_id(TARGET_REPO, candidate.stable_locator)


def test_freshness_signal_is_the_alert_updated_at(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    """§5 — the scheduled cron is not a trigger anchor; `updated_at` is."""
    cutoff = datetime(2026, 8, 28, tzinfo=UTC)

    fresh = enumerate_codeql_candidates(codeql_alerts, TARGET_REPO, freshness_cutoff=cutoff)
    stale = enumerate_codeql_candidates(
        codeql_alerts, TARGET_REPO, freshness_cutoff=datetime(2027, 1, 1, tzinfo=UTC)
    )

    assert all(candidate.updated_at is not None for candidate in fresh)
    assert all(candidate.updated_at_fresh is True for candidate in fresh)
    assert all(candidate.updated_at_fresh is False for candidate in stale)


def test_codeql_locator_separates_colocated_alerts(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    """§17 — the four `py/overly-large-range` alerts on line 55 yield four distinct ids."""
    candidates = [
        candidate
        for candidate in enumerate_fixture(codeql_alerts)
        if candidate.rule_id == "py/overly-large-range"
    ]

    assert len(candidates) == 4
    assert {candidate.file_path for candidate in candidates} == {COLOCATED_PATH}
    assert {candidate.line for candidate in candidates} == {55}
    assert len({candidate.candidate_id for candidate in candidates}) == 4
    assert len({candidate.position_digest for candidate in candidates}) == 4
    assert len({candidate.stable_locator for candidate in candidates}) == 4


def test_position_digest_covers_the_columns(codeql_alerts: list[Mapping[str, Any]]) -> None:
    """§14.1 — co-located alerts differ only in their columns, so the digest must include them."""
    locations = [
        alert["most_recent_instance"]["location"]
        for alert in codeql_alerts
        if alert["rule"]["id"] == "py/overly-large-range"
    ]

    assert {location["start_column"] for location in locations} == {6, 9, 12, 15}
    digests = {position_digest(location) for location in locations}
    assert len(digests) == 4
    assert all(len(digest) == 12 for digest in digests)


def test_colocated_alerts_all_dispatched(codeql_alerts: list[Mapping[str, Any]]) -> None:
    """§17 — none of the four is suppressed as a drift match; their weak-key multiplicity is 4."""
    scan = enumerate_fixture(codeql_alerts)
    colocated = [row for row in scan if row.rule_id == "py/overly-large-range"]
    prior = colocated[0].model_copy(
        update={"candidate_id": "codeql-prior", "position_digest": "ffffffffffff"}
    )

    assert len({weak_key(row) for row in colocated}) == 1

    for row in colocated:
        assert find_drift_match([prior], row, current_scan=scan) is None, row.candidate_id


def test_frontend_alerts_gated_out(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§5/§17 — the three JS/TS alerts fail `verifiability_exists` as `out_of_scope_frontend`."""
    frontend = [row for row in enumerate_fixture(codeql_alerts) if row.rule_id in FRONTEND_RULES]

    assert len(frontend) == 3
    for row in frontend:
        factors = resolve_factors(row, fixture_config)
        evaluation = evaluate_gates(row, fixture_config, resolved_factors=factors)
        assert evaluation.gate_passed is False
        assert evaluation.failed_gate is GateName.VERIFIABILITY_EXISTS
        result = evaluation.gate_results[GateName.VERIFIABILITY_EXISTS]
        assert result.passed is False
        assert result.reason is ReasonCode.OUT_OF_SCOPE_FRONTEND


def test_python_alerts_stay_in_scope(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    python_rows = [
        row for row in enumerate_fixture(codeql_alerts) if row.rule_id not in FRONTEND_RULES
    ]

    assert len(python_rows) == 8
    for row in python_rows:
        assert row.file_path is not None and row.file_path.endswith(".py")
        factors = resolve_factors(row, fixture_config)
        evaluation = evaluate_gates(row, fixture_config, resolved_factors=factors)
        assert evaluation.failed_gate is not GateName.VERIFIABILITY_EXISTS
        assert ReasonCode.OUT_OF_SCOPE_FRONTEND not in {
            result.reason for result in evaluation.gate_results.values()
        }


def test_business_impact_comes_from_security_severity(
    codeql_alerts: list[Mapping[str, Any]], fixture_config: PipelineConfig
) -> None:
    """§4.1 — the CodeQL rubric anchors `business_impact` on the alert severity."""
    for candidate in enumerate_fixture(codeql_alerts):
        assert candidate.security_severity_level is not None
        factors = resolve_factors(candidate, fixture_config)
        assert 1 <= factors.business_impact <= 5


def test_enumeration_from_config_uses_the_fixture_source(fixture_config: PipelineConfig) -> None:
    """§5 — with `alert_source = sarif_file` the lane never needs an API reader."""
    candidates = enumerate_from_config(fixture_config, repo_path=TARGET_CHECKOUT, repo=TARGET_REPO)

    assert len(candidates) == 11
    assert {candidate.repo for candidate in candidates} == {TARGET_REPO}


def test_normalized_symbol_comes_from_the_target_source(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    """§14.1 — the locator's symbol is AST-derived, so the drift key survives line shifts."""
    assert (TARGET_CHECKOUT / COLOCATED_PATH).is_file()

    candidates = enumerate_fixture(codeql_alerts, repo_path=TARGET_CHECKOUT)
    by_rule = {row.rule_id: row for row in candidates if row.rule_id == "py/stack-trace-exposure"}
    enclosed = by_rule["py/stack-trace-exposure"]

    assert enclosed.symbol_source == "ast"
    assert enclosed.normalized_symbol not in (None, "<module>")
    assert enclosed.symbol_relative_offset is not None and enclosed.symbol_relative_offset > 0

    colocated = [row for row in candidates if row.rule_id == "py/overly-large-range"]

    assert {row.symbol_source for row in colocated} == {"module_fallback"}
    assert {row.normalized_symbol for row in colocated} == {"<module>"}
    assert {row.symbol_relative_offset for row in colocated} == {0}
    assert len({row.candidate_id for row in colocated}) == 4
    assert all(row.region_digest is not None for row in colocated)


def test_region_digest_is_read_through_an_injected_reader(
    codeql_alerts: list[Mapping[str, Any]],
) -> None:
    """§14.1 — region text is injected, so the lane never needs the network or a checkout."""
    reads: list[tuple[str, int, int]] = []

    def reader(path: str, start_line: int, end_line: int) -> str:
        reads.append((path, start_line, end_line))
        return "for index in range(0, 10000):"

    candidates = enumerate_codeql_candidates(codeql_alerts, TARGET_REPO, region_reader=reader)

    assert len(reads) == len(candidates)
    assert len({candidate.region_digest for candidate in candidates}) == 1
    assert {candidate.region_source for candidate in candidates} == {"source_region"}


def test_alert_without_a_rule_id_is_rejected() -> None:
    """§5 — a malformed alert is an error, not a silently dropped row."""
    with pytest.raises(ValueError):
        enumerate_codeql_candidates(
            [{"most_recent_instance": {"location": {"path": "superset/x.py"}}}], TARGET_REPO
        )
