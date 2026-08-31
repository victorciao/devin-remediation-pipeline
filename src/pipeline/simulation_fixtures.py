"""Deterministic session-output fixtures for credential-free simulation."""

from __future__ import annotations

from collections.abc import Sequence

from pipeline.schemas import Candidate, ItemOutcome, Lane, PerItemOutcome
from pipeline.verify import (
    AlertObservation,
    ItemRunResult,
    Observers,
    SkipMarkerObservation,
    SuiteResult,
    SymbolObservation,
)


def simulated_fix_output(candidate: Candidate) -> dict[str, object]:
    """Return the structured output a simulated remediation session would report."""
    test_nodeid = candidate.nodeid if candidate.lane is Lane.SKIPPED_TESTS else None
    test_paths = (
        [candidate.nodeid.split("::", 1)[0]]
        if candidate.lane is Lane.SKIPPED_TESTS and candidate.nodeid
        else []
    )
    suite_scope = list(candidate.suite_scope) or (
        [candidate.file_path] if candidate.file_path else ["tests/"]
    )
    verify_command = (
        f"pytest {test_nodeid}" if test_nodeid is not None else "pytest " + suite_scope[0]
    )
    return {
        "files_changed": [candidate.file_path or "superset/simulated.py"],
        "test_nodeid": test_nodeid,
        "test_paths": test_paths,
        "verify_command": verify_command,
        "head_sha": candidate.head_sha or f"simulated-head-{candidate.candidate_id[:12]}",
        "suite_scope": suite_scope,
        "fix_summary": f"Simulated remediation for {candidate.stable_locator}.",
        "testing_notes": "Simulated run: no session was created and no command was executed.",
        "criterion_notes": "Simulated run: the criterion is not observed in SIMULATE.",
        "feasible": True,
        "infeasible_reason": None,
    }


def simulated_observers(*, base_sha: str) -> Observers:
    """Return observation seams that stand in for real execution in SIMULATE.

    Every command string is labelled ``SIMULATED`` so no report can read as an
    orchestrator observation of the live fork.
    """

    def run_item(sha: str, nodeid: str) -> ItemRunResult:
        red = sha == base_sha
        return ItemRunResult(
            outcomes=(
                PerItemOutcome(
                    nodeid=nodeid,
                    outcome=ItemOutcome.FAILED if red else ItemOutcome.PASSED,
                    exception_type="AssertionError" if red else None,
                    message="simulated red baseline" if red else None,
                ),
            ),
            command=f"SIMULATED pytest {nodeid} at {sha}",
        )

    def run_suite(scope: Sequence[str], sha: str) -> SuiteResult:
        return SuiteResult(
            passed=True,
            command=f"SIMULATED pytest {' '.join(scope) or 'tests/'} at {sha}",
        )

    def run_item_with_test_diff(
        base: str,
        head: str,
        nodeid: str,
        paths: Sequence[str],
    ) -> ItemRunResult:
        del head, paths
        return run_item(base, nodeid)

    def probe_symbol(target: Candidate, sha: str) -> SymbolObservation:
        return SymbolObservation(
            resolves=False,
            caller_count=0,
            override_count=0,
            command=f"SIMULATED symbol re-check of {target.stable_locator} at {sha}",
        )

    def probe_alerts(target: Candidate, sha: str) -> AlertObservation:
        return AlertObservation(
            locators=(),
            command=f"SIMULATED alert re-read for {target.candidate_id} at {sha}",
        )

    def probe_skip_marker(target: Candidate, sha: str) -> SkipMarkerObservation:
        return SkipMarkerObservation(
            present=False,
            command=f"SIMULATED skip-marker probe for {target.candidate_id} at {sha}",
        )

    def read_ci_suite(sha: str, check_context: str) -> SuiteResult:
        return SuiteResult(
            passed=True,
            command=f"SIMULATED check-runs context={check_context} at {sha}",
            conclusion="success",
        )

    return Observers(
        run_item=run_item,
        run_item_with_test_diff=run_item_with_test_diff,
        run_suite=run_suite,
        probe_symbol=probe_symbol,
        probe_alerts=probe_alerts,
        probe_skip_marker=probe_skip_marker,
        read_ci_suite=read_ci_suite,
    )


__all__ = ["simulated_fix_output", "simulated_observers"]
