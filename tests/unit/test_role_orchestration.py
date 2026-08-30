"""§9.3 phase B: the reviewer is handed the implementer's diff on its existing session.

Before phase B existed the reviewer never saw the implementer's diff, so `diff_reviewed`
could not become true and every candidate escalated; and a retry reran at the same attempt,
where the idempotent create returned the same session and made the retry a no-op.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest
from pydantic import SecretStr

from pipeline.config import Mode, PipelineConfig
from pipeline.schemas import Candidate, ReasonCode
from pipeline.session_client import (
    PlannerOutputError,
    RuntimeOrchestrator,
    SessionClient,
    SessionMessageError,
)
from tests.factories import codeql_candidate

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
HEAD_BRANCH = "devin/codeql-0"
TARGET_REPO = "victorciao/superset"
CRITERION = "AC-1"
NODEID = "tests/unit_tests/db_engine_specs/test_base.py::test_normalize_indexes"
PRODUCTION_PATH = "superset/db_engine_specs/base.py"
IMPLEMENTER_DIFF = f"""\
--- a/{PRODUCTION_PATH}
+++ b/{PRODUCTION_PATH}
@@
-        return None
+        return indexes
"""
REVIEWER_DIFF = """\
--- a/tests/unit_tests/db_engine_specs/test_base.py
+++ b/tests/unit_tests/db_engine_specs/test_base.py
@@
-    assert normalize_indexes(indexes) == []
+    assert normalize_indexes(indexes) == [{"column_names": ["a"]}]
"""

PLANNER_OUTPUT: Mapping[str, object] = {
    "criteria": [
        {
            "id": CRITERION,
            "statement": "normalize_indexes returns the parsed indexes",
            "verify_command": "pytest -q tests/unit_tests/db_engine_specs/test_base.py",
            "expected_failure": {
                "nodeid": NODEID,
                "exception_type": "AssertionError",
                "message_pattern": "normalize_indexes returned None",
            },
        }
    ],
    "files_in_scope": [PRODUCTION_PATH],
    "out_of_scope": ["tests/"],
}


def implementer_output() -> Mapping[str, object]:
    """A production-only implementer payload that addresses the single criterion."""
    return {
        "files_changed": [PRODUCTION_PATH],
        "criteria_addressed": [CRITERION],
        "commands_run": ["pytest -q tests/unit_tests/db_engine_specs/test_base.py"],
        "committed_diff": IMPLEMENTER_DIFF,
    }


def reviewer_output(
    *, diff_reviewed: bool, red_baseline_valid: bool = True
) -> Mapping[str, object]:
    """A reviewer payload whose `diff_reviewed` evidence is present only after phase B."""
    review: Mapping[str, object] | bool = (
        {
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "files_read": [PRODUCTION_PATH],
        }
        if diff_reviewed
        else False
    )
    return {
        "tests": [
            {
                "path": "tests/unit_tests/db_engine_specs/test_base.py",
                "nodeid": NODEID,
                "criterion_id": CRITERION,
            }
        ],
        "red_baseline": {
            "observed": {
                "per_item_outcomes": [
                    {
                        "nodeid": NODEID,
                        "outcome": "FAILED",
                        "exception_type": "AssertionError",
                        "message": (
                            "AssertionError: normalize_indexes returned None"
                            if red_baseline_valid
                            else "ImportError: no such module"
                        ),
                    }
                ]
            }
        },
        "green_result": {"passed": True},
        "diff_reviewed": review,
        "committed_diff": REVIEWER_DIFF,
        "findings": [],
    }


class ScriptedDevinTransport:
    """A Devin API stand-in keyed by role tag: no network, and every call is recorded."""

    def __init__(
        self,
        *,
        reviews_after_message: bool = True,
        invalid_baseline_attempts: frozenset[int] = frozenset(),
        message_detail: str = "queued",
        acknowledges_message: bool = True,
    ) -> None:
        self.reviews_after_message = reviews_after_message
        self.message_detail = message_detail
        self.acknowledges_message = acknowledges_message
        self._acknowledged: set[str] = set()
        self.invalid_baseline_attempts = invalid_baseline_attempts
        self.created: list[tuple[str, int]] = []
        self.messaged: list[tuple[str, str]] = []
        self.polled: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self._reviewed: set[str] = set()

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Serve session creation and the phase-B follow-up message."""
        if path.endswith("/message"):
            session_id = path.split("/")[3]
            message = payload["message"]
            assert isinstance(message, str)
            self.messaged.append((session_id, message))
            self.calls.append(("message", session_id))
            if self.reviews_after_message:
                self._reviewed.add(session_id)
            if self.acknowledges_message:
                self._acknowledged.add(session_id)
            return {"detail": self.message_detail}
        tags = payload["tags"]
        assert isinstance(tags, list)
        role = str(tags[2])
        attempt = int(str(tags[3]).removeprefix("attempt:"))
        self.created.append((role, attempt))
        return {"session_id": f"{role}-{attempt}", "is_new_session": True}

    def get(self, path: str) -> Mapping[str, object]:
        """Return the finished snapshot for whichever role owns the polled session."""
        session_id = path.rsplit("/", 1)[-1]
        self.polled.append(session_id)
        self.calls.append(("poll", session_id))
        role = session_id.rsplit("-", 1)[0]
        if role == "planner":
            output: Mapping[str, object] = PLANNER_OUTPUT
        elif role == "implementer":
            output = implementer_output()
        else:
            attempt = int(session_id.rsplit("-", 1)[-1])
            reviewed = reviewer_output(
                diff_reviewed=session_id in self._reviewed,
                red_baseline_valid=attempt not in self.invalid_baseline_attempts,
            )
            output = (
                {**reviewed, "phase_b_acknowledged": True}
                if session_id in self._acknowledged
                else reviewed
            )
        return {
            "status_enum": "finished",
            "acu_used": 1.0,
            "session_id": session_id,
            "structured_output": output,
        }


def candidate() -> Candidate:
    """The candidate whose branch the roles work on."""
    return codeql_candidate(
        candidate_id="codeql-0",
        base_sha=BASE_SHA,
        head_sha=HEAD_SHA,
        head_branch=HEAD_BRANCH,
    )


def phase_b_prompt(committed_diff: str) -> str:
    """Stand in for `render_reviewer_phase_b_prompt`, which embeds the diff itself."""
    return "REVIEWER PHASE B PROMPT\n\nIMPLEMENTER COMMITTED DIFF:\n" + committed_diff


def prompt_factory(
    planner_output: Mapping[str, object],
) -> tuple[str, str, Callable[[str], str]]:
    """Stand in for `__main__`'s renderer wiring; the planner output must arrive here."""
    assert planner_output == PLANNER_OUTPUT
    return (
        "IMPLEMENTER PROMPT",
        "REVIEWER PROMPT",
        phase_b_prompt,
    )


def orchestrator(
    transport: ScriptedDevinTransport | None,
    *,
    mode: Mode = Mode.LIVE,
    **overrides: Any,  # noqa: ANN401
) -> RuntimeOrchestrator:
    """Build an orchestrator over a scripted transport with no real clock."""
    config = PipelineConfig(
        mode=mode,
        github_token=SecretStr("placeholder-github-token"),
        devin_api_key=SecretStr("placeholder-devin-key"),
        **overrides,
    )
    ticks = [0.0]

    def clock() -> float:
        """A clock that advances on every read, so no poll loop can spin forever."""
        ticks[0] += 1.0
        return ticks[0]

    return RuntimeOrchestrator(
        SessionClient(
            config,
            transport=transport,
            clock=clock,
            sleeper=lambda _seconds: None,
        )
    )


def run(
    transport: ScriptedDevinTransport | None,
    *,
    mode: Mode = Mode.LIVE,
    factory: Any = prompt_factory,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Run one candidate through planner, the concurrent join and the review loop."""
    return orchestrator(transport, mode=mode).run_candidate(
        "codeql-0",
        "PLANNER PROMPT",
        "IMPLEMENTER PLACEHOLDER",
        "REVIEWER PLACEHOLDER",
        candidate=candidate(),
        prompt_factory=factory,
    )


# -- phase B ---------------------------------------------------------------------------


def test_an_unreviewed_reviewer_is_sent_the_implementer_diff_on_its_own_session() -> None:
    """§9.3 — the follow-up goes to the existing reviewer session, not a new one."""
    transport = ScriptedDevinTransport()

    run(transport)

    assert [session_id for session_id, _ in transport.messaged] == ["reviewer-1"]
    assert IMPLEMENTER_DIFF in transport.messaged[0][1]
    assert "REVIEWER PHASE B PROMPT" in transport.messaged[0][1]
    assert transport.created.count(("reviewer", 1)) == 1


def test_the_reviewer_snapshot_is_re_polled_after_the_phase_b_message() -> None:
    """§9.3 — a snapshot is accepted only once the session has processed the diff."""
    transport = ScriptedDevinTransport()

    result = run(transport)

    assert transport.polled.count("reviewer-1") == 2
    reviewer_calls = [kind for kind, session_id in transport.calls if session_id == "reviewer-1"]
    assert reviewer_calls == ["poll", "message", "poll"]
    structured = result.reviewer.snapshot.payload["structured_output"]
    assert isinstance(structured, Mapping)
    assert structured["diff_reviewed"] != False  # noqa: E712


def test_a_completed_phase_b_review_lets_the_loop_converge() -> None:
    """§12.1 — the diff review is a convergence precondition, and phase B satisfies it."""
    transport = ScriptedDevinTransport()

    result = run(transport)

    assert result.review is not None
    assert result.review.converged is True
    assert result.review.reason is None


def test_at_most_one_phase_b_round_per_reviewer_session() -> None:
    """§9.3 — a reviewer that read the diff and still did not review it is escalated."""
    transport = ScriptedDevinTransport(reviews_after_message=False)

    result = run(transport)

    assert len(transport.messaged) == 1
    assert result.review is not None
    assert result.review.converged is False
    assert result.review.reason is ReasonCode.DIFF_REVIEW_INCOMPLETE


def test_a_snapshot_is_not_accepted_until_the_session_processes_the_follow_up() -> None:
    """§9.3 — re-reading the unchanged snapshot would have accepted the pre-message review."""
    transport = ScriptedDevinTransport(reviews_after_message=False, acknowledges_message=False)

    with pytest.raises(TimeoutError, match="follow-up message"):
        run(transport)

    assert len(transport.messaged) == 1


def test_a_rerun_creates_a_session_at_a_strictly_higher_attempt() -> None:
    """§12.2 — the idempotent create returns the same session for the same attempt.

    Rerunning at an attempt already used made the retry a guaranteed no-op: the create is
    idempotent per (candidate, role, attempt), so it handed back the session that had just
    produced the rejected output.
    """
    transport = ScriptedDevinTransport(invalid_baseline_attempts=frozenset({1}))

    result = run(transport)

    for role in ("implementer", "reviewer"):
        attempts = [attempt for created_role, attempt in transport.created if created_role == role]
        assert len(attempts) > 1, "the loop must actually rerun for this to pin anything"
        assert attempts == sorted(attempts)
        assert len(attempts) == len(set(attempts))
        assert attempts[1] > attempts[0]
    assert len(transport.created) == len(set(transport.created))
    assert result.review is not None
    assert ("implementer", 1) in transport.created
    assert ("reviewer", 1) in transport.created


def test_a_reruns_reviewer_session_is_also_sent_the_implementer_diff() -> None:
    """§9 step 4 — one phase-B round per reviewer session, not one per run.

    A single per-run flag meant a rerun's fresh reviewer session was never sent the diff, so
    the candidate terminated `diff_review_incomplete` for a diff it had never been given.
    """
    transport = ScriptedDevinTransport(invalid_baseline_attempts=frozenset({1}))

    result = run(transport)

    messaged = [session_id for session_id, _ in transport.messaged]
    reviewers = [session_id for role, session_id in transport.created if role == "reviewer"]
    assert len(messaged) == len(set(messaged))
    assert len(messaged) > 1
    assert messaged == [f"reviewer-{attempt}" for attempt in reviewers]
    assert result.review is not None
    assert result.review.reason is not ReasonCode.DIFF_REVIEW_INCOMPLETE


def test_a_reviewer_session_that_is_no_longer_running_fails_loudly() -> None:
    """§9.3 — a follow-up the session cannot process is a failure, not a silent skip."""
    transport = ScriptedDevinTransport(message_detail="session is not running")

    with pytest.raises(SessionMessageError, match="not running"):
        run(transport)


def test_live_without_a_prompt_factory_never_dispatches_a_stub_prompt() -> None:
    """§9 — a LIVE role prompt must carry planner context, so a stub is not an option."""
    transport = ScriptedDevinTransport()

    with pytest.raises(PlannerOutputError, match="factory"):
        run(transport, factory=None)


# -- SIMULATE is unchanged --------------------------------------------------------------


def test_simulate_makes_no_transport_call_at_all() -> None:
    """§7 — SIMULATE never reaches the Devin API, so phase B cannot fire there either."""
    transport = ScriptedDevinTransport()

    result = run(transport, mode=Mode.SIMULATE, factory=None)

    assert transport.created == []
    assert transport.messaged == []
    assert transport.polled == []
    assert result.review is not None


def test_simulate_is_identical_with_and_without_a_transport() -> None:
    """§7 — the phase-B wiring is LIVE-only; SIMULATE output is byte-identical."""
    without = run(None, mode=Mode.SIMULATE, factory=None)
    with_transport = run(ScriptedDevinTransport(), mode=Mode.SIMULATE, factory=None)

    assert with_transport.review == without.review
    assert with_transport.reviewer.snapshot.payload == without.reviewer.snapshot.payload
    assert with_transport.implementer.snapshot.payload == without.implementer.snapshot.payload


def test_a_simulate_follow_up_message_reaches_no_transport() -> None:
    """§7 — `send_message` is suppressed in SIMULATE rather than posted."""
    transport = ScriptedDevinTransport()
    client = orchestrator(transport, mode=Mode.SIMULATE)._client  # noqa: SLF001

    response = client.send_message("reviewer-1", "REVIEWER PHASE B PROMPT")

    assert transport.messaged == []
    assert "suppress" in str(response["detail"])


# -- planner output validation gates the join ------------------------------------------


def test_an_unusable_planner_output_never_reaches_the_other_roles() -> None:
    """§9 — the candidate defers before the implementer and reviewer are created."""
    transport = ScriptedDevinTransport()

    def empty_criteria(
        _planner_output: Mapping[str, object],
    ) -> tuple[str, str, Callable[[str], str]]:
        raise AssertionError("prompts may not be rendered from unusable planner output")

    class NoCriteriaTransport(ScriptedDevinTransport):
        def get(self, path: str) -> Mapping[str, object]:
            response = dict(super().get(path))
            if path.endswith("planner-1"):
                response["structured_output"] = {"files_in_scope": [], "out_of_scope": []}
            return response

    transport = NoCriteriaTransport()

    with pytest.raises(PlannerOutputError, match="criteria"):
        run(transport, factory=empty_criteria)

    assert [role for role, _ in transport.created] == ["planner"]
    assert transport.messaged == []
