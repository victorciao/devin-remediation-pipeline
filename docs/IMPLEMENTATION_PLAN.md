# Implementation Plan — Devin Remediation Pipeline for Apache Superset

## 1. Purpose and scope

This repo finds real technical debt in the Superset fork `victorciao/superset`, ranks it deterministically, and lands verified fixes there as
pull requests. **Per candidate there is one Devin session and one GitHub artifact: the PR.**

The **orchestrator** (this program) owns everything except the fix: discovery, gates, scoring, branch creation, launching the session, red→green
verification, PR creation, CI watching, merging, state, KPIs, resume. The **session** writes the fix and its regression test on the candidate
branch. Nothing a session *reports* is a gate; only what the orchestrator observes is.

Out of scope: tracking issues, multi-role sessions, a frontend lane, real-time dashboards.

## 2. Architecture

- `lanes/` — `codeql.py`, `skipped_tests.py`, `deprecations.py` enumerate candidates.
- `gate.py`, `score.py`, `dispatch.py` — gates, deterministic score, tier + per-run budget.
- `session_client.py` — creates and polls exactly one Devin session per dispatched candidate.
- `verify.py` (new) — runs the candidate's test at `base_sha` (must fail) and at the branch head (must pass), from the orchestrator's own execution.
- `github_client.py` — branch, PR, labels, required-context polling, merge; `templates/render.py` — PR title/body rendering and validation.
- `state.py` — append-only `state/candidates.jsonl`, write-once identities, resume; `observability/` — event log, run report, KPI rollup.

## 3. Candidate lifecycle (single state table)

| State | Entered when | Action | Artifact | Terminal |
|---|---|---|---|---|
| `enumerated` | a lane emits it | gate | none | no |
| `gated` | all gates passed | score | none | no |
| `scored` | tier assigned | dispatch if high tier and within `budget_N` | none | no |
| `dispatching` | branch created, session launched | poll the session | branch | no |
| `session_done` | session terminal with required output | verify red→green | branch | no |
| `verified` | test red at `base_sha`, green at head | render + create PR | branch | no |
| `pr_created` | PR exists and its number is recorded | watch required contexts | PR | no |
| `merged` | all required contexts green, merge succeeded | record KPIs | PR | **yes** |
| `terminal` | any §9 failure | record reason; never merge | ≤1 PR | **yes** |
| `deferred` | budget overflow, session ceiling, transport error | retry next run | ≤1 PR | no |

Terminal reasons: `session_failed`, `session_blocked`, `invalid_red_baseline`, `stale_skip`, `green_not_reached`, `ci_check_failed`,
`ci_evidence_unavailable`, `artifact_validation_failed`, `human_routed` (a gate routed it; no artifact is ever written for it).

## 4. Discovery and gates

**LANE 1 — CodeQL alerts.** `alert_source = api | sarif_file`; `api` reads `GET /repos/{o}/{r}/code-scanning/alerts`, `sarif_file` reads
`fixtures/codeql_alerts.json`. Freshness comes from `alert.updated_at`. Python only: an alert path outside `superset/**/*.py` fails
`verifiability_exists` with `out_of_scope_frontend`, gating out 3 of the 11 live JS/TS alerts at baseline `a140e74`.

**LANE 2 — skipped tests.** AST enumerator over `tests/` matching **unconditional** `@pytest.mark.skip` / `@unittest.skip` only, decorator names
resolved through the module's import bindings; `skipif`/`skipUnless` are excluded as `conditional_environment_guard`, `xfail` as
`expected_failure_xfail`, and `pytestmark`, in-body `pytest.skip()` and relative-import aliases are out of scope. Live: **35** included, **33**
excluded (30 guards, 3 xfail). Each record carries the collectable nodeid (`path::Class::method`), `class_scope`, `kind`, `enclosed_tests`,
`parametrized`, `collects_single_item`, `enclosing_skip_nodeid`.

**LANE 3 — EOL `@deprecated` removals.** AST scan of `superset/**/*.py` yielding `module:qualname`. EOL = `removed_in <=` current version, or,
absent `removed_in`, `major(deprecated_in) <= current_major - eol_major_lag` (`eol_major_lag = 2`). `current_major` is the highest concrete
release in `version_source` (`.github/ISSUE_TEMPLATE/bug-report.yml`) — **6** at baseline, so the threshold is `major <= 4`; a source with no
concrete release is a startup error. Live: 2 EOL sites, 1 automatable (`normalize_indexes`).

**Gates** — binary, all must pass, each failure recording its own reason:

1. `trigger_exists` — a machine-readable source record exists.
2. `automatability` — rubric `>= 2` **and** every lane hard condition: LANE 2 breadth (`enclosed_tests > lane2_class_breadth_max = 5` →
   `class_scope_too_broad`; `enclosed_tests = 0` with no live `pytest --collect-only` count → `class_breadth_unknown`), LANE 2 nesting
   (`enclosing_skip_nodeid` present → `blocked_by_enclosing_skip`), LANE 3 `no_internal_callers_and_no_override_surface` (→ `public_api_surface`
   / `internal_caller`).
3. `verifiability_exists` — a concrete pass/fail signal (a runnable nodeid) exists.

A gate failure is dropped or terminal-`human_routed`; it never produces a GitHub write. Recurrence is neither a gate nor a score input.

## 5. Scoring

```
score = min( business_impact × verifiability × automatability × signal_quality / max(risk, 1),
             score_cap )
```

Each factor is a 1–5 rubric row from `config/rubrics.yaml`, one table per lane per factor (`business_impact` from alert severity / covered
surface / public-API exposure, `signal_quality` from rule precision and freshness / skip-reason specificity / age in majors, `risk` from blast
radius / test-only diff, +1 when `kind = class` / caller-and-override count).

- `risk` floored at 1, composite capped at `score_cap = 200`. `tier_high_min = 60`, `tier_medium_min = 20`; example: `4×4×4×4 / 2 = 128` → high.
- **High** → dispatch a session and open a PR (auto-merge eligible only when `risk <= 2`, `auto_merge_enabled`, and the §8 gate holds).
  **Medium** and **low** → reported only: no session, no artifact.
- `budget_N = 10` PRs per run; overflow is `deferred/budget_overflow`, retried next run. `BUDGET_HARD_MAX = 25` clamps the knob and logs
  `guardrail_clamped`.

## 6. The session contract

The orchestrator first creates `devin/remediation/<candidate_id>` from the target base and pins `base_sha`, then creates one session.

The prompt must contain: repo and branch; `base_sha`; lane and locator (alert rule + path + region, or nodeid, or `module:qualname`); the fix
objective; the requirement to add or re-enable exactly one regression test that fails before the fix and passes after; the command to run it;
`git commit --signoff` and push to the candidate branch only; and the prohibition on opening any PR or issue, touching other branches, or editing
unrelated tests.

Required `structured_output_schema`:

```
{ files_changed[], test_nodeid, test_paths[], verify_command, head_sha,
  fix_summary, testing_notes, feasible: bool, infeasible_reason: string|null }
```

- `feasible = false` with a reason is a legitimate answer: the candidate settles `terminal/session_failed` with that reason and no PR is opened.
- Terminal session status is `finished`, **or** `blocked` whose `structured_output` already carries the required keys; `blocked` without them is
  `session_blocked`, `expired` fails, and `structured_output` can appear while `working`, so its presence alone is never terminal.
- Every creation passes `idempotent: true`, `tags: ["devin-remediation", candidate_id, "attempt:<n>"]`, an attempt ordinal in the prompt
  preamble, `session_timeout_s` and `max_acu_limit`. For attempt `> 1` the orchestrator asserts a genuinely new session: `is_new_session` `true`
  → proceed, `false` → fatal, `null` → compare the returned `session_id` with the previous attempt's. Exceeding `max_sessions` defers this and
  every later candidate with `session_ceiling`; the run still publishes and reports what it holds.

## 7. Verification and publication

Red→green is established **once, by the orchestrator, before the PR is opened**:

1. At `base_sha` with only the session's test-path diff applied, run `test_nodeid`: it must exit `FAILED`. All items `PASSED` →
   `terminal/stale_skip`; any `SKIPPED`, a collection `ERROR`, or no run → `terminal/invalid_red_baseline`. For a multi-item locator the run is
   valid iff at least one item `FAILED` and none is `SKIPPED`, except items carrying their own marker, logged as `still_skipped_descendants`.
2. At the candidate head, run the same nodeid. Not green → `terminal/green_not_reached`. Both per-item outcome vectors go to the state row and
   the event log.

**Duplicate detection is one exact query**, taken immediately before the write: `GET /repos/{o}/{r}/pulls?state=all&head=<owner>:<candidate
branch>`. A match is adopted, never duplicated. No marker, no marker search, no issue.

The PR body renders Superset's `.github/PULL_REQUEST_TEMPLATE.md` heading set verbatim and in order — `SUMMARY`, `BEFORE/AFTER SCREENSHOTS OR
ANIMATED GIF` (`n/a` for backend fixes), `TESTING INSTRUCTIONS`, `ADDITIONAL INFORMATION` with its checkbox block — plus a `TESTS` section after
`SUMMARY` carrying the orchestrator's red→green evidence (nodeid, failure signature at `base_sha`, pass at head, commands run) and a config-gated
`AUTOMATION METADATA` section last. The body states that every commit carries the `Signed-off-by` trailer. A body failing section presence/order
validation defers the candidate with `artifact_validation_failed` **before** any write; a security-lane body carries rule ID and file scope only,
never exploit detail. The title must match the fork's `pr-lint` regex, pinned in `templates/superset/pr_title_regex.txt`:

```
^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+
```

## 8. Auto-merge gate

The gate is the fork's own CI: **every context in `required_contexts` observed green on the PR head.** `required_contexts` is a config value
measured once from a probe PR on the fork, not a hard-coded list. `pre-commit checks`, `Python-Unit` and `Frontend` run there without repository
secrets and are the expected members; `docker`, `showtime-trigger` and `check-python-deps` need real secrets and are never gates. Polling waits
at most `ci_wait_timeout_s` (default `5400`). Expiry, or a PR held in pending workflow approval, records `ci_evidence_unavailable`; a failing
required context records `ci_check_failed`. Neither ever merges. There is **no** merge-time re-run of the suite — CI covers that. Merge happens
only when `auto_merge_enabled`, the §5 tier and this gate all hold; the merge is then re-read from the fork and `merged_at` recorded.

## 9. Failure and recovery

Every failure is terminal, leaves **at most one PR** on the fork, and never merges:

| Failure | State / reason | Report says |
|---|---|---|
| Session errors, expires, or answers `feasible = false` | `terminal/session_failed` | not remediated, with the session's reason; branch left behind, no PR |
| Session `blocked` without the required output | `terminal/session_blocked` | not remediated, reason `session_blocked`; no PR |
| Test not red at `base_sha` | `terminal/invalid_red_baseline` or `terminal/stale_skip` | the per-item outcome vector; no PR |
| Test not green at head | `terminal/green_not_reached` | the failure signature; no PR |
| Required context fails or never reports | `terminal/ci_check_failed` / `terminal/ci_evidence_unavailable` | PR URL as open-not-merged, with the offending contexts |

**The one remaining non-atomic step is: create the PR, then record its number.** A crash between them leaves a PR no state row names. Resume
settles it from the fork: for any candidate whose last row is `verified` or `pr_created` without a `pr_number`, resume queries `GET
/pulls?state=all&head=<candidate branch>` and adopts the single match (recording `pr_number`, `pr_url`, `head_sha`) before doing anything else;
no match means the write never landed and publication is retried. Because the branch name derives from `candidate_id` the query is exact, so at
most one PR can ever exist per candidate.

State rows are append-only, last-write-wins per `candidate_id`; durable identities (`pr_url`, `pr_number`, `head_sha`, `merged_at`) are
write-once-non-null and replacing a non-null value with `None` raises `StatePreservationError`. "Already published" is decided by artifact proof
— a persisted number or a fork match — never by a state value alone. A per-candidate failure defers or terminates that candidate only; the run
continues to publication and reporting.

## 10. State and observability

- `state/candidates.jsonl` is the dedupe/resume source of truth. `candidate_id = sha256(lane | repo | stable_locator)`, where `stable_locator` is
  `rule_id + file_path + normalized_symbol + position_digest` (LANE 1; **never** `alert.number`), the collectable nodeid (LANE 2), or
  `module:qualname` (LANE 3). `position_digest = sha256("{start_line}:{start_column}-{end_line}:{end_column}")[:12]`: four live
  `py/overly-large-range` alerts share one line of `add_chart_to_existing_dashboard.py` and differ only by column, so without it they collapse
  into one candidate.
- **Drift** — an edit above an alert shifts its digest, so before dispatching a LANE 1 candidate the orchestrator attempts a drift match against
  state: the weak key `(rule_id, file_path, normalized_symbol)` must have multiplicity 1 among current alerts *and* among active (not
  `superseded_by`) rows, and the persisted `region_digest` or `symbol_relative_offset` must agree. A hit links the rows (`supersedes` /
  `superseded_by`) and suppresses re-dispatch. Neither condition is configurable.
- **Layer 1** JSONL event log per candidate: `run_id`, lane, `candidate_id`, gate results and failed gate, score with factor breakdown, tier,
  action, `session_id`, red→green per-item outcomes, `test_added`/`test_paths`, PR URL and number, required-context statuses, terminal state and
  reason, LANE 2 breadth fields, `related_candidate_id`.
- **Layer 2** `reports/run-<run_id>.md`: candidates seen, gated out with reasons, scored, dispatched, deferred, every terminal candidate with its
  reason, PR links, merge results. No candidate may be unaccounted for.
- **Layer 3** `reports/kpis.md`: PR merge rate, verification pass rate (red→green established over dispatched), backlog burn-down per lane
  against `fixtures/baseline.json` (`n/a` for a lane absent from `baseline_valid_lanes`), test-inclusion rate, session-failure rate,
  deferred-by-reason; alerts when merge rate `< merge_rate_floor = 0.50` or session-failure rate `> session_failure_ceiling = 0.30`.

## 11. Configuration and modes

| Knob | Default | Safety |
|---|---|---|
| `mode` | `simulate` | **safety-relevant** — `live` must be explicit; unset/empty/unknown → `simulate`, logged |
| `budget_N` / `max_sessions` | `10` / `budget_N` | **safety-relevant** — clamped at `BUDGET_HARD_MAX = 25`; the ceiling defers, never kills the run |
| `score_cap` / `tier_high_min` / `tier_medium_min` | `200` / `60` / `20` | `tier_high_min > tier_medium_min` |
| `eol_major_lag` / `version_source` | `2` / `.github/ISSUE_TEMPLATE/bug-report.yml` | drift-tested; no concrete release → startup error |
| `lane2_class_breadth_max` / `alert_source` / `alert_fixture_path` | `5` / `api` / `fixtures/codeql_alerts.json` | **safety-relevant** — §4 breadth ceiling |
| `required_contexts` / `ci_wait_timeout_s` | probe-measured / `5400` | **safety-relevant** — empty forces `auto_merge_enabled = false`; expiry → `ci_evidence_unavailable` |
| `auto_merge_enabled` | `false` | **safety-relevant** — never sufficient alone (§5, §8) |
| `session_timeout_s` / `max_acu_limit` / `max_total_acu` | `5400` / per-session / `500` | — |
| `merge_rate_floor` / `session_failure_ceiling` / `kpi_sink` | `0.50` / `0.30` / `local` | `gsheet` under `simulate` is a startup error |
| `coverage_bar` | `0.80` | enforced over `gate.py`, `score.py`, `dispatch.py`, `dedupe.py`, `templates/render.py`, `observability/kpis.py` |

Also configurable: target `owner/repo`, GitHub token, Devin API key, rubrics, templates.

**SIMULATE** (default) needs **no credentials**, makes **no** network writes and creates no sessions: discovery from fixtures, gates, scoring,
rendering, reporting; every artifact is marked `artifact_simulated = true` with `writes_suppressed = <n>`, and any attempted write raises.
`docker compose run --rm pipeline` must complete a full SIMULATE run offline.

**LIVE preconditions**, all checked before the first write, each failure aborting before it: `mode = live` supplied explicitly; `github_token`
and `devin_api_key` present; token identity and scopes recorded from `GET /user`; `GET /repos/{o}/{r}` reachable with push access; code scanning
readable (`200`) or `alert_source = sarif_file`; Actions enabled with ≥1 completed `pull_request` run; `required_contexts` non-empty.

## 12. Ordered task list

1. Strip the §14 dead modules, symbols and config keys; keep the suite importable.
2. Collapse `session_client.py` to one role: create, poll to terminal, validate the §6 schema, attempt/retry assertion, session ceiling. Add
   `prompts.render_fix_prompt` building the §6 prompt from a `Candidate`.
3. Add `verify.py` — `verify_red_at_base` / `verify_green_at_head`, executing the nodeid in a real checkout and returning per-item outcomes.
4. `github_client.py`: drop issue and comment writes; `pull_request_for_head` as the sole dedupe path; add `required_contexts` polling and merge.
   `templates/render.py`: PR body/title only, with the `TESTS` evidence section.
5. `state.py`: the §3 state set, resume-from-fork reconciliation of the PR write, no marker search, no reservations.
6. `__main__.py`: the linear §3 pipeline, per-candidate error scoping, one publication path. Update `observability/` to the §10 fields, KPIs and
   alerts.
7. Update `tests/` to this contract and add the §13 fault-injection test.
8. Measure `required_contexts` from a probe PR; record it in config and `RESULTS.md`.
9. Run LIVE for one candidate; write `RESULTS.md`; refresh `README.md` (setup, simulate vs live, config table, how to read the reports).

## 13. Definition of done

- One real candidate goes discovery → session → orchestrator-verified red→green → PR → CI green → merged on `victorciao/superset`, with PR URL,
  merge commit and `session_id` recorded on its state row and in `RESULTS.md`.
- Crash recovery is proven by **fault injection**, not inspection: the process is `SIGKILL`ed immediately before and immediately after the single
  PR-creating write, each run is resumed to completion, and afterwards **exactly one PR exists for that candidate** on the fork.
- A full SIMULATE run completes with no credentials and no writes, and under `docker compose`. Every §11 knob is settable without code edits; the
  README documents defaults and safety classes.
- `tests/`, `ruff` and `mypy --strict` are green, pure-logic coverage `>= coverage_bar`; the run report accounts for every candidate and the KPI
  rollup agrees with the fork re-read after the run.

## 14. Deletion list for the implementer

- Delete `review_loop.py` wholesale: `FindingSeverity`, `ReviewFinding`, `ReviewIteration`, `ReviewLoopResult`, `evaluate_review_iteration`, `run_review_loop`, `apply_review_result`, `review_iteration_from_payload`.
- `session_client.py`: `SessionRole`, `ROLE_OUTPUT_SCHEMAS`, `validated_diff_review`, `_candidate_diff_review_matches`, `_validated_diff_review_head`, `_sent_message_timestamp`, `_message_timestamps`, `_message_processed`, `send_message`, `poll_session_after_message`, `RoleCollisionError`, `PlannerOutputError`, `DiffReviewIncompleteError`, `PhaseBCorrelationTimeoutError`, `PhaseBHeadUnavailableError`, `BranchNotAdvancedError`, `RuntimeOrchestrator.run_planner`/`run_implementer`/`run_reviewer`/`inspect_implementer_diff`/`inspect_reviewer_diff`.
- `prompts.py`: `PHASE_B_REVIEWER_OUTPUT_SCHEMA`, `render_planner_prompt`, `render_implementer_prompt`, `render_reviewer_prompt`, `render_reviewer_phase_b_prompt`, `validate_planner_output`, `_findings_text`, `_planner_text`.
- `red_baseline.py`: `DiffInspection`, `classify_implementer_diff`, `inspect_reviewer_diff`, `validate_nested_marker_lifts`, `should_reauthor_baseline`.
- `templates/render.py`: `candidate_marker`, `render_issue_title`, `render_issue_body`, `render_degraded_comment_body`, `validate_issue_body`, `_planner_text`, `_reviewer_text`.
- `github_client.py`: `create_issue`, `patch_issue`, `comment_pr`, `publish_degraded`, the issue path of `publish_artifacts`, and the hard-coded `REQUIRED_CONTEXTS` tuple (moves to config).
- `state.py`: `MarkerSearchOutcome`, `MarkerArtifact`, `github_marker_search`, `marker_artifact`, `marker_exists`, `marker_search_unavailable`, `marker_search_orphaned`, `marker_search_outcome`, and the reservation fields/branches in `append_if_new_artifact`.
- `schemas.py` enums: `CandidateState.ISSUE_CREATED`/`ISSUE_PATCHED`/`COMMENT_CREATED`/`CONVERGED`; `Action.REVIEWER_ONLY_DIFF`; `ReasonCode.DISAGREEMENT_UNRESOLVED`/`DIFF_REVIEW_INCOMPLETE`/`IMPLEMENTER_TEST_EDIT`/`ROLE_COLLISION`/`PHASE_B_CORRELATION_UNAVAILABLE`/`RESERVATION_HELD`/`MARKER_SEARCH_FAILED`/`MARKER_SEARCH_UNCONFIGURED`/`ARTIFACT_DEGRADED`/`ARTIFACT_ORPHANED`/`BRANCH_NOT_ADVANCED`/`ROLE_COMMIT_MISSING`.
- `schemas.py` `Candidate`/`EventRecord` fields: `issue_url`, `issue_number`, `comment_url`, `artifact_degraded`, `planner_session_id`, `implementer_session_id`, `reviewer_session_id`, `planner_criteria`, `reviewer_criterion_ids`, `role_attempt_evidence`, `diff_reviewed`, `reviewed_head_sha`, `iterations`, `disagreement_summary`, `phase_b_protocol_violation`, `marker_search_outcome`, `reserved_at`, `reserved_by_run_id`, `unresolved_major`.
- `config.py`: `iteration_cap`, `issue_sink`/`IssueSink`, `has_issues`, `reservation_lease_s`, `DEFAULT_ITERATION_CAP`, `DEFAULT_MAX_SESSIONS` (`max_sessions` becomes `budget_N`); add `required_contexts`.
- `observability/kpis.py`: `_criterion_coverage` plus the criterion-coverage, `disagreement_unresolved`, sessions-per-role and implementer-test-edit KPIs.
