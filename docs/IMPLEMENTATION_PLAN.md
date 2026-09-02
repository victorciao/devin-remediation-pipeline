# Implementation Plan — Devin Remediation Pipeline for Apache Superset

## 1. Purpose and scope

This repo (REPO A) finds real technical debt in the Superset fork `victorciao/superset` (REPO B), ranks it deterministically, and lands verified fixes there as pull requests. Three lanes are in scope: CodeQL alerts, unconditionally skipped tests, and EOL `@deprecated` removals.

Two loops exist and they are separate:

- **Build time (§3)** — how this repo is built, once, by a planner / implementer / reviewer trio of Devin sessions.
- **Runtime (§4)** — what the pipeline does per candidate, on every run. **High tier: one Devin session and two GitHub artifacts — a tracking issue, then the PR that closes it. Medium tier: one tracking issue, no session and no branch. Low tier: nothing.**

### 1.1 Requirements this repo satisfies

- REPO B is a fork of `apache/superset` (`victorciao/superset`), and every write lands there.
- Issues are created in the fork for the debt that is remediated: a high-tier candidate's issue is opened at dispatch and closed by its PR (§11). Medium-tier issues cover ranked debt held back for a human.
- Runs are event-driven (§5): the fork's CodeQL analysis completing on `master`, a weekly cron, `workflow_dispatch`.
- Devin sessions are created and managed programmatically through the Devin API (§9).
- Outputs are observable by a technical audience: PRs and issues on the fork, the JSONL event log, the KPI rollup, the run report and `RESULTS.md` (§14).
- The repo is public, runs under `docker compose`, and its README covers how to run LIVE and how to SIMULATE (§15).

## 2. Out of scope

- Low-tier candidates are reported only: no session, no artifact.
- No companion artifacts beyond a high-tier candidate's issue and PR: no PR comments, no patching an artifact after it is written.
- No frontend lane: JS/TS alerts are enumerated and reported, never dispatched. No dashboards or hosted services; the outputs are Markdown reports and a JSONL log.

## 3. Build-time loop: planner, implementer, reviewer

Three Devin sessions build this repo, with strict role separation — no session does another's work:

- **Planner** designs: it writes and revises this plan, the interfaces and the task ordering. It writes no production code.
- **Implementer** writes production code under `src/` and `scripts/` to the planned interfaces. It does not author the tests that judge it.
- **Reviewer** is independent: it reads the implementer's **diff** and authors `tests/` from it. A green suite is never evidence that review happened; the diff must be read, and the review names blocking, major and minor findings.

They iterate: implementer fixes, reviewer re-reads the new diff. **Convergence — no blocking and no major finding outstanding — plus green CI on this repo is the precondition for shipping a task.** Minor findings may be accepted with a note. This loop is a working agreement between sessions, not machinery inside the pipeline:
nothing in `src/` implements it, and it does not run per candidate.

## 4. Runtime architecture

The **orchestrator** (this program) owns everything except the fix: discovery, gates, scoring, branch creation, launching the session, verifying the lane's success criterion, PR creation, CI watching, state, KPIs, resume. The **session** writes the fix and its evidence on the candidate branch. **Nothing a session
reports about its own results is ever evidence; only what the orchestrator observes by executing commands or reading the fork counts.** A dispatched candidate gets exactly one session that does the whole job — feasibility, fix, test, run, push, report — with no per-candidate roles, no review loop and no mid-session follow-up
message.

- `lanes/` — `codeql.py`, `skipped_tests.py`, `deprecations.py` enumerate candidates and declare each one's success criterion.
- `gate.py`, `score.py`, `dispatch.py` — gates, deterministic score, tier and per-run budget.
- `session_client.py` — creates and polls exactly one Devin session per dispatched candidate; `verify.py` — evaluates that candidate's declared success criterion (§10) from the orchestrator's own execution.
- `github_client.py` — branch, PR, issue, labels and check-run polling; `templates/render.py` — PR and issue title/body rendering and validation.
- `state.py` — append-only `state/candidates.jsonl`, write-once identities, resume; `observability/` — event log, run report, KPI rollup.

## 5. What triggers a run

A run is triggered by an event on the fork, never by lane state. Every run enumerates all three lanes regardless of which trigger fired; the trigger only says there is a reason to look, and nothing about lane selection is derived from the event.

- **Primary: the fork's `codeql-analysis` workflow completing on `master`.** One event serves all three lanes. It guarantees a scan has just run against the current head, so LANE 1 reads fresh alerts and §7's `base_sha` analysis check matches; and it implies `master` moved, which is the only thing that can change LANE 2 or LANE
  3 discovery, both being AST scans of the checked-out source and pure functions of the code at `base_sha`. Triggering on the push itself would be worse for LANE 1: the run would read the analysis that preceded the push.
- **Weekly cron.** A LANE 3 candidate can become eligible with no code change at all, because a `@deprecated` symbol crosses the EOL threshold when the release number moves. It is also the safety net for a missed event.
- **`workflow_dispatch`** for a manual LIVE run.

All three are LIVE entry points. SIMULATE is invoked as `docker compose run --rm pipeline`, locally, no credentials, no writes, `sarif_file` input. A scheduled SIMULATE smoke test belongs in this repo's own CI, which §17 covers.

## 6. Candidate lifecycle (single state table)

| State | Entered when | Action | Artifact | Terminal |
|---|---|---|---|---|
| `enumerated` | a lane emits it, criterion declared | gate | none | no |
| `gated` | all gates passed | score | none | no |
| `scored` | tier assigned | high tier: publish the tracking issue, then dispatch, if within `budget_N`; medium tier: publish an issue; low tier: report | none | no |
| `issue_created` | issue exists (created or marker-adopted) and its number is recorded | high tier: create the branch and dispatch; medium tier: record KPIs | issue | medium tier: **yes** |
| `dispatching` | branch created, session launched | poll the session | issue + branch | no |
| `session_done` | session terminal with required output | evaluate the criterion's pre-PR evidence (§10) | issue + branch | no |
| `verified` | pre-PR evidence satisfied and recorded | render + create the PR, `Closes #<issue_number>` | issue + branch | no |
| `pr_created` | PR exists and its number is recorded | watch the head's check runs and settle any post-PR criterion (§12) | issue + PR | no |
| `awaiting_human_merge` | §12 gate green and PR remains open | leave open, report it; a human owns merging | PR | **yes** |
| `merged` | an external human merge is observed and verified | record KPIs | PR | **yes** |
| `terminal` | any §13 failure | record reason; never merge | ≤1 issue, ≤1 PR | **yes** |
| `deferred` | budget overflow, session ceiling, transport error | retry next run | ≤1 PR | no |

Terminal reasons: `session_failed`, `session_blocked`, `criterion_not_met`, `invalid_red_baseline`, `stale_skip`, `green_not_reached`, `alert_still_present`, `symbol_still_referenced`, `suite_regressed`, `ci_check_failed`, `ci_evidence_unavailable`, `artifact_validation_failed`, `manual_merge_required`, `human_routed` (a gate
routed it; no artifact is ever written for it). Deferred reasons: `budget_overflow`, `session_ceiling`, and `marker_search_failed` / `marker_search_unconfigured` (issue path only — no issue is written when the marker search cannot run).

## 7. Discovery and gates

**LANE 1 — CodeQL alerts.** `alert_source = code_scanning_api | sarif_file`, default `code_scanning_api`: alerts are read with `GET /repos/{o}/{r}/code-scanning/alerts` for `master`, produced by the fork's own `codeql-analysis` workflow. `sarif_file` (`fixtures/codeql_alerts.json`) is the SIMULATE input. Python only: a path
outside `superset/**/*.py` fails `verifiability_exists` with `out_of_scope_frontend`, gating out 3 of the 11 live JS/TS alerts at `a140e74`.

Code scanning is a store, not a live query, so freshness means an analysis has run against the head this run works from, and that is verified explicitly: `GET /repos/{o}/{r}/code-scanning/analyses` supplies the latest analysis for `master`, and its commit SHA must equal `base_sha`. A mismatch, or no analysis at all, is a startup
error under LIVE — never an empty or stale candidate set read as "no debt found". Freshness for `signal_quality` comes from `alert.updated_at` and that analysis timestamp.

**LANE 2 — skipped tests.** AST enumerator over `tests/` matching **unconditional** `@pytest.mark.skip` / `@unittest.skip` only, decorator names resolved through the module's import bindings; `skipif`/`skipUnless` are excluded as `conditional_environment_guard`, `xfail` as `expected_failure_xfail`; `pytestmark`, in-body
`pytest.skip()` and relative-import aliases are out of scope. Live: **35** included, **33** excluded (30 guards, 3 xfail). Each record carries the collectable nodeid (`path::Class::method`), `class_scope`, `kind`, `enclosed_tests`, `parametrized`, `collects_single_item`, `enclosing_skip_nodeid`.

**LANE 3 — EOL `@deprecated` removals.** AST scan of `superset/**/*.py` yielding `module:qualname`. EOL = `removed_in <=` current version, or, absent `removed_in`, `major(deprecated_in) <= current_major - eol_major_lag` (`eol_major_lag = 2`). `current_major` is the highest concrete release in `version_source` — **6** at
baseline, so the threshold is `major <= 4`; a source with no concrete release is a startup error. Live: 2 EOL sites, 1 automatable (`normalize_indexes`). LANES 2 and 3 are likewise computed fresh on every run — both are AST scans the orchestrator performs over the checked-out fork at `base_sha` — and `fixtures/baseline.json` is
only the SIMULATE input and offline fallback data, never live input.

**Gates** — binary, all must pass, each failure recording its own reason:

1. `trigger_exists` — a machine-readable source record exists.
2. `automatability` — rubric `>= 2` **and** every lane hard condition: LANE 2 breadth (`enclosed_tests > lane2_class_breadth_max = 5` → `class_scope_too_broad`; `enclosed_tests = 0` with no live `pytest --collect-only` count → `class_breadth_unknown`), LANE 2 nesting (`enclosing_skip_nodeid` present →
   `blocked_by_enclosing_skip`), LANE 3 `no_internal_callers_and_no_override_surface` (→ `public_api_surface` / `internal_caller`).
3. `verifiability_exists` — the lane can declare a success criterion (§10) the orchestrator is able to observe for this candidate.

A gate failure is dropped or terminal-`human_routed`; it never produces a GitHub write. Recurrence is neither a gate nor a score input.

## 8. Scoring and tiers

```
score = min( business_impact × verifiability × automatability × signal_quality / max(risk, 1),
             score_cap )
```

Each factor is a 1–5 rubric row from `config/rubrics.yaml`, one table per lane per factor: `business_impact` from alert severity / covered surface / public-API exposure, `signal_quality` from rule precision and freshness / skip-reason specificity / age in majors, `risk` from blast radius / test-only diff, +1 when `kind = class`
/ caller-and-override count.

- `risk` floored at 1, composite capped at `score_cap = 200`. `tier_high_min = 60`, `tier_medium_min = 20`; example: `4×4×4×4 / 2 = 128` → high.
- **High** → dispatch a session and open a PR. The PR's contexts and criterion are still watched and recorded, then the candidate settles `awaiting_human_merge` / `manual_merge_required`; the pipeline never merges pull requests.
- **Medium** → publish a tracking issue (§11): no session, no branch, no PR, no merge. An issue never gates anything and never blocks a PR candidate. **Low** → reported only.
- `budget_N = 5` PRs per run; overflow is `deferred/budget_overflow`, retried next run. Issues are not charged against `budget_N`.

## 9. The session contract

The orchestrator first creates the stable publication prefix `devin/remediation/<candidate_id>` from the target base and pins `base_sha`, then creates one session. Hosted workflows pass the stable `devin/remediation` prefix; the candidate ID suffix makes each branch and PR lookup durable across runs.

The prompt must contain: repo and branch; `base_sha`; lane and locator (alert rule + path + region, or nodeid, or `module:qualname`); the fix objective; **the lane's success criterion verbatim, and the evidence the session must leave behind for it** (§10), including a regression test at the narrowest level that can express the
fix — a unit test is preferred to an integration test, and re-enabling an existing test counts; the command to run it; `git commit --signoff` and push to the candidate branch only; and the prohibition on opening any PR or issue, touching other branches, or editing unrelated tests.

Required `structured_output_schema`:

```
{ files_changed[], test_nodeid: string|null, test_paths[], verify_command, head_sha, suite_scope[],
  fix_summary, testing_notes, criterion_notes, feasible: bool, infeasible_reason: string|null }
```

`test_nodeid` is null only when the lane's criterion does not require a new test and `criterion_notes` says why (§10, LANE 1).

- `feasible = false` with a reason is a legitimate answer: the candidate settles `terminal/session_failed` with that reason and no PR is opened.
- Terminal session status is `finished`, **or** `blocked` whose `structured_output` already carries the required keys; `blocked` without them is `session_blocked`, `expired` fails, and `structured_output` can appear while `working`, so its presence alone is never terminal.
- Every creation passes `idempotent: true`, `tags: ["devin-remediation", candidate_id, "attempt:<n>"]`, an attempt ordinal in the prompt preamble, `session_timeout_s` and `max_acu_limit`. For attempt `> 1` the orchestrator asserts a genuinely new session: `is_new_session` `true` → proceed, `false` → fatal, `null` → compare the
  returned `session_id` with the previous attempt's.

## 10. Verification: per-lane success criteria

Every candidate carries a **success criterion** declared by its lane at enumeration. The orchestrator always evaluates it from its own observation — the session's account is never the evidence — and *where* it is evaluated follows the evidence source. **Pre-PR**, in a local checkout, when the evidence is obtainable there: LANE
2's red-at-base/green-at-head, LANE 3's symbol and caller re-check, and suite-green under `ci_evidence_mode = local`. **Post-PR**, from the fork, when the evidence exists only on the PR ref: LANE 1 under `pr_ref_alerts`, and suite-green under `ci_evidence_mode = actions`. A post-PR criterion is recorded on the PR head: a candidate whose alert is still present at head holds an open PR and settles `terminal/alert_still_present`, and expiry of `alert_analysis_wait_s` settles `terminal/criterion_not_met` — never silently passed. The criterion and its observed outcome go to the state row and
the event log.

**LANE 2 — the test's skip marker is absent at the candidate head and the suite covering the test passes there.**

Integration candidates are unobservable locally because Superset's integration suite requires a provisioned metadata database, Redis, and Celery, so the suite clause is read from the fork's authoritative `test-postgres-required` CI check at the PR head. The accepted limitation is that this CI check is suite-level: it proves the suite containing the re-enabled test passes, not that specific nodeid in isolation. The marker-absence clause rules out a trivially green skipped test. Unit-test candidates remain locally observable where configured; Actions-mode integration candidates defer their pre-PR suite evidence until the PR head.

**LANE 1 — the alert is gone at head and nothing regressed.** The fixed path is `pr_ref_alerts`: the fork's `codeql-analysis` runs on `pull_request`, so alert absence at the candidate head is read from the alerts for the PR ref once that analysis completes, bounded by `alert_analysis_wait_s`; expiry settles
`terminal/criterion_not_met`, never a pass. The alert's `stable_locator` must be absent → otherwise `terminal/alert_still_present`. The suite covering `suite_scope` must pass at head → otherwise
`terminal/suite_regressed`. A regression test is required when the alert class admits one; when it does not — `py/overly-large-range` is such a class — `test_nodeid` is null, the row records why, and alert-absence plus suite-green is the whole criterion. Discovery reads the analysis at `base_sha` and verification reads the
analysis at the candidate head; the orchestrator reads both itself and never accepts the session's account of either. Under `pr_ref_alerts` the candidate therefore reaches a PR on the strength of the session's fix plus suite-green, with alert-absence confirmed afterwards — a deliberate consequence of GitHub owning the scan, and
it costs nothing that matters because a human owns the merge after that confirmation.

**LANE 3 — the symbol is gone and nothing references it.** At head the `module:qualname` must no longer resolve and the gate's `no_internal_callers_and_no_override_surface` check, re-run at head, must hold → otherwise `terminal/symbol_still_referenced`. The suite covering `suite_scope` must pass at head → otherwise
`terminal/suite_regressed`. No new test is required; a deletion's evidence is that nothing broke.

Suite-green evidence comes from the orchestrator's own run when `ci_evidence_mode = local`, or from the fork's `Python-Unit` context on the PR head when `ci_evidence_mode = actions` — in which case the criterion is completed after the PR exists. Any criterion the orchestrator cannot observe at
all settles `terminal/criterion_not_met` with what was attempted.

## 11. Publication

A high-tier candidate has exactly **two writes in a fixed order: the tracking issue, then the PR**. The issue is created at dispatch time, before the session, and states the debt the pipeline intends to remediate; it carries the same hidden marker and the same lane / locator / score / factor-breakdown body as a medium-tier
issue. The PR body then contains `Closes #<issue_number>`, which cross-links both directions and closes the issue on merge, so **there is deliberately no third write**: no issue patch, no PR link written back to the issue. That patch write is what would reopen a crash window, and `Closes` removes the need for it. If the issue
write succeeds and the candidate later fails its criterion, the issue stays open with no PR — a correct outcome, recorded as such on the state row and in the report. Medium tier is issue-only: no session, no branch, no PR, no merge.

Each run reconciles both artifacts against the fork before writing either. Duplicate detection differs between them, and the asymmetry is deliberate:

- **PR path (high tier)** — one exact query taken immediately before the write: `GET /repos/{o}/{r}/pulls?state=all&head=<owner>:<candidate branch>`. A match is adopted, never duplicated. No marker. The stable branch prefix plus `candidate_id` suffix makes the lookup durable across hosted runs, so at most one PR can ever exist per candidate.
- **Issue path (both tiers)** — there is no branch to key on, so the issue body carries a hidden HTML-comment marker holding `candidate_id`, and dedupe is a GitHub issue search for that marker. Search indexing lags: measured on this fork at up to **~17 s**, so a crash after creating an issue but before its state row lands can
  let a later run create a second issue. **This residual window is a known, accepted, bounded risk: the blast radius is one duplicate issue, never a duplicate PR and never a merge.** Resume searches the marker first, adopts a found issue (recording its number and URL), and creates one only when the search returns nothing; a
  search that errors or is unconfigured defers the candidate and writes nothing. No write-intent rows, no reservation leases.

Before hosted execution, committed run directories under `history/` are restored into the live append-only state file in chronological order; seed keeps one last-write-wins row per candidate and publication exports by the seeded line offset, not by inferring `run_id`. Previously settled candidates are reconciled from that state and skipped, never redispatched. The issue body renders the fork's issue template heading set, the marker, the lane, the locator and the score with its factor breakdown; a medium-tier body adds why the candidate was not automated. Its title obeys the PR title regex.

Run-history publication authenticates with `REMEDIATION_GITHUB_PAT` because the default workflow token is read-only; a publication failure annotates the run without failing remediation.

The PR body renders Superset's `.github/PULL_REQUEST_TEMPLATE.md` heading set verbatim and in order — `SUMMARY`, `BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF` (`n/a` for backend fixes), `TESTING INSTRUCTIONS`, `ADDITIONAL INFORMATION` with its checkbox block — plus an `EVIDENCE` section after `SUMMARY` stating the criterion and the
commands the orchestrator ran with their outcomes, and a config-gated `AUTOMATION METADATA` section last. The body states that every commit carries the `Signed-off-by` trailer and that a human owns the merge; the pipeline never merges. A body failing section presence/order validation defers the candidate with
`artifact_validation_failed` **before** any write; a security-lane body carries rule ID and file scope only, never exploit detail. The title must match the fork's `pr-lint` regex, pinned in `templates/superset/pr_title_regex.txt`: `^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+`

## 12. Merge gate

The gate is the fork's own CI, read from the **check runs on the PR head**. Superset's workflows are change-filtered — a Python-only fix never runs the `Frontend` jobs, and a check that does not run reports `skipped`, not `success` — so the gate is a conclusion rule, not a fixed context list:

1. Poll the head's check runs until none is `queued` or `in_progress`, bounded by `ci_wait_timeout_s` (default `5400`); expiry is `ci_evidence_unavailable`.
2. Every check run that reached a conclusion must be `success`. `skipped` and `neutral` are permitted and are named as such on the row and in the report. Any `failure`, `cancelled` or `timed_out` is `ci_check_failed`.
3. Every context in `required_contexts_min` must be **present and successful** on the head, so an empty or all-skipped check list can never read as green. `pre-commit checks` is the expected member; the value is measured from a probe PR on the fork.
4. A PR held in pending workflow approval is `ci_evidence_unavailable`.
5. Any post-PR criterion (§10) must be satisfied on the head, alongside the conclusion rule. `terminal/alert_still_present` and `alert_analysis_wait_s` expiry (`terminal/criterion_not_met`) block the merge exactly as `ci_evidence_unavailable` does.

`docker`, `showtime-trigger` and `check-python-deps` need real repository secrets and are never members of `required_contexts_min`; if they run and conclude `failure` they are a genuine `ci_check_failed`. Nothing merges on `ci_evidence_unavailable`, `ci_check_failed` or an unsatisfied post-PR criterion. There is **no**
merge-time re-run of the suite — CI covers that. The pipeline never merges; once this gate and the post-PR criteria hold, the candidate settles `awaiting_human_merge` and an external human merge may later be recorded as observed evidence.

## 13. Failure and recovery

Every failure is terminal, leaves **at most one artifact** on the fork, and never merges:

| Failure | State / reason | Report says |
|---|---|---|
| Session errors, expires, or answers `feasible = false` | `terminal/session_failed` | not remediated, with the session's reason; branch left behind, no PR |
| Session `blocked` without the required output | `terminal/session_blocked` | not remediated, reason `session_blocked`; no PR |
| LANE 2 test not red at `base_sha` | `terminal/invalid_red_baseline` or `terminal/stale_skip` | the per-item outcome vector; no PR |
| LANE 2 test not green at head | `terminal/green_not_reached` | the failure signature; no PR |
| LANE 1 alert still present at head | `terminal/alert_still_present` | the locator still reported by the re-scan; no PR |
| LANE 3 symbol or caller surface remains | `terminal/symbol_still_referenced` | what still resolves or calls it; no PR |
| Suite over `suite_scope` fails at head | `terminal/suite_regressed` | the failing nodeids; no PR |
| Criterion cannot be observed at all | `terminal/criterion_not_met` | the criterion and what was attempted; no PR |
| A check run concludes `failure`/`cancelled`/`timed_out`, or `required_contexts_min` is absent or unsuccessful | `terminal/ci_check_failed` | PR URL as open-not-merged, with the offending check runs |
| Check runs never settle within `ci_wait_timeout_s`, or the PR waits on workflow approval | `terminal/ci_evidence_unavailable` | PR URL as open-not-merged, with the unsettled check runs |
| PR passes the post-PR gate and remains open | `awaiting_human_merge` / `manual_merge_required` | PR URL, gate result, merge owned by a human |
| A persisted `awaiting_human_merge` or terminal PR is observed as merged | `merged` | externally observed `merged_at` and verified merge evidence |
| A persisted `awaiting_human_merge` or terminal PR is observed closed without a merge | `terminal/closed_pull_request` | PR URL and close outcome |
| Issue marker search errors or is unconfigured | `deferred/marker_search_failed` / `deferred/marker_search_unconfigured` | not published this run; retried next run, no issue written |
| High-tier candidate fails its criterion after its issue was written | the criterion's terminal reason, with `issue_url` retained | issue URL as open-unremediated; no PR |

**The one non-atomic step is: create the PR, then record its number.** A crash between them leaves a PR no state row names. Resume settles it from the fork: for any candidate whose last row is `verified` or `pr_created` without a `pr_number`, resume queries `GET /pulls?state=all&head=<candidate branch>` and adopts the single
match (recording `pr_number`, `pr_url`, `head_sha`) before doing anything else; no match means the write never landed and publication is retried. Because the branch name derives from `candidate_id` the query is exact, so at most one PR can ever exist per candidate. The issue write of either tier reconciles marker-first instead,
and its accepted residual window is stated once in §11.

State rows are append-only, last-write-wins per `candidate_id`; durable identities (`pr_url`, `pr_number`, `issue_url`, `issue_number`, `head_sha`, `merged_at`) are write-once-non-null and replacing a non-null value with `None` raises `StatePreservationError`. Once an artifact-backed row is settled, later writes must remain settled and a merged row is final; a blocked write is caught and reported as a candidate deferral while the persisted settlement remains intact. "Already published" is decided by artifact proof — a persisted number
or a fork match — never by a state value alone. A per-candidate failure defers or terminates that candidate only; the run continues to publication and reporting.

## 14. State and observability

- `state/candidates.jsonl` is the dedupe/resume source of truth. For LANE 1, `candidate_id = sha256(lane | repo | rule_id | path | normalized_symbol)`; the identity scheme is `rule_id|path|normalized_symbol`, while `position_digest` remains metadata for drift evidence only. A position-based identity previously produced two candidates for one defect, represented by fork PRs #38 and #40, so positions must not define identity. LANE 2 uses the collectable nodeid and LANE 3 uses `module:qualname`.
- **Drift** — an edit above an alert shifts its digest, so before dispatching a LANE 1 candidate the orchestrator attempts a drift match against state: the weak key `(rule_id, file_path, normalized_symbol)` must have multiplicity 1 among current alerts *and* among active (not `superseded_by`) rows, and the persisted
  `region_digest` or `symbol_relative_offset` must agree. A hit links the rows (`supersedes` / `superseded_by`) and suppresses re-dispatch; neither condition is configurable. Because LANE 1 reads a fresh analysis every run, positions move between analyses and drift matching is load-bearing rather than an edge case: it is what
  stops a known alert being re-dispatched under a new `candidate_id`.
- **Layer 1** JSONL event log per candidate: `run_id`, lane, `candidate_id`, gate results and failed gate, score with factor breakdown, tier, `session_id`, the declared criterion and its observed evidence, `test_nodeid`/`test_paths`/`suite_scope`, PR or issue URL and number, every check run's name and conclusion,
  terminal state and reason, both artifact identities where a high-tier candidate has an issue and a PR, LANE 2 breadth fields, `related_candidate_id`.
- Failed sessions retain their observed `session_id` on terminal and deferred rows; retries may replace that ID, so it is evidence rather than durable identity.
- **Layer 2** `reports/run-<run_id>.md`: candidates seen, gated out with reasons, scored, dispatched, deferred, every terminal candidate with its reason, PR and issue links, merge results, `skipped`/`neutral` check runs named, and every `awaiting_human_merge` PR called out as awaiting a human. No candidate may be unaccounted
  for.
- **Layer 3** `reports/kpis.md`: observed human-merge rate (verified externally merged PRs over published PRs); **verification pass rate = candidates whose declared criterion was satisfied / candidates dispatched**, reported overall and per lane since the criteria differ; test-inclusion rate over the candidates whose criterion required a test; issues created and issues adopted, split by tier, with high-tier issues closed by their merged PR counted separately; session-failure rate; deferred-by-reason. Alerts when
  merge rate `< merge_rate_floor = 0.50`, verification pass rate `< verification_pass_rate_floor = 0.80`, publication safety undetermined, or session-failure rate `> session_failure_ceiling = 0.30`. Verification pass rate uses dispatched candidates evidenced by persisted `head_branch` and the current run's event IDs, and is reported overall and per lane. SIMULATE preserves `unconfigured` marker outcomes because it does not perform a search; only LIVE `unconfigured` outcomes contribute to publication-safety uncertainty, while failed and orphaned outcomes count in every mode.
- Cross-run `RESULTS.md` includes a per-run flow table with one row per run: problems seen, first seen here, issues created, sessions, PRs opened, criterion satisfied, and session failures. A candidate is attributed to the first run in which it appears; unavailable evidence is `n/a`, not `0`.
- In KPI output, `Problems With Pull Request` counts candidates with a PR, while `Pull Requests Opened` counts distinct `pr_url` values; they differ when one problem gets a second PR.
- The run prints a one-line verdict with mode, problems, scored, dispatched, deferred, and the reports directory from the same `RunSummary` used by the per-run report, and explicitly says when duplicate detection was unavailable. Reading state written by an incompatible version raises `StateCompatibilityError` naming the file and directing the reader to a fresh output directory; there is no implicit migration.

## 15. Configuration and modes

| Knob | Default | Notes |
|---|---|---|
| `mode` | `simulate` | **safety-relevant** — `live` must be explicit; unset/empty/unknown → `simulate`, logged |
| `budget_N` | `5` | high-tier candidates per run, each an issue plus a PR — it bounds what a human has to review; **safety-relevant** — clamped at `BUDGET_HARD_MAX = 25` so a misconfigured knob cannot produce a large run, logging `guardrail_clamped` |
| `max_sessions` | `budget_N + 3` | **safety-relevant** — per-run ceiling on Devin sessions created; it bounds what a run costs. Not the same ceiling as `budget_N`: a session can end infeasible, error or blocked and produce no PR, and a retried candidate consumes a second session, so sessions ≥ PRs. Hitting it defers the remaining candidates while the run still publishes and reports what it holds |
| `score_cap` / `tier_high_min` / `tier_medium_min` | `200` / `60` / `20` | `tier_high_min > tier_medium_min` |
| `eol_major_lag` / `version_source` | `2` / `.github/ISSUE_TEMPLATE/bug-report.yml` | drift-tested; no concrete release → startup error |
| `lane2_class_breadth_max` | `5` | **safety-relevant** — §7 breadth ceiling |
| `alert_source` / `alert_fixture_path` | `code_scanning_api` / `fixtures/codeql_alerts.json` | §7 — `code_scanning_api` reads the fork's alerts for `master` and requires the latest analysis to sit on `base_sha`; a mismatch or missing analysis is a startup error under LIVE; `sarif_file` is the SIMULATE input |
| `alert_analysis_wait_s` / `ci_evidence_mode` | `2700` / `local` | §10 — `pr_ref_alerts` reads alerts for the PR ref once the fork's `codeql-analysis` has analysed that head, bounded by the wait; expiry settles `terminal/criterion_not_met` |
| `required_contexts_min` / `ci_wait_timeout_s` | probe-measured (`pre-commit checks`) / `5400` | **safety-relevant** — §12; must be present and successful on the head; expiry → `ci_evidence_unavailable` |
| `has_issues` / `marker_search_enabled` | probed / `true` | **safety-relevant** — issues disabled on the fork or search unavailable → defer, never write, and a high-tier candidate is deferred before its session is created |
| `session_timeout_s` / `max_acu_limit` / `max_total_acu` | `5400` / per-session / `500` | — |
| `merge_rate_floor` / `verification_pass_rate_floor` / `session_failure_ceiling` | `0.50` / `0.80` / `0.30` | KPI thresholds are always written to local artifacts |
| `verification_pass_rate_alert` / `publication_safety_alert` | derived | Alert when verification pass rate is below its floor or publication safety is undetermined |
| Unit-test coverage floor | `80%` | This repository's own unit-test coverage floor is `[tool.coverage.report] fail_under = 80` in `pyproject.toml`, enforced by `pytest --cov` in CI over the six pure-logic modules, currently approximately 94%; it is not a configurable pipeline setting |

`--repo-path` has no default. When it is absent, SIMULATE falls back to the baseline inventory and records that fallback; LIVE aborts with an actionable message. `docker-compose.yml` requires `SUPERSET_CHECKOUT`.

Also configurable: target `owner/repo`, rubrics, templates, and the environment-only GitHub and Devin credentials. **SIMULATE** (default) needs **no credentials**, makes **no** network writes and creates no sessions: discovery from fixtures, gates, scoring, rendering, reporting; every artifact is marked `artifact_simulated = true` with `writes_suppressed
= <n>`, and any attempted write raises. `docker compose run --rm remediation` must complete a full SIMULATE run offline; the image needs no CodeQL toolchain, since the fork's workflow performs every scan.

**LIVE preconditions**, all checked before the first write, each failure aborting before it: `mode = live` supplied explicitly; `GITHUB_PAT_REMEDIATION` and `DEVIN_API_KEY` environment credentials present; token identity and scopes recorded from `GET /user`; `GET /repos/{o}/{r}` reachable with push access; `codeql-analysis` enabled on the fork, with its latest
`master` analysis sitting on `base_sha`; Actions enabled with ≥1 completed `pull_request` run; `required_contexts_min` non-empty; issue search reachable when medium-tier candidates are present.

## 16. Ordered task list

1. Strip the §18 dead modules, symbols and config keys; keep the suite importable.
2. `session_client.py`: one runtime role — create, poll to terminal, validate the §9 schema, attempt/retry assertion, session ceiling. Add `prompts.render_fix_prompt` building the §9 prompt, criterion included, from a `Candidate`.
3. Publication order for high tier: `create_issue` at dispatch, then the PR with `Closes #<issue_number>`; both reconciled against the fork first (§11), no third write. Lanes declare a `success_criterion` per candidate; `schemas.py` carries it plus `suite_scope` and the observed evidence.
4. `lanes/codeql.py`: `alert_source = code_scanning_api` as the default — alerts for `master` plus the `GET /code-scanning/analyses` check that the latest analysis sits on `base_sha`, a startup error otherwise; `sarif_file` kept as the SIMULATE input.
5. Add `verify.py` — one evaluator per lane criterion (§10): LANE 2 red-at-base/green-at-head with per-item vectors, LANE 1 alert re-check plus suite, LANE 3 symbol and caller re-check plus suite; each returns evidence or a terminal reason.
6. `github_client.py`: `pull_request_for_head` as the PR dedupe path; marker search plus `create_issue` for both tiers; check-run polling and the §12 rule. `templates/render.py`: PR body/title with `EVIDENCE`, issue body/title with the marker.
7. `state.py`: the §6 state set, `issue_created` on the high-tier path as well, `awaiting_human_merge`, resume-from-fork reconciliation of the PR write and marker-first reconciliation of the issue write.
8. `__main__.py`: the linear §6 pipeline, per-candidate error scoping, one publication path. Update `observability/` to the §14 fields, KPIs and alerts.
9. Update `tests/` to this contract and add the §17 fault-injection test.
10. Measure `required_contexts_min` from a probe PR, and record the observed check-run set (including `skipped` members) in config and `RESULTS.md`.
11. Run LIVE for one candidate; write `RESULTS.md`; refresh `README.md` (setup, simulate vs live, config table, how to read the reports).

## 17. Definition of done

### 17.1 Working gate — the system is declared working here

This is the acceptance gate for declaring the system working end to end, and its contents are fixed in advance: "finite" means only that no criterion may be added to it mid-flight, so reaching it cannot be deferred by widening it. It says nothing about code review. §3 is untouched and still binding: every task, including the
ones that make up this gate, ships only on convergence plus green CI on this repo, and a blocking or major finding against code still in flight is resolved before that code ships. A finding that arrives after the gate has been met is a listed follow-up (§17.2) rather than a reason to reopen the gate.

- One real Python candidate goes discovery → tracking issue → session → orchestrator-verified criterion → PR closing that issue → CI green → `awaiting_human_merge` on `victorciao/superset`; the pipeline never merges, and any later human merge is recorded only as externally observed evidence with issue URL, PR URL, merge commit and `session_id` on its state row and in `RESULTS.md`.
- Crash recovery is proven by **real OS-process fault injection** in `tests/integration/test_crash_recovery_fault_injection.py`, not inspection: the process is `SIGKILL`ed immediately before and immediately after the PR-creating write, each run is resumed to completion, and afterwards **exactly one PR exists for that candidate** on the fork.
- A full SIMULATE run completes with no credentials and no writes, and under `docker compose`. Every §15 knob is settable without code edits; the README documents defaults and safety classes.
- `tests/`, `ruff` and `mypy --strict` are green; `[tool.coverage.report] fail_under = 80` enforces pure-logic coverage, currently approximately 94% across the six covered modules; the run report accounts for every candidate and the KPI rollup agrees with the fork re-read after the run.

### 17.2 Future improvements — not yet implemented

- Concurrent Devin sessions. Today `settled = [runner.process(candidate) for candidate in dispatched]` is strictly serial; parallelizing would overlap per-candidate CI waits and introduce ACU-ceiling accounting races, requiring a runner-loop redesign.
- Auto-merge. The pipeline never merges pull requests; merging on the pipeline's own judgement would need a merge policy and a human-accountable owner for it.
- A GSheet KPI sink. KPIs are always written locally; an external sink needs credential handling and a retention owner.
- A CodeQL CLI alert check. LANE 1 uses PR-reference alert evidence; running the CLI in-pipeline needs the toolchain in the image and a database-build budget.
