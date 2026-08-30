# Orchestration & publication subsystem — design

Scope: driving the three Devin role sessions per candidate and publishing the resulting artifacts —
`session_client.py`, `state.py`, `review_loop.py`, `github_client.py`, and `__main__.py`'s
`_prepare_live_candidate`, role-loop failure handler and `_publish_live`. Contracts are fixed by
`docs/IMPLEMENTATION_PLAN.md` §§7, 9, 11, 12, 13, 14 and the measured facts P-1..P-9 in
`docs/api-probe-2026-08-30.md` and `docs/api-probe-2026-09-01.md`; nothing here redesigns those.

## 0. Exit gate (acceptance criteria — these replace "no blocking findings")

- **G1** One real candidate completes planner → implementer → phase-B review correlated at a
  resolved head SHA → local CI evidence → issue → PR → issue patched with the PR link, with
  `iterations >= 1` and the three artifact URLs recorded on its state row.
- **G2** Every failure mode is proven by fault injection, not inspection: the process is `SIGKILL`ed
  (a) after each role-session dispatch and (b) between each adjacent pair of artifact writes
  (issue→PR, PR→patch, and reservation→issue); each run is resumed to completion; afterwards
  exactly one issue and one PR exist for that candidate on the fork.
- **G3** `tests/` is green on the current contract with R1..R21 below encoded as tests, so a later
  fix batch fails at the door.
- **G4** After one review of the implemented design, a finding is blocking only if it changes what
  is written to `victorciao/superset`; everything else is a listed follow-up.
- **G5** A LIVE run's KPI rollup agrees with the fork, re-read after the run: `dispatched_pr` equals
  the PRs that exist, burn-down counts every published candidate, and no candidate whose artifact
  exists is reported `capability_unavailable`.
- **G6** No candidate is deferred for a reason naming a subsystem that did not fail.

## 1. Invariants (numbered rules)

- **R1** An *evidence value* is a triple `(value, source, provenance)`, `source ∈ {observed_api,
  observed_exec, agent_claim}`; provenance is the request/command and the SHA or timestamp it was
  taken at. Gates may read only `observed_*`. Prevents: an agent's sentence becoming a fact by being
  stored in the same field shape as a measurement.
- **R2** An `agent_claim` may enter a decision only as the *left side of a comparison* against an
  `observed_*` value. `diff_reviewed.head_sha` compared against the orchestrator-resolved branch
  head is the pattern — **already correct** (`session_client.py:1084-1100`, `:135-150`).
- **R3** The red baseline is a claim treated as a measurement today: `review_loop.py:393-412`
  classifies whatever the reviewer put in `red_baseline.observed`. Until the orchestrator runs the
  nodeid itself, `red_result` carries `source=agent_claim`, such a candidate is `needs-human-review`,
  never auto-merge eligible, and its PR body says the baseline is reviewer-reported. Prevents:
  shipping "verified red→green" the pipeline never saw.
- **R4** Absence is a positive observation, never a default. `MarkerSearchOutcome.ABSENT` may be
  produced only from a `200` with `total_count == 0` (P-4); `FAILED`, `ORPHANED` and `UNCONFIGURED`
  must not return `None` through the same channel as `ABSENT` (`state.py:229-237`) — the lookup
  returns the outcome and every caller branches on it. Prevents: "could not look" reading as
  "nothing is there" one line before the first write.
- **R5** `ABSENT` proves only "no indexed marker at time t": indexing lags (P-6, measured 1–17 s,
  treat as unbounded), so an absence search is necessary but never sufficient authority for a first
  write. The sufficient authority is R12's claim.
- **R6** A durable identity (`issue_url`, `issue_number`, `pr_url`, `pr_number`, `comment_url`,
  `head_sha`, `reviewed_head_sha`, `merged_at`) is write-once-non-null; `StatePreservationError` in
  `_append_locked` stays the enforcement point (**already correct**, `state.py:266-276`), so every
  writer must carry forward what it does not itself resolve (B9-1).
- **R7** `reviewed_head_sha` is written once from a validated phase-B answer and is the only source
  of the rendered `diff_range` — **already correct**.

- **R8** The transcript is the only correlation source. `GET /v1/sessions/{id}` returns the complete
  `messages` array with per-message `type`/`event_id`/`timestamp` and has no pagination surface
  (P-3, P-7). The `POST .../message` body carries nothing usable (P-2: `200`, null body) and must
  not be read for identity or time (B9-2).
- **R9** `structured_output` merges keys across messages and is not fenced by the creation schema
  (P-2). Therefore the presence of a required key is never evidence that the key is *new*.
- **R10** Timestamps are compared as instants (`datetime.fromisoformat`), never as strings (m9-2).
- **R11** A session's terminal condition is `finished`, or `blocked` whose `structured_output`
  already carries the role's required keys (P-1) — **already correct**.

- **R12** An exclusive claim (`reserved_at`, `reserved_by_run_id`) is taken at the point of use,
  renewed at the first write of each phase, and expires after `reservation_lease_s`.
- **R13** A claim whose `reserved_at` exceeds `now + reservation_lease_s` is invalid (clock skew or
  a hand-edited row): log and reclaim. Small negative skew inside the lease still reads as live
  (M9-2). Prevents: a row parked forever and reported as contention.
- **R14** Every raised failure declares its scope: `CandidateScoped` (defer this candidate, continue
  the run) or `RunScoped` (abort after reporting). Handlers dispatch on scope, not exception class,
  and no handler may widen a scope. `capability_unavailable` is reserved for a capability that was
  actually exercised and failed (M9-3, M9-4, M8-7).
- **R15** `main` must exit `1` with a sanitized message for any `RunScoped` failure, including
  `StatePreservationError`, and never with a traceback (M9-3).

- **R16** One function publishes, for both fresh and resuming runs, keyed by `candidate_id`
  (§4 below). No second code path may create an issue, PR or comment.
- **R17** "Already done" is decided by artifact proof — a persisted URL/number for that artifact, or
  a marker/head-branch match on the fork — never by a state value (§14.1) — and each artifact is
  written at most once per candidate.
- **R18** Before each artifact write the intended next write is recorded as a `write_intent` row
  (`candidate_id`, artifact kind, run id); the identity row supersedes it. A resume finds at most one
  open intent and reconciles exactly that artifact against the fork (`GET /pulls?state=all&head=…`,
  P-9; marker search for an issue) before retrying. Prevents: a crash between two writes leaving an
  artifact no state row names (G2).
- **R19** Publication order is issue → PR (`Closes #n`) → patch issue with the PR link (§7); labels,
  including the mandatory `needs-human-review` under `ci_evidence_mode=local`, are applied inside the
  same per-candidate `try` as the writes, so a label failure cannot leave a PR unlabelled and
  unaccounted. `needs-human-review` does not exist on the fork and must be created (P-8).

- **R20** `iterations` = review rounds + phase-B exchanges (a sum, not `max()`; clamp the *report*,
  not the count) (m9-1). Burn-down and `completed` count every row with an artifact, including
  `PR_CREATED`/`ISSUE_PATCHED`/`COMMENT_CREATED`; `dispatched_pr` counts `pr_url is not None`
  whatever the lifecycle state; deferred-with-artifact is its own line (M9-5).
- **R21** "Not published" and "could not look" stay distinct: the per-candidate
  `MarkerSearchOutcome` is on the row and in the report (**already correct**, M8-8), and a duplicate
  suppressed by found-marker proof is a *success* (`artifact_exists`), never `capability_unavailable`
  (M9-4). A SIMULATE artifact states `mode=simulate`, `writes_suppressed=<n>` and
  `artifact_simulated=true` in the body, the row and the KPI rollup (m9-5).

## 2. Candidate state machine (one table)

| State | Entered when | Action | Publishes on this and every later run | Claim |
|---|---|---|---|---|
| `enumerated`/`gated`/`scored` | selection | — | nothing | none |
| `dispatching` | claim taken at point of use | intended | nothing; resume re-enters role loop, or publication if a `write_intent` is open | held, renewed per phase |
| `converged` | review loop converged, `diff_reviewed` true, baseline valid | `open_pr` | issue → PR → issue patch | renewed before first write |
| `converged` | medium tier | `open_issue` | issue only | renewed |
| `terminal` + `stale_skip` | reviewer-only diff, converged | `reviewer_only_diff` | issue → PR → issue patch | renewed |
| `terminal` + any other reason | cap hit, `diff_review_incomplete`, `invalid_red_baseline`, `branch_not_advanced`, `role_commit_missing`, `implementer_test_edit` | `human_review` (set by the failure handler) | issue only, `needs-human-review`; **never a PR** | released after write |
| `deferred` | any `CandidateScoped` failure, `reservation_held`, `session_ceiling` | unchanged | nothing new; only completes an open `write_intent` | released |
| `issue_created`/`pr_created` | mid-publication crash | as recorded | the remaining artifacts of the recorded intent only | renewed |
| `issue_patched`/`comment_created` | complete | as recorded | nothing (artifact proof present) | released |

Eligibility predicate, the only one: `publishable(row) = intent_for(row) is not None`, where
`intent_for` maps exactly the rows above to `{issue+pr, issue_only, resume_intent}` and everything
else to `None`. It reads *state and action*, never action alone (B9-4).

## 3. Correlation contract (phase B)

Given: transcript always complete (P-3, P-7); `structured_output` merged across messages (P-2);
message POST body unusable (P-2).

1. **Before sending**, snapshot `S0`: the `messages` timestamps and the *unscrubbed*
   `structured_output`, including any phase-A `diff_reviewed` whatever its shape. A phase-A
   `diff_reviewed` is recorded as `phase_b_protocol_violation` and never removed from `S0` (B9-3).
2. **Send** the phase-B prompt, which renders the required object verbatim: the literal `base_sha`,
   the orchestrator-resolved `head_sha`, and the enumerated changed paths (§12.1). Phase B is never
   sent without a resolved head — **already correct**.
3. **Resolve the sent message** from the transcript: poll `GET /v1/sessions/{id}` until a
   `user_message` absent from `S0` appears; that instant is `t_sent`. If none appears within
   `phase_b_send_ack_timeout_s`, refuse with `phase_b_correlation_unavailable`.
4. **Acceptable answer**: a `devin_message` with timestamp `> t_sent` and absent from `S0`, *and* a
   `diff_reviewed` object that (a) is not equal to the phase-A value from `S0`, (b) has non-empty
   `base_sha` equal to the candidate's, (c) has `head_sha` equal to the resolved head, and (d) has
   `files_read ⊇` the implementer's changed paths.
5. **Protocol violation** (logged, never acceptance): a `diff_reviewed` that predates `t_sent`,
   equals the phase-A value, or arrives with no correlated `devin_message`. Non-correlation is
   refusal, never inference (§12.2).
6. **The single corrective exchange** may assume only: the same reviewer session, one send, the
   defect quoted verbatim (missing paths, or expected vs. reported `head_sha`), and its own fresh
   `S0`/`t_sent` — nothing is inherited. Exhaustion raises `DiffReviewIncompleteError`
   (`CandidateScoped`) with the summed `iterations`. Phase B is attempted once per reviewer session
   id — **already correct**.

## 4. Publication contract (one path)

`publish_candidate(candidate_id)` is the sole entry, called identically by a fresh and a resuming
run, and per artifact:

1. Load the last row; compute `intent_for(row)`; `None` → return unchanged.
2. Renew the claim (`append_if_new_artifact` with this `run_id`), which re-takes the marker search
   *inside* the lock (**already correct**, `state.py:305-320`). Found marker → `artifact_exists`
   (a success, R21); `reservation_held` by a live foreign claim → defer.
3. For each artifact in order (R19): if artifact proof exists → adopt it; else write
   `write_intent`, reconcile against the fork (R18), then create, then write the identity row
   carrying every durable field forward (R6).
4. Labels and the CI-evidence body patch happen inside the same `try`; their failure degrades the
   row (`auto_merge_eligible=False`, reason recorded) but never re-raises past the candidate.

## 5. Ordered task list

1. `state.py` — return `MarkerSearchOutcome` from the lookup instead of `None`-on-failure; type
   `reservation_reason` as `ReasonCode | None`; add `artifact_exists`. R4, M9-4, n9-1.
2. `state.py` — bound future `reserved_at` by `now + reservation_lease_s` and reclaim beyond it. M9-2.
3. `state.py` — `decide_resume`: `SKIP`/adopt when `persisted is None` and a marker artifact was
   found; no publication-resume for `TERMINAL`/`DEFERRED` rows with no intent. B9-4, M9-4.
4. `state.py` + `schemas.py` — add the `write_intent` row kind and `open_intent(candidate_id)`. R18.
5. `schemas.py` — `Evidence` wrapper (`value`, `source`, `provenance`) and `red_baseline.source`. R1, R3.
6. `session_client.py` — derive `t_sent` from the transcript, delete `_sent_message_timestamp`'s
   POST-body path, add the bounded send-ack wait. B9-2, R8.
7. `session_client.py` — compare against the unscrubbed `S0`; reject a `diff_reviewed` equal to the
   phase-A value; record a premature *malformed* object as a violation too. B9-3, R9.
8. `session_client.py` — parse timestamps as instants. m9-2.
9. `session_client.py` — sum review rounds and phase-B exchanges; delete the dead `rerun`
   short-circuit. m9-1, m9-3.
10. `errors.py` (new) — `CandidateScoped`/`RunScoped` mixins; retag existing exceptions. R14.
11. `__main__.py` role-loop failure handler — set `action=HUMAN_REVIEW` on terminal/deferred rows;
    keep `iterations`. B9-4.
12. `__main__.py` — wrap `_prepare_live_candidate` per candidate; add `StatePreservationError` to
    `main`'s handled set. M9-3, R15.
13. `__main__.py` — reserve at the point of use, not for the batch; renew at the first write of the
    session phase and of the publication phase. M9-1, R12.
14. `__main__.py` — publication callbacks write `head_shas[0] or candidate.head_sha`
    (`after_pr_created`, `after_issue_patched`, `after_comment_created`); keep the guard. B9-1, R6.
15. `__main__.py` — replace the action-only filter with `intent_for(row)`; unify fresh and resume
    into `publish_candidate`. B9-4, R16.
16. `__main__.py` — drop `reason_detail = … or marker_outcome.value` for healthy candidates. n9-2.
17. `github_client.py` — reconcile the open `write_intent` before retrying its artifact. R18.
18. `observability/kpis.py`, `report.py` — burn-down/`completed`/`dispatched_pr` per R20; exclude
    `STALE_SKIP` from `escalated`; emit `writes_suppressed`/`artifact_simulated`. M9-5, m9-4, m9-5.
19. `tests/` — encode R1..R21; add the fault-injection harness for G2.
