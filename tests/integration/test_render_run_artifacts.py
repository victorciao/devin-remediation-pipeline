"""§14.1 — LIVE and SIMULATE render different artifacts from the same candidate set.

`render_run_artifacts` (formerly `simulate_run`) is the only artifact writer in the pipeline, so
one LIVE run wrote fifty issue bodies each announcing "Simulated remediation for <id>" — including
for candidates that were gated or deferred and would never be published at all. LIVE must render
only what it actually routed to an issue or a PR, and describe it as the real publication it is;
SIMULATE keeps its breadth and its wording, because rendering everything is the point of a dry run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pipeline import __main__ as entrypoint
from pipeline.config import Mode, PipelineConfig
from pipeline.http_transport import HttpTransportError
from pipeline.lanes.codeql import read_alert_fixture
from pipeline.observability.events import EventLog
from pipeline.schemas import Action, Candidate, CandidateState, GateName, ReasonCode
from pipeline.simulation import render_run_artifacts, simulate_run
from pipeline.state import CandidateStateStore
from tests.conftest import FIXTURES_DIR, RUBRICS_PATH, TARGET_CHECKOUT, TEMPLATES_DIR
from tests.factories import codeql_candidate
from tests.fakes import FakeGitHubTransport

RUN_ID = "run-1"
SIMULATED_WORDING = "SIMULATED remediation for"
SIMULATED_HEADING = "### SIMULATED ARTIFACT"
SUPPRESSED_WORDING = "Writes are suppressed; no remote artifact exists."
LIVE_WORDING = "Remediation tracking for"
LIVE_STATE_FILE = "candidates-live.jsonl"
SIMULATE_STATE_FILE = "candidates.jsonl"


def config_for(mode: Mode, **fields: Any) -> PipelineConfig:  # noqa: ANN401
    """A config in `mode` pointed at the shipped rubrics, templates and alert fixture."""
    return PipelineConfig(
        mode=mode,
        rubrics_path=RUBRICS_PATH,
        templates_dir=TEMPLATES_DIR,
        alert_fixture_path=FIXTURES_DIR / "codeql_alerts.json",
        **{"ci_wait_timeout_s": 1, **fields},
    )


def routed_pr() -> Candidate:
    """A candidate a LIVE run actually published a pull request for."""
    return codeql_candidate(
        candidate_id="codeql-pr",
        gate_passed=True,
        action=Action.OPEN_PR,
        state=CandidateState.PR_CREATED,
        pr_number=1,
        pr_url="https://example.invalid/pr/1",
        issue_url="https://example.invalid/issues/1",
        base_sha="a" * 40,
        head_sha="b" * 40,
    )


def routed_issue() -> Candidate:
    """A candidate a LIVE run actually published a tracking issue for."""
    return codeql_candidate(
        candidate_id="codeql-issue",
        gate_passed=True,
        action=Action.OPEN_ISSUE,
        state=CandidateState.ISSUE_CREATED,
        issue_url="https://example.invalid/issues/2",
    )


def gated() -> Candidate:
    """A candidate no run may publish: it never passed the gates."""
    return codeql_candidate(
        candidate_id="codeql-gated",
        gate_passed=False,
        failed_gate=GateName.AUTOMATABILITY,
        action=Action.LOG_ONLY,
        state=CandidateState.GATED,
        reason=ReasonCode.AUTOMATABILITY_LOW,
    )


def budget_deferred() -> Candidate:
    """A candidate the budget pushed out of this run."""
    return codeql_candidate(
        candidate_id="codeql-budget",
        gate_passed=True,
        action=Action.DEFERRED,
        state=CandidateState.DEFERRED,
        reason=ReasonCode.BUDGET_OVERFLOW,
    )


def render(mode: Mode, output_dir: Path, **role_outputs: Any) -> tuple[Path, ...]:  # noqa: ANN401
    """Render one run over the same four candidates in `mode`."""
    return render_run_artifacts(
        [routed_pr(), routed_issue(), gated(), budget_deferred()],
        run_id=RUN_ID,
        output_dir=output_dir,
        config=config_for(mode),
        **role_outputs,
    )


def issue_bodies(output_dir: Path) -> dict[str, str]:
    """Every rendered issue body, keyed by candidate id."""
    directory = output_dir / "reports" / "issues"
    return {path.stem: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.md"))}


def test_live_renders_issue_bodies_only_for_routed_candidates(tmp_path: Path) -> None:
    """§14.1 — an artifact is a record of a publication, so an unpublished candidate has none."""
    render(Mode.LIVE, tmp_path / "out")

    assert set(issue_bodies(tmp_path / "out")) == {"codeql-pr", "codeql-issue"}


def test_live_does_not_describe_its_artifacts_as_simulated(tmp_path: Path) -> None:
    """§14.1 — the LIVE run wrote artifacts calling their own remediation simulated."""
    render(Mode.LIVE, tmp_path / "out")
    bodies = issue_bodies(tmp_path / "out")

    assert bodies != {}
    for body in bodies.values():
        assert SIMULATED_WORDING not in body
        assert SIMULATED_HEADING not in body
        assert SUPPRESSED_WORDING not in body
        assert LIVE_WORDING in body


def test_live_pr_bodies_do_not_claim_that_writes_were_suppressed(tmp_path: Path) -> None:
    """§14.1 — the PR body's automation metadata reports the mode that actually ran.

    The metadata block is the role loop's local evidence, so it is rendered from the
    implementer and reviewer outputs the loop produced for this candidate.
    """
    render(Mode.LIVE, tmp_path / "out")
    body = (tmp_path / "out" / "reports" / "prs" / "codeql-pr.md").read_text(encoding="utf-8")

    assert "**mode**: live" in body
    assert "**writes_suppressed**: False" in body
    assert "**artifact_simulated**: False" in body


def test_simulate_keeps_its_breadth_and_its_wording(tmp_path: Path) -> None:
    """§14.1 — a dry run's value is that it renders every candidate it enumerated."""
    render(Mode.SIMULATE, tmp_path / "out")
    bodies = issue_bodies(tmp_path / "out")

    assert set(bodies) == {"codeql-pr", "codeql-issue", "codeql-gated", "codeql-budget"}
    for candidate_id, body in bodies.items():
        assert f"{SIMULATED_WORDING} {candidate_id}." in body


def test_a_simulated_body_says_on_its_face_that_no_write_happened(tmp_path: Path) -> None:
    """§17 (10) — a simulated artifact read out of context must not look like a real one.

    A rendered body is a file someone can open months later with no memory of the mode it came
    from, so the heading and the suppression statement travel with the body itself.
    """
    render(Mode.SIMULATE, tmp_path / "out")
    bodies = issue_bodies(tmp_path / "out")

    assert bodies != {}
    for body in bodies.values():
        assert SIMULATED_HEADING in body
        assert SUPPRESSED_WORDING in body

    pr_body = (tmp_path / "out" / "reports" / "prs" / "codeql-pr.md").read_text(encoding="utf-8")

    assert SIMULATED_HEADING in pr_body
    assert "**writes_suppressed**: True" in pr_body
    assert "**artifact_simulated**: True" in pr_body


def test_simulated_durable_rows_are_stamped_as_simulated(tmp_path: Path) -> None:
    """§17 (10) — the durable row is what a later LIVE run reads, so it carries the stamp.

    Without it a crashed SIMULATE run's lifecycle rows are indistinguishable from a run that
    actually published, which is the dedupe hazard the stamp exists to close.
    """
    render(Mode.SIMULATE, tmp_path / "out")
    simulated = CandidateStateStore(tmp_path / "out" / "state" / SIMULATE_STATE_FILE).rows()

    assert simulated != []
    assert all(row.artifact_simulated for row in simulated)

    render(Mode.LIVE, tmp_path / "live")
    published = CandidateStateStore(tmp_path / "live" / "state" / LIVE_STATE_FILE).rows()

    assert published != []
    assert not any(row.artifact_simulated for row in published)


def test_simulate_run_is_still_the_same_callable(tmp_path: Path) -> None:
    """§14.1 — the rename keeps the old name working for existing callers."""
    assert simulate_run is render_run_artifacts

    produced = simulate_run(
        [routed_pr()],
        run_id=RUN_ID,
        output_dir=tmp_path / "out",
        config=config_for(Mode.SIMULATE),
    )

    assert all(path.is_file() for path in produced)


def test_simulate_persists_every_candidate_it_rendered(tmp_path: Path) -> None:
    """§14.1 — SIMULATE's durable rows cover the whole enumerated set, unchanged."""
    render(Mode.SIMULATE, tmp_path / "out")
    store = CandidateStateStore(tmp_path / "out" / "state" / SIMULATE_STATE_FILE)

    assert {row.candidate_id for row in store.rows()} == {
        "codeql-pr",
        "codeql-issue",
        "codeql-gated",
        "codeql-budget",
    }


class FailingDevinTransport:
    """A Devin transport whose sessions are unavailable, so the role loop cannot run."""

    def post(self, path: str, payload: Any) -> Any:  # noqa: ANN401, ARG002
        raise HttpTransportError("sessions unavailable", status_code=503)

    def get(self, path: str) -> Any:  # noqa: ANN401, ARG002
        raise HttpTransportError("sessions unavailable", status_code=503)


def baseline_file(tmp_path: Path) -> Path:
    """A one-lane baseline: LANE 1 alone is enough to enumerate a candidate set."""
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


def test_a_live_run_records_a_durable_row_for_every_enumerated_candidate(
    tmp_path: Path,
) -> None:
    """§14.1 — a candidate absent from state is a candidate a later run cannot account for.

    LIVE writes no rows from the artifact renderer, so gated and budget-deferred candidates
    were enumerated, reported, and then forgotten: the next run rediscovered them from
    scratch and the run's own state could not be reconciled against its report.
    """
    transport = FakeGitHubTransport(
        base_sha="1" * 40,
        code_scanning_alerts=read_alert_fixture(FIXTURES_DIR / "codeql_alerts.json"),
        completed_workflow_runs=True,
    )
    output_dir = tmp_path / "out"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(entrypoint, "UrllibGitHubTransport", lambda: transport)
        patch.setattr(entrypoint, "UrllibDevinTransport", FailingDevinTransport)
        entrypoint.run_once(
            config=config_for(Mode.LIVE, budget_N=1),
            repo_path=TARGET_CHECKOUT,
            output_dir=output_dir,
            baseline_path=baseline_file(tmp_path),
            base_sha="1" * 40,
            head_branch="devin/durable-rows",
            base_branch="master",
        )

    events = EventLog(output_dir / "reports" / "events.jsonl").read()
    enumerated = {event.candidate_id for event in events}
    store = CandidateStateStore(output_dir / "state" / LIVE_STATE_FILE)
    persisted = {row.candidate_id for row in store.rows()}

    assert enumerated != set()
    assert enumerated <= persisted
    assert ReasonCode.BUDGET_OVERFLOW in {
        row.reason for row in store.rows() if row.state is CandidateState.DEFERRED
    }


def test_live_persists_the_candidates_it_renders_no_artifact_for(tmp_path: Path) -> None:
    """§14.1 — a candidate with no artifact still needs a row: it was still enumerated.

    Filtering LIVE artifacts to routed candidates must not also filter durable state, or a
    gated or budget-deferred candidate is reported once and then forgotten, and the next run
    rediscovers it from scratch with no record of why this run declined it.
    """
    render(Mode.LIVE, tmp_path / "out")
    store = CandidateStateStore(tmp_path / "out" / "state" / LIVE_STATE_FILE)
    rows = {row.candidate_id: row for row in store.rows()}

    assert set(rows) == {"codeql-pr", "codeql-issue", "codeql-gated", "codeql-budget"}
    assert rows["codeql-gated"].state is CandidateState.GATED
    assert rows["codeql-budget"].state is CandidateState.DEFERRED
    assert rows["codeql-budget"].reason is ReasonCode.BUDGET_OVERFLOW
    assert set(issue_bodies(tmp_path / "out")) == {"codeql-pr", "codeql-issue"}
