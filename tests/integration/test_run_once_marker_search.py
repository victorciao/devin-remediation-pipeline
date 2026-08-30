"""§14.1 — a configured marker search that fails is a loud run failure, not a silent no-op.

The first real LIVE run against `victorciao/superset` sent every marker lookup into an HTTP 422
and then reported a clean, quiet run: dedupe was unavailable, so nothing could be published, yet
the CLI exited 0. `run_once` must now still persist everything it observed — run report, KPI
rollup, rendered artifacts, durable rows, and a Layer 1 `marker_search_failure` event — and only
then abort, while an *unconfigured* marker search (SIMULATE) stays a normal successful run.

The whole entrypoint is driven here rather than the helpers: the defect was that nothing turned an
unavailable capability into a non-zero exit, which no helper can be asked about.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline import __main__ as entrypoint
from pipeline.config import Mode, PipelineConfig
from pipeline.http_transport import HttpTransportError
from pipeline.lanes.codeql import read_alert_fixture
from pipeline.observability.events import EventLog
from pipeline.schemas import CandidateState, ReasonCode, RunEventRecord
from tests.conftest import FIXTURES_DIR, RUBRICS_PATH, TARGET_CHECKOUT, TEMPLATES_DIR
from tests.fakes import FakeGitHubTransport, WriteRecord

MARKER_SEARCH_FAILED = "marker_search_failed"
CAPABILITY_NOTE = (
    "marker search failed; no candidate can be dispatched while dedupe capability is unavailable"
)


class FakeDevinTransport:
    """A Devin transport that fails loudly if a deferring run ever reaches a session."""

    def post(self, path: str, payload: Any) -> Any:  # noqa: ANN401
        raise AssertionError(f"no session may be created while dedupe is unavailable: {path}")

    def get(self, path: str) -> Any:  # noqa: ANN401
        raise AssertionError(f"no session may be polled while dedupe is unavailable: {path}")


@dataclass(frozen=True)
class AbortedRun:
    """What one aborted LIVE run left behind."""

    output_dir: Path
    message: str
    writes: list[WriteRecord]

    def report(self) -> str:
        """The Layer 2 run report written before the abort."""
        return next(
            path.read_text(encoding="utf-8")
            for path in (self.output_dir / "reports").glob("run-*.md")
        )

    def rows(self) -> list[dict[str, Any]]:
        """Every durable candidate row written before the abort."""
        path = self.output_dir / "state" / "candidates.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def events(self) -> list[RunEventRecord]:
        """Every Layer 1 run-level event written before the abort."""
        return EventLog(self.output_dir / "reports" / "events.jsonl").read_run_events()


def baseline_file(tmp_path: Path) -> Path:
    """A one-lane baseline: LANE 1 alone is enough to reach the marker search."""
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "repo": "victorciao/superset",
                "baseline_valid_lanes": ["codeql"],
                "current_major": 6,
                "current_release": "6.1.0",
            }
        ),
        encoding="utf-8",
    )
    return path


def config_for(mode: Mode, **fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A config in `mode` pointed at the shipped rubrics, templates and alert fixture."""
    return PipelineConfig(
        mode=mode,
        github_token=SecretStr("placeholder-token"),
        devin_api_key=SecretStr("placeholder-key"),
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        alert_fixture_path=FIXTURES_DIR / "codeql_alerts.json",
        **{"ci_wait_timeout_s": 1, **fields},
    )


@pytest.fixture(scope="module")
def aborted_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[AbortedRun]:
    """One LIVE run whose configured marker search fails on every lookup.

    The run is executed once for the module: it is the assertions about what survived the
    abort that differ, not the run.
    """
    tmp_path = tmp_path_factory.mktemp("marker-search-failure")
    transport = FakeGitHubTransport(
        marker_search_error=HttpTransportError("Validation Failed", status_code=422),
        code_scanning_alerts=read_alert_fixture(FIXTURES_DIR / "codeql_alerts.json"),
    )
    output_dir = tmp_path / "out"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(entrypoint, "UrllibGitHubTransport", lambda: transport)
        patch.setattr(entrypoint, "UrllibDevinTransport", FakeDevinTransport)
        with pytest.raises(entrypoint.RunAbort) as raised:
            entrypoint.run_once(
                config=config_for(Mode.LIVE),
                repo_path=TARGET_CHECKOUT,
                output_dir=output_dir,
                baseline_path=baseline_file(tmp_path),
                base_sha="1" * 40,
                head_branch="devin/marker-search",
                base_branch="master",
            )
    yield AbortedRun(output_dir, str(raised.value), transport.writes)


def test_a_failing_live_marker_search_aborts_the_run(aborted_run: AbortedRun) -> None:
    """§14.1 — dedupe is a precondition for dispatch, so its failure fails the run."""
    assert MARKER_SEARCH_FAILED in aborted_run.message
    assert ReasonCode.CAPABILITY_UNAVAILABLE.value in aborted_run.message


def test_the_run_report_survives_the_abort(aborted_run: AbortedRun) -> None:
    """§11 — the abort happens after reporting: the evidence of the failed run is on disk."""
    report = aborted_run.report()

    assert CAPABILITY_NOTE in report
    assert "## Dispatched by tier\n- None\n" in report


def test_the_kpi_rollup_survives_the_abort(aborted_run: AbortedRun) -> None:
    """§11 — the rollup is written too, and reports nothing as dispatched."""
    rollup = (aborted_run.output_dir / "reports" / "kpis.md").read_text(encoding="utf-8")

    assert "**Dispatched Pr:** 0" in rollup
    assert "**Dispatched Issue:** 0" in rollup


def test_the_rendered_artifacts_survive_the_abort(aborted_run: AbortedRun) -> None:
    """§11 — rendered issue and PR bodies are written before the abort, not lost with it."""
    issues = sorted((aborted_run.output_dir / "reports" / "issues").glob("*.md"))
    prs = sorted((aborted_run.output_dir / "reports" / "prs").glob("*.md"))

    assert issues != []
    assert prs != []
    assert all(path.read_text(encoding="utf-8").strip() != "" for path in [*issues, *prs])


def test_the_durable_state_rows_survive_the_abort(aborted_run: AbortedRun) -> None:
    """§14.1 — every routed candidate defers durably, so a later run resumes rather than repeats."""
    rows = aborted_run.rows()
    deferred = [row for row in rows if row["state"] == CandidateState.DEFERRED.value]

    assert rows != []
    assert deferred != []
    assert all(
        row["reason"] == ReasonCode.CAPABILITY_UNAVAILABLE.value
        for row in deferred
        if row["action"] == "open_pr"
    )


def test_no_candidate_is_reported_dispatched_after_the_abort(aborted_run: AbortedRun) -> None:
    """§11 — the previous LIVE run claimed a dispatched PR it had only deferred."""
    latest = {row["candidate_id"]: row for row in aborted_run.rows()}

    assert [row for row in latest.values() if row["pr_url"] is not None] == []
    assert [row for row in latest.values() if row["issue_url"] is not None] == []


def test_a_marker_search_failure_event_records_the_reason(aborted_run: AbortedRun) -> None:
    """§11 Layer 1 — the failure is one run-level event, with its reason and detail."""
    failures = [
        event for event in aborted_run.events() if event.event_type == "marker_search_failure"
    ]

    assert len(failures) == 1
    assert failures[0].reason_detail == MARKER_SEARCH_FAILED
    assert failures[0].transition_reason is ReasonCode.CAPABILITY_UNAVAILABLE


def test_the_abort_publishes_nothing(aborted_run: AbortedRun) -> None:
    """§14.1 — fail-closed: an unavailable dedupe capability performs no remote write."""
    assert aborted_run.writes == []


def test_an_unconfigured_marker_search_completes_normally(tmp_path: Path) -> None:
    """§14.1 — SIMULATE configures no marker search, so there is nothing to fail: exit 0."""
    output_dir = tmp_path / "out"

    _run_id, produced = entrypoint.run_once(
        config=config_for(Mode.SIMULATE),
        repo_path=tmp_path / "nonexistent-target",
        output_dir=output_dir,
        baseline_path=baseline_file(tmp_path),
        base_sha="1" * 40,
    )

    events = EventLog(output_dir / "reports" / "events.jsonl").read_run_events()

    assert produced != ()
    assert list((output_dir / "reports").glob("run-*.md")) != []
    assert [event for event in events if event.event_type == "marker_search_failure"] == []
