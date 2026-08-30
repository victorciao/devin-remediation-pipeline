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
from pipeline.prompts import render_implementer_prompt, render_reviewer_prompt
from pipeline.review_loop import ReviewIteration
from pipeline.schemas import Candidate, ReasonCode
from pipeline.session_client import (
    BranchNotAdvancedError,
    DiffReviewIncompleteError,
    PhaseBCorrelationTimeoutError,
    PlannerOutputError,
    RuntimeOrchestrator,
    SessionAttempt,
    SessionBlockedError,
    SessionCeilingError,
    SessionClient,
    SessionMessageError,
    SessionRole,
    validated_diff_review,
)
from tests.factories import codeql_candidate

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40
STALE_HEAD_SHA = "c" * 40
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
    *,
    diff_reviewed: bool,
    red_baseline_valid: bool = True,
    reviewed_head_sha: str = HEAD_SHA,
    files_read: list[str] | None = None,
) -> Mapping[str, object]:
    """A reviewer payload whose `diff_reviewed` evidence is present only after phase B."""
    review: Mapping[str, object] | bool = (
        {
            "base_sha": BASE_SHA,
            "head_sha": reviewed_head_sha,
            "files_read": [PRODUCTION_PATH] if files_read is None else files_read,
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
        reviewed_head_sha: str = HEAD_SHA,
        reviews_before_message: bool = False,
        status: str = "finished",
        phase_b_answers: tuple[str, ...] = (),
        phase_b_head_sha: str | None = None,
        omits_message_history: bool = False,
    ) -> None:
        self.phase_b_head_sha = phase_b_head_sha
        self.omits_message_history = omits_message_history
        self.phase_b_answers = phase_b_answers
        self.reviewed_head_sha = reviewed_head_sha
        self.reviews_before_message = reviews_before_message
        self.status = status
        self.prompts: list[tuple[str, int, str]] = []
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
        self._messages: dict[str, list[Mapping[str, object]]] = {}
        self._message_counts: dict[str, int] = {}
        self._tick = 0

    def _answer(self, session_id: str) -> str:
        """The reviewer's answer state given how many phase-B messages it has received.

        `phase_b_answers` scripts one entry per exchange, so a corrective round can be answered
        differently from the first; the last entry repeats if the orchestrator asks again.
        """
        exchanges = self._message_counts.get(session_id, 0)
        if exchanges == 0 or not self.phase_b_answers:
            return "valid" if session_id in self._reviewed else "absent"
        return self.phase_b_answers[min(exchanges, len(self.phase_b_answers)) - 1]

    def _timestamp(self) -> str:
        """A strictly increasing API timestamp, so message correlation is decidable."""
        self._tick += 1
        return f"2026-08-29T00:00:{self._tick:02d}Z"

    def post(self, path: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Serve session creation and the phase-B follow-up message."""
        if path.endswith("/message"):
            session_id = path.split("/")[3]
            message = payload["message"]
            assert isinstance(message, str)
            self.messaged.append((session_id, message))
            self.calls.append(("message", session_id))
            history = self._messages.setdefault(session_id, [])
            self._message_counts[session_id] = self._message_counts.get(session_id, 0) + 1
            sent_at = self._timestamp()
            history.append({"type": "user_message", "timestamp": sent_at})
            if self.reviews_after_message:
                self._reviewed.add(session_id)
            if self.acknowledges_message:
                self._acknowledged.add(session_id)
                history.append({"type": "devin_message", "timestamp": self._timestamp()})
            return {"detail": self.message_detail, "created_at": sent_at}
        tags = payload["tags"]
        assert isinstance(tags, list)
        role = str(tags[2])
        attempt = int(str(tags[3]).removeprefix("attempt:"))
        self.created.append((role, attempt))
        prompt = payload["prompt"]
        assert isinstance(prompt, str)
        self.prompts.append((role, attempt, prompt))
        if self.reviews_before_message and role == "reviewer":
            self._reviewed.add(f"{role}-{attempt}")
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
            answer = self._answer(session_id)
            answered_head = self.reviewed_head_sha
            if self._message_counts.get(session_id, 0) and self.phase_b_head_sha is not None:
                answered_head = self.phase_b_head_sha
            reviewed = reviewer_output(
                diff_reviewed=answer != "absent",
                red_baseline_valid=attempt not in self.invalid_baseline_attempts,
                reviewed_head_sha=(STALE_HEAD_SHA if answer == "wrong_head" else answered_head),
                files_read=[] if answer == "missing_paths" else None,
            )
            output = (
                {**reviewed, "phase_b_acknowledged": True}
                if session_id in self._acknowledged
                else reviewed
            )
        snapshot: dict[str, object] = {
            "status_enum": self.status,
            "acu_used": 1.0,
            "session_id": session_id,
            "structured_output": output,
        }
        if not self.omits_message_history:
            snapshot["messages"] = list(self._messages.get(session_id, []))
        return snapshot


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
    previous_iteration: ReviewIteration | None = None,
) -> tuple[str, str, Callable[[str], str]]:
    """Stand in for `__main__`'s renderer wiring; the planner output must arrive here."""
    assert planner_output == PLANNER_OUTPUT
    return (
        "IMPLEMENTER PROMPT",
        "REVIEWER PROMPT",
        phase_b_prompt,
    )


def rendering_prompt_factory(
    planner_output: Mapping[str, object],
    previous_iteration: ReviewIteration | None = None,
) -> tuple[str, str, Callable[[str], str]]:
    """`__main__`'s real renderer wiring, so a rerun prompt is the text a role receives."""
    return (
        render_implementer_prompt(
            candidate(),
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
            planner_output=planner_output,
            previous_iteration=previous_iteration,
        ),
        render_reviewer_prompt(
            candidate(),
            target_repo=TARGET_REPO,
            base_sha=BASE_SHA,
            head_branch=HEAD_BRANCH,
            planner_output=planner_output,
            previous_iteration=previous_iteration,
        ),
        phase_b_prompt,
    )


def orchestrator(
    transport: ScriptedDevinTransport | None,
    *,
    mode: Mode = Mode.LIVE,
    budget: Mapping[str, Any] | None = None,
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
            **(budget or {}),
        )
    )


def run(
    transport: ScriptedDevinTransport | None,
    *,
    mode: Mode = Mode.LIVE,
    factory: Any = prompt_factory,  # noqa: ANN401
    budget: Mapping[str, Any] | None = None,
    **overrides: Any,  # noqa: ANN401
) -> Any:  # noqa: ANN401
    """Run one candidate through planner, the concurrent join and the review loop."""
    return orchestrator(transport, mode=mode, budget=budget).run_candidate(
        "codeql-0",
        "PLANNER PROMPT",
        "IMPLEMENTER PLACEHOLDER",
        "REVIEWER PLACEHOLDER",
        candidate=candidate(),
        prompt_factory=factory,
        **overrides,
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


def test_a_malformed_phase_b_answer_earns_exactly_one_corrective_exchange() -> None:
    """§17 (2) — one correction naming the defect, then a terminal outcome.

    An unbounded correction loop would spend the run on a reviewer that cannot answer, and a
    silent single rejection would throw away a reviewer that only mis-transcribed the head. The
    corrective text therefore has to name the defect — here the expected head against the one
    reported — and a second invalid answer settles `diff_review_incomplete` rather than being
    retried again.
    """
    transport = ScriptedDevinTransport(
        reviews_after_message=False,
        phase_b_answers=("wrong_head", "missing_paths"),
    )

    with pytest.raises(DiffReviewIncompleteError) as excinfo:
        run(transport)

    assert [session_id for session_id, _ in transport.messaged] == ["reviewer-1", "reviewer-1"]
    assert STALE_HEAD_SHA in transport.messaged[1][1]
    assert HEAD_SHA in transport.messaged[1][1]
    assert excinfo.value.iterations >= 1


def test_a_corrective_exchange_names_the_unread_paths() -> None:
    """§17 (2) — the defect named is the one the validator rejected, not a generic retry."""
    transport = ScriptedDevinTransport(
        reviews_after_message=False,
        phase_b_answers=("missing_paths", "wrong_head"),
    )

    with pytest.raises(DiffReviewIncompleteError):
        run(transport)

    assert len(transport.messaged) == 2
    assert "files_read" in transport.messaged[1][1]


def test_a_corrected_phase_b_answer_is_accepted_on_the_second_exchange() -> None:
    """§17 (2) — the point of the correction is that a fixed answer is honoured.

    Both exchanges count into the reported exchange total, so a candidate that needed a
    correction cannot look like one that answered first time.
    """
    transport = ScriptedDevinTransport(
        reviews_after_message=False,
        phase_b_answers=("missing_paths", "valid"),
    )

    result = run(transport)

    assert len(transport.messaged) == 2
    assert result.phase_b_exchanges == 2
    assert result.review is not None
    assert result.review.converged is True


def test_a_reviewer_repeating_its_invalid_answer_settles_terminally() -> None:
    """§17 (2) — a repeated invalid answer is still an answer, so it is terminal.

    Treating "the structured output did not change" as non-correlation reads a reviewer that
    answered and stood by its answer as a reviewer that never replied: the candidate then burns
    the whole phase-B timeout and reports `phase_b_correlation_unavailable`, which is not what
    happened. The plan's terminal outcome for a second invalid answer is
    `diff_review_incomplete`.
    """
    transport = ScriptedDevinTransport(
        reviews_after_message=False,
        phase_b_answers=("absent", "absent"),
    )

    with pytest.raises(DiffReviewIncompleteError):
        run(transport)

    assert len(transport.messaged) == 2


def test_a_snapshot_is_not_accepted_until_the_session_processes_the_follow_up() -> None:
    """§17 (1) — no correlated reviewer message means refusal, whatever else changed.

    The reviewer here answers the phase-B question in its structured output but the session's
    message history never gains a reply: accepting on the changed output alone would accept a
    review the session never sent, so the poller keeps polling and the candidate settles
    `phase_b_correlation_unavailable`.
    """
    transport = ScriptedDevinTransport(acknowledges_message=False)

    with pytest.raises(PhaseBCorrelationTimeoutError) as excinfo:
        run(transport)

    assert excinfo.value.reason is ReasonCode.PHASE_B_CORRELATION_UNAVAILABLE
    assert len(transport.messaged) == 1


def test_a_session_without_a_message_history_never_licenses_acceptance() -> None:
    """§17 (1) — an API response carrying no usable `messages` array is not an answer.

    Without the history there is nothing to correlate the phase-B question to, so no other
    signal — a finished status, a complete `diff_reviewed`, a changed structured output — may
    stand in for it; the candidate settles `phase_b_correlation_unavailable` instead.
    """
    transport = ScriptedDevinTransport(omits_message_history=True)

    with pytest.raises(PhaseBCorrelationTimeoutError) as excinfo:
        run(transport)

    assert excinfo.value.reason is ReasonCode.PHASE_B_CORRELATION_UNAVAILABLE


def test_a_review_supplied_before_the_phase_b_message_is_a_protocol_violation() -> None:
    """§17 (1) — a phase-A reviewer cannot have read the diff, so the object is a violation.

    A phase-A reviewer runs concurrently with the implementer; a `diff_reviewed` object in its
    first payload therefore describes a tree it could not have seen. It is recorded on the
    result so the row and the event carry it, scrubbed from the payload, and phase B is still
    asked — the premature object never counts as acceptance.
    """
    transport = ScriptedDevinTransport(reviews_before_message=True)

    result = run(transport)

    assert result.phase_b_protocol_violation is not None
    assert "diff_reviewed" in result.phase_b_protocol_violation
    assert [session_id for session_id, _ in transport.messaged] == ["reviewer-1"]


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
        _previous_iteration: ReviewIteration | None = None,
    ) -> tuple[str, str, Callable[[str], str]]:
        raise AssertionError("prompts may not be rendered from unusable planner output")

    class BlankCriterionTransport(ScriptedDevinTransport):
        def get(self, path: str) -> Mapping[str, object]:
            response = dict(super().get(path))
            if path.endswith("planner-1"):
                criterion = {**PLANNER_OUTPUT["criteria"][0], "statement": "   "}  # type: ignore[index]
                response["structured_output"] = {**PLANNER_OUTPUT, "criteria": [criterion]}
            return response

    transport = BlankCriterionTransport()

    with pytest.raises(PlannerOutputError, match="statement"):
        run(transport, factory=empty_criteria)

    assert [role for role, _ in transport.created] == ["planner"]
    assert transport.messaged == []


def test_a_planner_that_answered_without_criteria_at_all_is_a_blocked_session() -> None:
    """§9.3 — the required-output fence catches a missing schema key before validation.

    A planner snapshot with no `criteria` key has not answered its schema, so it is a
    blocked session rather than an unusable planner output.
    """

    class NoCriteriaTransport(ScriptedDevinTransport):
        def get(self, path: str) -> Mapping[str, object]:
            response = dict(super().get(path))
            if path.endswith("planner-1"):
                response["structured_output"] = {"files_in_scope": [], "out_of_scope": []}
            return response

    transport = NoCriteriaTransport()

    with pytest.raises(SessionBlockedError, match="without required output"):
        run(transport)

    assert [role for role, _ in transport.created] == ["planner"]
    assert transport.messaged == []


# -- the candidate-aware diff-review predicate -----------------------------------------


def test_a_well_formed_review_of_the_wrong_head_does_not_suppress_phase_b() -> None:
    """§9.3 — a review must identify the revision under review, not merely be structural.

    A reviewer reporting a syntactically complete `diff_reviewed` for a stale head has read
    a different tree than the one about to be published; accepting it would let phase B be
    skipped on evidence that does not describe the candidate.
    """
    transport = ScriptedDevinTransport(
        reviews_before_message=True,
        reviewed_head_sha=STALE_HEAD_SHA,
        phase_b_head_sha=HEAD_SHA,
    )

    result = run(transport)

    assert [session_id for session_id, _ in transport.messaged] == ["reviewer-1"]
    assert result.review is not None
    assert result.review.reviewed_head_sha == HEAD_SHA
    assert result.review.converged is True


# -- server-side branch verification ---------------------------------------------------


def test_a_branch_still_at_its_base_sha_is_not_advanced() -> None:
    """§9.3 — role sessions reporting success on an unmoved branch committed nothing."""
    transport = ScriptedDevinTransport()

    with pytest.raises(BranchNotAdvancedError, match=BASE_SHA):
        run(transport, head_sha_resolver=lambda: BASE_SHA)


def test_the_server_observed_head_sha_replaces_the_reported_one() -> None:
    """§9.3 — the reviewed head is re-resolved from the server every iteration."""
    transport = ScriptedDevinTransport(reviewed_head_sha="d" * 40)

    result = run(transport, head_sha_resolver=lambda: "d" * 40)

    assert result.review is not None
    assert result.review.converged is True


@pytest.mark.parametrize(
    "branch_paths",
    [
        ("tests/unit_tests/db_engine_specs/test_base.py",),
        ("superset/db_engine_specs/base.py",),
    ],
)
def test_a_branch_missing_one_role_commit_is_a_role_commit_missing_finding(
    branch_paths: tuple[str, ...],
) -> None:
    """§9.3 — both roles must have committed to the shared branch, as the server sees it.

    Structured output claiming a commit is self-reported; the changed paths between
    `base_sha..head_sha` are the only evidence that the commit exists.
    """
    transport = ScriptedDevinTransport()

    result = run(transport, branch_paths_resolver=lambda _base, _head: branch_paths)

    assert result.review is not None
    assert result.review.converged is False
    assert result.review.reason is ReasonCode.ROLE_COMMIT_MISSING
    assert result.review.needs_human_review is True


def test_a_branch_carrying_both_role_commits_raises_no_finding() -> None:
    """§9.3 — verification passes when the branch carries a production and a test path."""
    transport = ScriptedDevinTransport()

    result = run(
        transport,
        branch_paths_resolver=lambda _base, _head: (
            PRODUCTION_PATH,
            "tests/unit_tests/db_engine_specs/test_base.py",
        ),
    )

    assert result.review is not None
    assert result.review.converged is True
    assert result.review.reason is None


# -- rerun prompt context --------------------------------------------------------------


def test_a_rerun_prompt_carries_the_previous_iterations_failure() -> None:
    """§9.3 — a retry that repeats the original prompt re-rolls the same failed attempt.

    Without the prior findings, the failing test and the head SHA the previous attempt was
    observed at, the second attempt is a fresh dice roll rather than a correction.
    """
    transport = ScriptedDevinTransport(invalid_baseline_attempts=frozenset({1}))

    run(transport, factory=rendering_prompt_factory)

    first = [prompt for role, attempt, prompt in transport.prompts if role == "implementer"][0]
    rerun = [prompt for role, attempt, prompt in transport.prompts if role == "implementer"][1]
    assert "PREVIOUS ITERATION FAILURE" not in first
    assert "PREVIOUS ITERATION FAILURE" in rerun
    assert NODEID in rerun
    assert HEAD_SHA in rerun
    reviewer_rerun = [prompt for role, _, prompt in transport.prompts if role == "reviewer"][1]
    assert "PREVIOUS ITERATION FAILURE" in reviewer_rerun


# -- session evidence is persisted as sessions are created -----------------------------


def test_every_role_session_id_is_recorded_as_it_is_created() -> None:
    """§13 — a candidate whose loop fails still carries the ids needed to audit it."""
    transport = ScriptedDevinTransport()
    seen: list[SessionAttempt] = []

    run(transport, session_created=seen.append)

    assert seen[0].role is SessionRole.PLANNER
    assert {attempt.role for attempt in seen} == {
        SessionRole.PLANNER,
        SessionRole.IMPLEMENTER,
        SessionRole.REVIEWER,
    }
    assert all(attempt.session_id for attempt in seen)
    assert all(attempt.attempt >= 1 for attempt in seen)


def test_a_reviewer_that_times_out_still_reports_its_three_session_ids() -> None:
    """§13 — evidence recorded only on success loses exactly the failures worth auditing."""

    class NeverFinishingReviewer(ScriptedDevinTransport):
        def get(self, path: str) -> Mapping[str, object]:
            response = dict(super().get(path))
            if "reviewer" in path:
                response["status_enum"] = "running"
            return response

    transport = NeverFinishingReviewer()
    seen: list[SessionAttempt] = []

    with pytest.raises(TimeoutError):
        run(transport, session_created=seen.append)

    assert {attempt.role for attempt in seen} == {
        SessionRole.PLANNER,
        SessionRole.IMPLEMENTER,
        SessionRole.REVIEWER,
    }


# -- terminality is the required output, not the status word ---------------------------


@pytest.mark.parametrize("status", ["finished", "blocked"])
def test_a_stopped_session_that_answered_is_a_terminal_session(status: str) -> None:
    """§9.3 (l.692) — a role that completed its work settles at `blocked`, never `finished`.

    The evidence is the required structured output, not the status word: reading `blocked`
    as a failure fails every successful role.
    """
    transport = ScriptedDevinTransport(status=status)

    result = run(transport)

    assert [
        role.snapshot.status_enum for role in (result.planner, result.implementer, result.reviewer)
    ] == [
        status,
        status,
        status,
    ]
    assert validated_diff_review(result) is True


@pytest.mark.parametrize("status", ["blocked", "expired"])
def test_a_stopped_session_without_its_output_has_produced_no_evidence(status: str) -> None:
    """§9.3 (l.695) — a session waiting on something it cannot resolve fails the candidate.

    Accepting it as terminal accepted whatever partial structured output it happened to
    carry, so a blocked session could converge a candidate.
    """

    class SilentTransport(ScriptedDevinTransport):
        def get(self, path: str) -> Mapping[str, object]:
            response = dict(super().get(path))
            response.pop("structured_output")
            return response

    transport = SilentTransport(status=status)

    with pytest.raises(SessionBlockedError, match=status):
        run(transport)


def test_a_status_that_has_not_stopped_keeps_polling() -> None:
    """§9.3 — a status that is neither stop nor answer keeps polling until the deadline."""
    transport = ScriptedDevinTransport(status="stopped")

    with pytest.raises(TimeoutError):
        run(transport)


# -- the session ceiling refuses the candidate it cannot afford ------------------------


def test_the_session_ceiling_stops_the_candidate_it_cannot_afford() -> None:
    """§13 — the orchestrator refuses the creation loudly; the run defers that candidate.

    The error never reaches the operator: `run_once` catches it, appends the in-flight
    candidate as `deferred`/`session_ceiling` and carries on, so an exhausted budget costs
    exactly the candidates it could not pay for and none of the §11 accounting.
    """
    transport = ScriptedDevinTransport()

    with pytest.raises(SessionCeilingError, match="session ceiling"):
        run(transport, budget={"max_sessions": 2})


def test_a_ceiling_reached_mid_candidate_leaves_the_later_roles_uncreated() -> None:
    """§13 — nothing is dispatched after the budget is gone, not even the reviewer."""
    transport = ScriptedDevinTransport()

    with pytest.raises(SessionCeilingError):
        run(transport, budget={"max_sessions": 1})

    assert transport.created == [("planner", 1)]
    assert transport.messaged == []


def test_the_acu_ceiling_stops_the_candidate_the_same_way() -> None:
    """§13 — an ACU budget exhausted by a running session refuses the same way."""
    transport = ScriptedDevinTransport()

    with pytest.raises(SessionCeilingError, match="ACU"):
        run(transport, budget={"max_total_acu": 0.5})
