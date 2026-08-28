# Implementation Plan — Event-Driven Devin Remediation Pipeline for Apache Superset

> **Status:** version-controlled source of truth. This document is what the build-time
> plan-review step (T-1, §2) reviews against, and what every later change must be
> reconciled with. Referenced from the README.
>
> **Revision 3** — incorporates T-1 review round 1 (5 blocking / 15 major / 10 minor / 2 nit)
> and round 2 (7 major / 8 minor / 2 nit). See §20 for the revision log and the
> finding-by-finding disposition of both rounds.

Produce the deliverables for an event-driven Devin remediation pipeline that finds, ranks, and
remediates issues in Apache Superset, opening cross-linked PRs + issues with observability.
This document is committed into REPO A as `docs/IMPLEMENTATION_PLAN.md` before build begins.

## Two repositories

- **REPO A — the solution** (new public GitHub repo, e.g. `devin-remediation-pipeline`):
  standalone, Dockerized; contains pipeline code, README, `docs/IMPLEMENTATION_PLAN.md`,
  templates, tests, observability outputs. Do **not** nest inside the Superset fork.
- **REPO B — a fork/copy of Apache Superset** (existing `victorciao/superset` fork of
  `apache/superset`): the TARGET the pipeline acts on. Selected + remediated issues land here
  as real PRs + companion issues (the evidence).

**Critical ordering:** complete Phase 0 (discovery) before writing pipeline code. Do not build
components that depend on an unresolved unknown.

---

## §1 Glossary

- **CANDIDATE** — a single potential remediation surfaced by a lane (one CodeQL alert, one
  skipped test, one EOL deprecation), represented by the `Candidate` schema.
- **LANE** — a pluggable module that enumerates candidates from one source (CodeQL,
  skipped-test backlog, EOL deprecation).
- **GATE** — binary pass/fail preconditions a candidate must ALL pass to be scored.
- **SCORE** — a numeric priority computed only for gate-passing candidates.
- **TIER** — the dispatch bucket a score maps to (high / medium / low), deciding what artifact
  is produced.
- **RUN** — one end-to-end execution of the pipeline (triggered or scheduled).
- **RUNTIME FLOW** — the pipeline's per-candidate PLANNER → IMPLEMENTER + REVIEWER(tests) →
  code-review loop remediation process against the Superset fork. Has **no** plan-review step.
- **BUILD-TIME SDLC** — the reviewer-reviews-plan → implement-while-reviewer-writes-tests →
  code-review loop → push process for constructing REPO A. **Has** a plan-review step.
  Distinct from RUNTIME FLOW; never conflate the two.

---

## §2 Build-time SDLC (how the Devin session constructs REPO A — runs once)

The implementing Devin session(s) build REPO A via this workflow:

1. **PLAN REVIEW (T-1)** — a reviewer agent reviews the committed `docs/IMPLEMENTATION_PLAN.md`
   BEFORE any build. Blocking/major plan findings are resolved by the **human owner** (or a
   dedicated PLANNER session, if one is created — build-time has no standing planner role) and
   committed back to `docs/IMPLEMENTATION_PLAN.md` so history is auditable. Bounded by
   `iteration_cap = 5`, then human escalation. *(m-08)*
2. **IMPLEMENT + TEST CONCURRENTLY** — once the plan is approved, an IMPLEMENTER agent builds
   REPO A modules while a distinct REVIEWER agent independently authors REPO A's own tests.
3. **CODE-REVIEW LOOP** — reviewer/implementer iterate until no blocking/major findings remain,
   bounded by `iteration_cap = 5`; hitting the cap escalates to a human.
4. **PUSH** — push PR(s) on REPO A only after the loop converges AND REPO A CI is green.

Build-time reviewer and implementer MUST be distinct sessions (collision = configuration
error). The implementer must not author/edit build-time tests (an implementer edit →
`needs-human-review`), mirroring the runtime rule.

---

## §3 Phase 0 — Discovery (blocking; resolve unknowns before building)

- **0a** — Read the official Devin API docs; resolve session-creation endpoints, auth, and
  payload shapes. Do NOT guess — build against confirmed API.
- **0b** — Authenticate to GitHub; confirm token scopes: `security_events` (read CodeQL),
  `issues`/`repo` (read+write for issues/PRs), `actions` (read CI history), plus PR read for
  merge state. Confirm target `owner/repo`.
- **0c** — Enumerate LIVE data for the fork — CodeQL alerts, open issues, CI flake/skip history
  — and capture a BASELINE snapshot so backlog burn-down is measurable. Build a SIMULATE
  fixture from this snapshot so a credential-less reviewer can run the whole flow.
- **Review pause** — if scope shifts from assumptions (e.g. no CodeQL alerts present, or a
  missing token scope), pause for human review before Phase 1 build.

### 0d — Target-repo capability preconditions (blocking exit criteria)

A lane or dispatch path whose precondition is unmet is disabled for the run and recorded as
`capability_unavailable`; it never silently no-ops. *(B-01, B-02, B-03)*

| Precondition | Check | If unmet |
|---|---|---|
| Issues enabled | `GET /repos/{o}/{r}` → `has_issues == true` | dispatch preflight aborts **before any write**; degraded path `issue_sink = pr_comment` (§7) |
| Actions **can run**, not merely registered *(R2-M-06)* | `GET /actions/workflows` → `total_count > 0` **and** `GET /actions/runs?event=pull_request` (or `workflow_dispatch`) → ≥1 `completed` run | `ci_evidence_mode` falls back to `local` (§10); auto-merge hard-disabled |
| Code scanning available | `GET /code-scanning/alerts`; outcome split by status *(R2-m-03)* — `200` → available; `403` → `token_capability_missing` (hard stop, same row as below); `404` / empty analysis → `capability_unavailable` | LANE 1 falls back to `alert_source = sarif_file` (§5); no live LANE 1 evidence |
| Token capability | `repo` (or fine-grained Contents/Issues/PR/Actions/Code-scanning write). The probe records the **token identity** (`GET /user` login + `x-oauth-scopes`) in the event log so a permissions failure is never misread as an absent analysis *(R2-m-03)* | hard stop |

### 0e — Recorded Phase 0 outcome for `victorciao/superset`

Captured at `HEAD = a140e74`, snapshot committed to `fixtures/baseline.json`:

- Issues were **disabled** and Actions had **0 runs / 0 registered workflows**; code scanning
  returned `no analysis found`. All three were remediated during Phase 0 by the repo owner's
  token: Issues enabled, Actions enabled (`allowed_actions: all`, 49 workflows registered),
  and **CodeQL default setup configured** (`python`, `javascript-typescript`, default suite).
- The resulting first CodeQL analysis produced **11 open alerts**, committed to
  `fixtures/codeql_alerts.json` — LANE 1 therefore has live data. Rule mix:
  `py/stack-trace-exposure` (×2), `py/overly-large-range` (×4), `py/url-redirection` (×2),
  `js/xss`, `js/xss-through-exception`, `js/clear-text-storage-of-sensitive-data`.
- **34** unconditional skip sites under `tests/`, **as produced by the §5 LANE 2 enumerator**
  *(R2-M-04)*, plus **33** conditional sites recorded separately in
  `baseline.excluded_conditional_skips` with `excluded_reason: conditional_environment_guard`
  (every `skipif`/`skipUnless` in the tree is an availability or backend guard, and `xfail` is
  an expected-failure signal, not disabled coverage — see §5).
- **4** `@deprecated(deprecated_in=...)` sites, none carrying `removed_in`, of which **2** are
  EOL-passed under §4.2 (`normalize_indexes` at `3.0`,
  `DatabaseRestApi.table_extra_metadata_deprecated` at `4.0`).
- **Baseline validity** *(R2-M-03)* — the snapshot is regenerated **after** the capability fixes
  and records `baseline_valid_lanes: [codeql, skipped_tests, deprecations]` plus
  `current_release`, `current_major`, `eol_threshold_major` and `version_source`. Burn-down for
  any lane absent from `baseline_valid_lanes` is reported `n/a`, never as a fall from zero (§11).
- **CodeQL default setup**, read with the owner token *(R2-n-02)*: `state: configured`,
  `query_suite: default`, `schedule: weekly`, languages `javascript`, `javascript-typescript`,
  `python`, `typescript` — recorded in `docs/PHASE0_DISCOVERY.md`.
- **Actions runs: 1** (`CodeQL Setup`, event `dynamic`). No `pull_request` or
  `workflow_dispatch` run has ever completed, so the strengthened 0d row above resolves
  `ci_evidence_mode = local` for the evidence run *(R2-M-06)*.

---

## §4 Two-stage selection: GATE → SCORE

### Stage 1 — GATE (binary; must pass ALL, else dropped or human-routed)

1. `trigger_exists` — a machine-readable event/source exists (CodeQL SARIF alert, nightly-CI
   skip/failure record, parseable `@deprecated(deprecated_in=...)`).
2. `automatability` — the fix can be expressed as a well-scoped transformation, not open
   product judgment.
3. `verifiability_exists` — a concrete pass/fail signal exists (a targeted test path to run).

Recurrence/frequency is NOT a gate or score factor — it is used only when choosing WHICH LANES
to build.

### Stage 2 — SCORE (only for gate-passing candidates)

```
score = min( business_impact × verifiability × automatability × signal_quality / max(risk, 1),
             score_cap )
```

- Each factor on a 1–5 rubric (per-lane rubric tables live in config; see §4.1).
- `risk` is floored at 1 and the composite capped at `score_cap = 200`, so a near-zero risk
  estimate can't make a trivial change outrank a genuine security fix.
- **Tier thresholds** *(B-05)* — `tier_high_min = 60`, `tier_medium_min = 20`; below
  `tier_medium_min` → low → log/drop. Worked example: `4 × 4 × 4 × 4 / max(2,1) = 128` → high.
  The thresholds and the cap are tunable knobs (§13); the tier → action mapping in §6 is not.

### §4.1 Gate vs. score, and the per-lane rubrics

`automatability` and `verifiability` appear in both stages, and they are **not** the same
judgment *(M-15)*:

- The **gate** asks whether *any* well-scoped transformation / pass-fail signal exists at all —
  binary, equivalent to rubric `>= 2`. A rubric `1` fails the gate and never reaches scoring.
- The **score factor** grades *how cleanly*, on `2..5`. Because gate-failing `1`s are already
  removed, the factor carries only residual quality information and cannot double-count a
  no/yes decision.

Per-lane rubric tables are config data (`config/rubrics.yaml`), one table per lane per factor,
each row mapping an observable property to a value. Defaults:

| Lane | `business_impact` anchor | `signal_quality` anchor | `risk` anchor |
|---|---|---|---|
| 1 — CodeQL | alert `security_severity_level` (critical 5 … note 1) | rule precision + `updated_at` freshness | blast radius of the touched module |
| 2 — skipped test | breadth of the covered surface | skip `reason` specificity (a `TODO:` with a cause = 4; bare skip = 2) | test-only diff ⇒ 1–2 |
| 3 — deprecation | public-API exposure | age in majors past `deprecated_in` | caller/override count (see M-02 gate) |

### §4.2 EOL definition for LANE 3 *(M-02, R2-M-02)*

No `@deprecated` site in the target repo carries `removed_in`, so EOL must be derived:

> **EOL** = `removed_in` present and `<=` current version, **or** (no `removed_in`)
> `major(deprecated_in) <= current_major - eol_major_lag`, with `eol_major_lag = 2`.

**Version source** *(R2-M-02)* — the target declares no usable version at rest:
`pyproject.toml` uses `dynamic = ["version"]` and `superset-frontend/package.json` says
`0.0.0-dev`, which would make `current_major = 0` and the rule select nothing. `current_major`
is therefore read from the highest concrete release offered by the `superset-version` dropdown
in `.github/ISSUE_TEMPLATE/bug-report.yml` — the only in-repo enumeration of released versions,
maintained per release. At `a140e74` that is **6.1.0 → `current_major = 6`**, so the EOL
threshold is `major <= 4`. The source path is a config value (`version_source`), the resolved
value is recorded in `fixtures/baseline.json`, and a §17 test asserts a drift failure (rather
than a silent empty lane) if the dropdown stops yielding a concrete release.

Additional hard gate `no_internal_callers_and_no_override_surface`: a symbol still called inside
`superset/`, or named as an override point by `superset/db_engine_specs/lib.py` or
`superset/db_engine_specs/README.md`, fails `automatability` and is human-routed. Applying both
rules at `a140e74`:

| Site | `deprecated_in` | EOL (`<= 4`) | Caller/override gate | Outcome |
|---|---|---|---|---|
| `superset.db_engine_specs.base:BaseEngineSpec.normalize_indexes` | `3.0` | pass | pass | **qualifying demo candidate** |
| `superset.databases.api:DatabaseRestApi.table_extra_metadata_deprecated` | `4.0` | pass | **fail** — a routed public REST endpoint (removal is an API break needing a SIP) | human-routed, `public_api_surface` |
| `…base:BaseEngineSpec.get_url_for_impersonation` | `6.0.0` | fail | fail (called at `base.py:2306`, checked by `lib.py:145`) | dropped |
| `…base:BaseEngineSpec.update_impersonation_config` | `6.0.0` | fail | — | dropped |

So the lane has two EOL-passed sites and one automatable candidate; this closes former open
question 3.

---

## §5 Lanes (first two are the required demo lanes)

- **LANE 1 — CodeQL security alerts.** `alert_source = api | sarif_file` *(B-03)*. In `api`
  mode the lane reads `GET /repos/{owner}/{repo}/code-scanning/alerts`; in `sarif_file` mode it
  reads a committed SARIF/alert fixture (`fixtures/codeql_alerts.json`, captured live from the
  fork, or a SARIF produced by the CodeQL CLI) so the lane is exercisable without code-scanning
  access. Trigger anchor: CodeQL is **scheduled** (upstream `codeql-analysis.yml` cron
  `0 4 * * *`; the fork's default setup reports `schedule: weekly` — verified with the owner
  token, *(R2-n-02)*), and analysis is gated behind a
  python/frontend change detector — so the lane uses `alert.updated_at` as the freshness
  signal, never the cron. *(m-02)*
  **Scope** *(R2-m-04)*: this iteration verifies Python only. A LANE 1 candidate whose alert
  path is outside `superset/**/*.py` fails `verifiability_exists` with reason
  `out_of_scope_frontend` — that gates out the three JS/TS alerts
  (`js/xss`, `js/xss-through-exception`, `js/clear-text-storage-of-sensitive-data`) consistently
  with the §18 non-goal, rather than leaving them undefined.
- **LANE 2 — skipped/flaky-test backlog** (`tests/integration_tests/sqllab_tests.py` and the
  wider `tests/` tree); re-enable and verify. **Enumerator scope** *(M-01, R2-M-04)*: match
  `@pytest.mark.skip` and `@unittest.skip` — unconditional skips **only**. Excluded, and counted
  separately so the exclusion is auditable: `skipif` / `skipUnless` (every one of the target's
  sites is an availability or backend guard — `ocient_is_installed()`, `only_postgresql`,
  marshmallow-version, perf-suite — i.e. correct by design, not debt) and `xfail` (an expected
  failure that is still collected and reported, not disabled coverage). The enumerator and
  `scripts/build_baseline.py` share this definition, and a §17 test asserts
  `enumerator_count == baseline.totals.skipped_tests` so the two can never drift.
  `tests/integration_tests/model_tests.py` is **not** a source: all 11 of its skips are
  `skipUnless(is_module_installed(...))` guards.
- **LANE 3 — EOL-passed `@deprecated` removals**; scan scope `superset/**/*.py` *(n-01)*, EOL
  and caller/override gating per §4.2, verified via targeted
  `tests/unit_tests/db_engine_specs/`. Enumeration is AST-based (the decorator may sit several
  decorators above the `def`), producing `module:qualname` locators. Qualifying demo candidate:
  `normalize_indexes`.
- **Tier-2 (documented, lower priority)** — third-party modernization warnings suppressed in
  `superset/mcp_service/server.py` and `superset/db_engine_specs/redshift.py` — high blast
  radius / low verifiability.

---

## §6 Dispatch — action tiers (+ per-run budget)

- High score AND risk ≤ 2 AND all auto-merge preconditions met → open a PR, auto-merge eligible.
- High score AND risk ≥ 3 → open a PR labeled `needs-human-review`, no auto-merge.
- Medium → open an ISSUE with a proposed fix, no PR.
- Low → log only / drop.
- Per-run budget `budget_N = 10`: open at most 10 PRs/issues per run; overflow (even
  high-scoring) is deferred to later runs and recorded as deferred.
- Auto-merge is gated on the FULL CI gate stack being green (§10) — the score decides whether
  to ATTEMPT, CI decides whether to LAND.

---

## §7 Dual artifacts (every remediation: PR + companion issue, cross-linked)

- Every dispatched fix produces a technical PR (engineer / AI-reviewer audience) AND a
  companion high-level issue (EM/PM audience), cross-linked (`Closes #<issue>` in the PR so
  merging auto-closes the manager-facing issue).
- **Ordering is mandated, not optional** *(m-09)*: (1) create the issue, (2) create the PR with
  `Closes #<n>`, (3) patch the issue body with the PR link. The state after (1) but before (2)
  is the canonical resume case covered by the idempotency spec in §14.1.
- Medium tier (no PR): the issue is the standalone artifact.
- **Degraded path** *(B-02)*: if `has_issues == false` on the target, dispatch aborts before any
  write unless `issue_sink = pr_comment`, in which case the manager-facing artifact is rendered
  as a dedicated PR comment plus `reports/issues/<candidate_id>.md`, the run is tagged
  `artifact_degraded`, and those candidates do **not** count toward §19 evidence. This path
  necessarily inverts the ordering above (there is no issue number to reference), so it has its
  own state transition `dispatching → pr_created → comment_created` *(R2-m-06)* and the §14
  template-validation rule applies to the rendered **comment body** exactly as it does to an
  issue body.
- PR body carries a scaled `### IMPLEMENTATION PLAN` section (candidate-level, planner-authored),
  distinct from the manager issue and run report.

---

## §8 Issue & PR templates (Superset conventions, locked & versioned)

- Use SUPERSET's OWN conventions for BOTH issues and PRs (no invented high-level schema); keep
  the EM/PM register by writing non-technical prose WITHIN the conventional shapes.
- **PR** *(M-03)* — conform to `.github/PULL_REQUEST_TEMPLATE.md`, whose real heading set is
  exactly four sections, in this order:
  `### SUMMARY`, `### BEFORE/AFTER SCREENSHOTS OR ANIMATED GIF`, `### TESTING INSTRUCTIONS`,
  `### ADDITIONAL INFORMATION` (followed by its verbatim `- [ ]` checkbox block — there is **no**
  `CHECKLIST` heading). BEFORE/AFTER is kept and marked `n/a` for backend-only fixes.
  **Insertion points:** `### IMPLEMENTATION PLAN` and `### TESTS` immediately after
  `### SUMMARY`; config-gated `### AUTOMATION METADATA` appended last.
  The template is vendored at `templates/superset/PULL_REQUEST_TEMPLATE.md` with a drift test
  diffing it against the target repo's live file.
- **PR title** *(m-01)* — must match the regex enforced by the `lint-check` job via the local
  composite action `./.github/actions/pr-lint-action`, pinned verbatim in
  `templates/superset/pr_title_regex.txt` (drift-tested against `.github/workflows/pr-lint.yml`):

  ```
  ^(build|chore|ci|docs|feat|fix|perf|refactor|style|test|other)(\(.+\))?(\!)?:\s.+
  ```

  Note it permits `other` and does **not** permit `revert`, and requires whitespace after the
  colon.
- **Issue templates mapped PER LANE:**
  - flaky-test / defect lanes → `bug-report.yml` shape.
  - public-API deprecation removals → `sip.md` shape. Its YAML front-matter is **stripped**
    before rendering, `assignees` (`apache/superset-committers`) is never propagated, and the
    title becomes `[SIP] <generated>`. SIP-shaped issues on a fork are demonstrative only — the
    real SIP process needs committer numbering and a list vote. *(m-03)*
  - SECURITY lane (CodeQL) → CANNOT use `bug-report.yml` (it explicitly bans GitHub issues for
    security problems), and the target repo ships **no** generic/tracking form *(M-05)*. REPO A
    therefore ships `templates/issues/security_tracking.md` with a locked, detail-free section
    list: `### SUMMARY (no exploit detail)` / `### SCOPE (files or modules only)` /
    `### REMEDIATION STATUS` / `### VERIFICATION` / `### REFERENCES (rule ID only)`. This is
    the `SECURITY_ISSUE_MODE = generic_tracking` constant (§13, not a knob).
- Enforce section presence + order via snapshot/format tests so formats stay consistent across
  all generated issues and PRs.

---

## §9 Runtime flow — per-candidate remediation (three roles, NO plan-review step)

Three independent, distinct sessions per candidate:

- **PLANNER** — authors the per-PR `### IMPLEMENTATION PLAN` with explicit, testable acceptance
  criteria that act as the shared test oracle.
- **IMPLEMENTER** — writes ONLY the code fix to the planner's spec. Barred from creating/editing
  tests (any such edit → `needs-human-review`).
- **REVIEWER** — concurrently (from the planner spec, NOT the implementer's diff) authors the
  red→green regression test, then runs the code-review loop.

**Sequence**

1. Planner writes spec.
2. Implementer (code) and Reviewer (tests) work CONCURRENTLY from the spec.
3. **JOIN: mandatory RED-BASELINE gate** — the reviewer test MUST fail against pre-fix code for
   the expected reason (a test that passes pre-fix, or fails for an unrelated reason, is invalid
   → re-author/escalate). Then red→green against the implementer's fix. A still-red join is a
   real bug signal that feeds the review loop, not something to hide.
4. Code-review loop (severity taxonomy: `blocking` / `major` / `minor` / `nit`) until no
   blocking/major remain, bounded by `iteration_cap = 5`.
5. **Terminal** — converged + all gates green → dispatch/auto-merge eligible; cap hit or
   unresolved red→green disagreement → `disagreement_unresolved` → `needs-human-review` (post a
   disagreement summary: failing test, mapped criterion, pre-fix signature, fix rationale). NO
   third adjudicating agent — straight to human.

### §9.1 The red baseline is a falsifiable comparison *(M-08)*

Per acceptance criterion the planner emits
`expected_failure = {nodeid, exception_type, message_pattern (regex), assert_location (optional)}`.
The baseline is **valid iff** running `nodeid` at the pre-fix commit exits `FAILED` (not
`SKIPPED`, not a collection `ERROR`) **and** the captured exception type equals
`exception_type` **and** `message_pattern` matches the failure text. Any other outcome is
`invalid_red_baseline` → re-author once, then escalate. All four expected fields and their
observed counterparts are logged so the §11 expected-reason-match KPI is computable.

### §9.2 Branch, concurrency and commit mechanics *(M-09)*

- The **orchestrator** creates `devin/remediation/<candidate_id>` from the target base and pins
  `base_sha`; it — not the sessions — opens the PR, and only after JOIN.
- The **reviewer** runs its red baseline in its own checkout at `base_sha` and pushes
  test-path-only commits first. **For LANE 2 the reviewer also owns the skip-marker change**
  *(R2-M-07)*: it applies the removal or narrowing as a scratch working-tree patch on
  `base_sha`, classifies the result (`FAILED` → valid baseline; `PASSED` → `stale_skip`;
  `SKIPPED` → `invalid_red_baseline`), and only then commits that test-path change. This is the
  only way the classification is observable — at `base_sha` the marker is by definition still
  present, so no other role can run the un-skipped test.
- The **implementer** commits non-test paths only and rebases onto the reviewer's commit at
  JOIN. It has **no** LANE 2 carve-out.
- Every commit uses `git commit --signoff`. Push races resolve by rebase-retry (max 3), then
  `needs-human-review`.

### §9.3 Test-inclusion policy and the implementer's permitted diff *(B-04)*

The implementer restriction is **scope-based, not path-based**:

> The implementer may not author or modify assertions or test oracles, **nor skip markers**
> *(R2-M-07)*. Its permitted diff is non-test production code only. Any hunk touching an
> assertion, a fixture body, test logic, or a `@pytest.mark.skip` / `skipif` decorator →
> `needs-human-review`.

This is enforced by a **diff classifier** in T10, not a filename check; skip-marker hunks are
rejected for the implementer and accepted for the reviewer, keeping the marker single-owner. A
red→green test is REQUIRED for behavioral lanes (CodeQL, deprecation). LANE 2 is **not**
"self-satisfied": per §9.2 the reviewer runs the un-skipped test at `base_sha` and it MUST fail
(not skip, not pass) — that failure is its baseline. If it **passes** there the candidate is
tagged `stale_skip`, a distinct valid terminal outcome exempt from red→green (the remediation
is simply deleting a dead skip marker) that ships as a reviewer-only diff.

**Structural invariants (NOT configurable)** — reviewer≠implementer separation, red-baseline
requirement (per §9.1/§9.3), no-auto-merge-without-green-CI, no third adjudicating agent,
criterion-mapped tests, **and "an unresolved `major` never auto-merges"** *(M-06)*. This runtime
flow has NO plan-review step.

**Additional safeguards against reviewer-as-sole-test-author risk** — each reviewer test maps to
a planner acceptance criterion (unmapped → escalate); a fix+test that breaks an existing suite
test blocks auto-merge regardless of the reviewer's own test being green.

---

## §10 CI gate stack (Superset sign-off gates the generated PRs must pass)

Auto-merge eligibility requires the full stack green. The gates split into two sets, and the
plan previously conflated them *(M-04)*.

**Runs as a pre-commit hook** (verifiable locally via `pre-commit run --files <changed>`):

| Hook | Note |
|---|---|
| `ruff-format` | formatting |
| `ruff` | lint |
| `mypy` | typing (main) |
| `pylint` (Superset plugins) | lints only `superset/` files changed since merge-base `origin/$TARGET_BRANCH` — needs `origin/master` fetched; **never** lints `tests/` |
| `db-engine-spec-metadata` | fires for concrete engine-spec modules only — it excludes `base.py`/`lib.py`, so the current LANE 3 candidate is out of its scope *(R2-m-02)* |
| frontend hooks | only when `superset-frontend/**` is touched |
| `zizmor` | runs `--no-exit-codes` → advisory only |

**Runs only in CI** (cannot be reproduced by pre-commit). The **13 contexts required by
upstream `.asf.yaml`** *(R2-m-01)* are exactly:

`lint-check` (the PR-title action), `pre-commit (current)`, `unit-tests-required`,
`test-postgres-required`, `test-sqlite`, `test-mysql`, `test-postgres-hive`,
`test-postgres-presto`, `frontend-build`, `cypress-matrix-required`,
`playwright-tests-required`, `dependency-review`, `enforce-single-migration-head`.

Other CI-only workflows the pipeline also runs, but which are **not** `.asf.yaml`-required:
`license_check` (an Apache-RAT / `setup-java` workflow invoking `./scripts/check_license.sh` —
not a pre-commit hook either).

That required set is applied by ASF infra to `apache/superset` only — a fork inherits the
workflows but not the branch protection.

**DCO** — there is no DCO workflow in the repository; DCO is ASF-infra-enforced upstream. The
pipeline still signs every commit (`git commit --signoff`) and asserts the trailer itself.

**CODEOWNERS** *(m-05)* — consulted only to annotate the PR body with paths that would require
owner review upstream. No review requests are sent on the fork (the listed ASF committers are
not collaborators, and `require_code_owner_reviews` is an upstream `.asf.yaml` setting). Note
`CODEOWNERS` has no entry covering `superset/db_engine_specs/` or `tests/` anyway.

### §10.1 `ci_evidence_mode` *(B-01)*

| Mode | Gate satisfied by | Auto-merge |
|---|---|---|
| `github` | the CI contexts above reporting success on the PR head | permitted, subject to §14 |
| `local` | `pre-commit run --files <changed>` plus the targeted pytest paths executed in-session, with command output attached to the PR body | **hard-disabled** (`auto_merge_enabled` forced `false`, not merely defaulted) |

The mode is resolved by the §3 0d precondition check and recorded in the Layer 1 event log;
§19's REPO B criterion names which mode satisfied it. On `victorciao/superset` the strengthened
0d check resolves **`local`** (1 lifetime Actions run, event `dynamic`; no `pull_request` run
has ever completed).

**`ci_wait_timeout_s`** *(R2-M-06)* — `github` mode waits at most `ci_wait_timeout_s`
(default `5400`) for the required contexts to report on the PR head. On expiry the candidate is
recorded `ci_evidence_unavailable`, its evidence is downgraded to `local`, and it is **never**
auto-merge eligible. Without this bound a fork whose workflows never trigger blocks the
orchestrator indefinitely.

---

## §11 Observability

Answers "how would an engineering leader know this is working?"

- **Layer 1 — structured JSONL event log (source of truth)** — per candidate: `run_id`, `lane`,
  `candidate_id`, gate result (+ which gate failed), score + factor breakdown, tier, action,
  Devin `session_id`s by role (planner/implementer/reviewer), iterations, PR/issue URLs,
  `test_added` / `test_paths` / `test_author` / `test_exempt_reason`, terminal outcome.
- **Layer 2 — per-run summary report** — candidates seen, gated out (with reason), scored,
  dispatched by tier, deferred (budget overflow), resulting PR + issue links.
- **Layer 3 — rolling KPI rollup** persisted to `reports/kpis.md` (default local sink; Google
  Sheet optional via `kpi_sink`; precedent *(m-04)*: a Google Sheet sink already exists in this
  repo's tooling — `.github/workflows/tech-debt.yml` runs a secret-gated (`GSHEET_KEY`),
  `continue-on-error` frontend lint-stats upload on pushes to `master`/release branches. That is
  a precedent for the *sink*, not for KPI content or cadence). KPIs: task-state counts (active/completed), PR merge rate (merged-clean vs edited
  vs rejected), verification pass rate, backlog burn-down (vs Phase 0c baseline), test-inclusion
  rate, criterion-coverage rate, expected-reason match rate on red baselines,
  `disagreement_unresolved` rate, sessions-per-candidate by role, implementer-test-edit
  violation rate (~0 expected).
- **Burn-down validity** *(R2-M-03)* — burn-down is computed only for lanes listed in
  `baseline.baseline_valid_lanes`; a lane whose baseline was captured while it was
  `capability_unavailable` reports `n/a` with that reason, never a spurious increase from zero.
- **ALERTING** (visually distinct in the rollup): merge rate < `merge_rate_floor = 0.50` → flag;
  session-failure rate > `session_failure_ceiling = 0.30` → flag.
- Merge-rate / burn-down are lagging and only meaningful after several runs.

---

## §12 Session management

For each dispatched candidate, call the Devin API (per Phase 0 findings) to create the role
sessions with scoped prompts (file paths, alert/test/deprecation context, fix + verify
instructions, emit BOTH artifacts using the templates). Poll status; record state transitions +
timing into the Layer 1 event log; collect PR + issue URLs.

### §12.1 Role prompt contracts — `structured_output_schema` per role *(M-10)*

Phase 0a confirmed `structured_output_schema` is accepted by `POST /v1/sessions`. Each role gets
a schema so criterion mapping and the §11 criterion-coverage KPI are machine-checkable:

```
PLANNER     -> { criteria: [ { id: "AC-1", statement, expected_failure {…§9.1}, verify_command } ],
                 files_in_scope[], out_of_scope[] }
IMPLEMENTER -> { files_changed[], criteria_addressed[], commands_run[] }
REVIEWER    -> { tests: [ { path, nodeid, criterion_id } ],
                 red_baseline { … }, green_result { … },
                 findings: [ { severity, criterion_id, note } ] }
```

A reviewer test whose `criterion_id` is absent from the planner output is **rejected** (the §9
unmapped-test escalation).

### §12.2 Polling and cost contract

`GET /v1/sessions/{id}` until a terminal `status_enum`; `session_timeout_s` per role; per-role
`max_acu_limit`; every creation passes `idempotent: true` and
`tags: ["devin-remediation", candidate_id, role, "attempt:<n>"]`. Exceeding the per-run
session/cost ceiling (§14) aborts the run rather than degrading silently.

**Idempotent creation must not swallow retries** *(R2-M-05)*. `idempotent: true` returns the
pre-existing session for an identical request, which would defeat the two retry paths the plan
requires — the §14.1 retry of a stuck/timed-out session under the same `candidate_id`, and the
§9.1 re-author after `invalid_red_baseline`. Every creation therefore carries an **attempt
ordinal** in both the prompt preamble and the `attempt:<n>` tag, making each retry a distinct
request. The orchestrator asserts `is_new_session == true` for any attempt `> 1`; a dedupe hit
there is a fatal orchestration error, not a silent no-op. `is_new_session` is recorded in the
Layer 1 event log.

---

## §13 Config knobs (tunable; locked values = shipped DEFAULTS, not hardcoded)

All live on one config surface (env vars / config file), changeable without editing logic. The
README ships a config reference table with default, allowed values, and safety classification.

| Knob | Default | Allowed values | Safety |
|---|---|---|---|
| `mode` | `simulate` | `simulate`, `live` | **safety-relevant** — `live` must be supplied explicitly via CLI/env; unset, empty, or unrecognized values resolve to `simulate` and are logged *(M-13)* |
| `iteration_cap` | `5` | `1..10` | — |
| `coverage_bar` | `0.80` | `0.0..1.0` | — |
| `budget_N` | `10` | `1..BUDGET_HARD_MAX` | **safety-relevant** — clamped at `BUDGET_HARD_MAX` *(M-12)* |
| `score_cap` | `200` | `> 0` | — |
| `tier_high_min` | `60` | `> tier_medium_min` | — |
| `tier_medium_min` | `20` | `> 0` | — |
| `eol_major_lag` | `2` | `>= 1` | — |
| `merge_rate_floor` | `0.50` | `0.0..1.0` | — |
| `session_failure_ceiling` | `0.30` | `0.0..1.0` | — |
| `kpi_sink` | `local` | `local`, `gsheet` | `gsheet` while `mode = simulate` is a **config validation error at startup** (non-zero exit, no partial run) *(R2-m-07)* |
| `major_only_requires_human` | `true` | `true`, `false` | **routing-only** — see below |
| `alert_source` | `api` | `api`, `sarif_file` | — |
| `ci_evidence_mode` | resolved by §3 0d | `github`, `local` | `local` forces `auto_merge_enabled = false` |
| `ci_wait_timeout_s` | `5400` | `> 0` | on expiry → `ci_evidence_unavailable`, evidence downgraded to `local`, never auto-merge *(R2-M-06)* |
| `auto_merge_enabled` | `false` | `true`, `false` | **safety-relevant** *(R2-m-05)* — forced `false` whenever `ci_evidence_mode = local`, and never sufficient on its own (§6 tier, §9 invariants and §10 gates all still apply) |
| `issue_sink` | `issues` | `issues`, `pr_comment` | `pr_comment` tags the run `artifact_degraded` |
| `version_source` | `.github/ISSUE_TEMPLATE/bug-report.yml` | repo-relative path | drift-tested; yielding no concrete release is a startup error, not an empty lane *(R2-M-02)* |

Also configurable: target `owner/repo`, GitHub token, Devin API key, per-lane factor rubrics
(`config/rubrics.yaml`), artifact templates.

**Non-knobs (module constants, not config surface):**

- `BUDGET_HARD_MAX = 25` *(M-12)* — values above it are clamped, the clamp is logged as
  `guardrail_clamped`, and running above it requires an explicit `--i-know-what-im-doing` flag.
- `major_only_requires_human` is **routing-only** *(M-06)*: `false` still blocks auto-merge and
  merely omits the `needs-human-review` label. The block itself is a §9 structural invariant.
- `SECURITY_ISSUE_MODE = generic_tracking` *(R2-n-01)* — it had exactly one allowed value, so it
  is a constant, not a knob. Security-lane issues are always detail-free (§8, §14).
- Structural invariants (§9) are NOT knobs.

**`coverage_bar` subject** *(m-07)* — the "pure-logic modules" are exactly
`src/pipeline/gate.py`, `score.py`, `dispatch.py`, `dedupe.py`, `templates/render.py`,
`observability/kpis.py`, encoded in `pyproject.toml`'s coverage config so CI enforces the bar.

---

## §14 Guardrails / constraints

- SIMULATE mode makes NO writes (no PRs/issues/merges); only `live` writes.
- No secrets in the Docker image; credentials injected at runtime.
- Idempotency/dedupe: re-runs must not open duplicate PRs/issues for the same candidate — see
  §14.1.
- Budget cap (`budget_N`) enforced; repo allowlist; GitHub API rate-limit backoff + an explicit
  per-run session/cost ceiling so a runaway run can't burn the API budget.
- No auto-merge without the full CI gate stack green.
- A behavioral-lane PR is not auto-merge eligible unless it includes a new/updated test (or a
  recorded lane-permitted exemption).
- A converged PR with no `blocking` but any unresolved `major` → NOT auto-merge eligible →
  `needs-human-review` (`major_only_requires_human = true`).
- Reviewer ≠ implementer (runtime and build-time); collision = configuration error. No
  third/adjudicating agent for `disagreement_unresolved` — always human-escalated.
- No security-lane candidate may open a public issue containing vulnerability detail.
- No issue/PR opened unless its body validates against the selected template — one of the target
  repo's forms (`bug-report.yml`, `sip.md`) or REPO A's `security_tracking.md` *(M-05)* —
  (section presence + order) and, for PRs, the title matches the Conventional-Commits regex.
- **Label preflight** *(m-10)*: `needs-human-review` does not exist on the target. Dispatch
  ensures it once (`GET` then `POST /repos/{o}/{r}/labels`); if creation is denied it falls back
  to a PR comment plus a `reports/` record rather than failing the API call.

### §14.1 Idempotency, state store, and resume *(M-07)*

```
candidate_id = sha256(lane | repo | stable_locator)
```

| Lane | `stable_locator` |
|---|---|
| 1 — CodeQL | `rule_id + file_path + normalized_symbol + position_digest` — **never** `alert.number` (unstable across re-scans) |
| 2 — skipped test | the pytest nodeid |
| 3 — deprecation | `module:qualname` |

**LANE 1 needs a positional discriminator** *(R2-M-01)*. Rule + path + symbol collides on real
data: four of the eleven live alerts are `py/overly-large-range` on the same line of
`superset/mcp_service/dashboard/tool/add_chart_to_existing_dashboard.py:55`, differing only by
column (6, 9, 12, 15). Under last-write-wins that silently collapses four candidates into one
and understates the burn-down denominator. So:

```
position_digest = sha256("{start_line}:{start_column}-{end_line}:{end_column}")[:12]
```

taken from `most_recent_instance.location`. It is stable across re-scans of unchanged code
(unlike `alert.number`) while still separating co-located alerts. A §17 test asserts the four
fixture alerts yield four distinct `candidate_id`s.

`state/candidates.jsonl` (append-only, last-write-wins by `candidate_id`) is the **dedupe and
resume source of truth** — distinct from the Layer 1 observability log. States:

```
enumerated → gated → scored → dispatching → issue_created → pr_created → converged → terminal
                                          └→ pr_created → comment_created → …   (issue_sink = pr_comment)
```

Before any write the pipeline re-reads state **and** searches the target repo for the marker
`<!-- devin-remediation-id: <candidate_id> -->`, which is embedded in every generated body.
This covers the §18 failure modes: a mid-run crash resumes from the last recorded state; a
stuck/timed-out session is retried under the same `candidate_id`; "issue created but PR failed"
replays without creating a second issue.

---

## §15 REPO A layout

Concrete tree, so T0 and the coverage configuration agree *(n-02)*:

```
docs/IMPLEMENTATION_PLAN.md      # this document — source of truth, referenced from README
docs/PHASE0_DISCOVERY.md
src/pipeline/
  lanes/            codeql.py, skipped_tests.py, deprecations.py
  gate.py  score.py  dispatch.py  dedupe.py  session_client.py  config.py  schemas.py
  github_client.py
  templates/render.py
  observability/    events.py, report.py, kpis.py
templates/
  superset/PULL_REQUEST_TEMPLATE.md, pr_title_regex.txt
  issues/bug_report.md, sip.md, security_tracking.md
config/rubrics.yaml
fixtures/         baseline.json, codeql_alerts.json
state/            candidates.jsonl        (runtime dedupe/resume store)
reports/          run-<run_id>.md, kpis.md, issues/
tests/            unit/, integration/
Dockerfile  docker-compose.yml  README.md  RESULTS.md
```

---

## §16 Task breakdown (sequenced)

| ID | Task |
|---|---|
| T-1 | Plan review (build-time reviewer reviews committed `docs/IMPLEMENTATION_PLAN.md`; blocking/major resolved + committed back before build; bounded by `iteration_cap = 5`). |
| T0 | Scaffold REPO A (commit `docs/IMPLEMENTATION_PLAN.md`, Dockerfile, compose, config surface, CI). |
| T1 | Phase 0 discovery (Devin API shape, GitHub auth/scopes, live enumeration + baseline + SIMULATE fixture). |
| T2 | Data schemas (`Candidate`, event record) / interface contracts. |
| T3 | Gate module. |
| T4 | Score module. |
| T5 | Tier/dispatch + budget. |
| T6 | Lanes (CodeQL, skipped-test, deprecation). |
| T7 | Session client (planner/implementer/reviewer orchestration; concurrent implement+test; red-baseline + join; review loop). |
| T8 | Templates (PR + per-lane issues) + format/snapshot tests. |
| T9 | Observability (3 layers + alerting). |
| T10 | Guardrails (dedupe, simulate-no-write, budget, backoff, cost ceiling). |
| T11 | Docker packaging + SIMULATE `docker compose up`. |
| T12 | README (setup, simulate vs live, schedule cadence, config reference table, observability "how a leader knows", links) + `RESULTS.md`. |
| T13 | Live run against REPO B to produce the evidence remediations. |

Build via the §2 concurrent implementer-code / reviewer-tests split; push only after the
code-review loop converges with green CI.

---

## §17 Test plan (REPO A's own tests — authored by the BUILD-TIME reviewer, independent of implementer)

**Unit (credential-free; GitHub/Devin clients mocked)**

- gate predicates, scoring math (incl. risk floor/cap), tier mapping, dedupe/idempotency, rubric
  normalization, template rendering.
- \>10 gate-passing high-score candidates → exactly 10 dispatched, rest recorded deferred.
- a converged PR with an unresolved `major` (no `blocking`) → NOT auto-merge eligible →
  `needs-human-review`.
- reviewer test with no mapped planner criterion → rejected/escalated.
- a pre-fix failure whose signature doesn't match the expected reason → invalid red baseline.
- a still-red join not converging within the cap → `disagreement_unresolved` →
  `needs-human-review`, never auto-merge.
- a fix+test that breaks an existing (mocked) suite test → blocks auto-merge even if reviewer's
  own test is green.
- a non-converging loop escalates to `needs-human-review` at exactly `iteration_cap`,
  parameterized over `cap ∈ {3, 5}`, plus a separate assertion that the shipped default is `5`
  *(m-06)*.
- KPI rollup raises merge-rate alert when < 0.50 and session-failure alert when > 0.30.
- security-lane issue never exposes vulnerability detail; issues/PRs validate against locked
  templates.
- pure-logic module coverage ≥ `coverage_bar = 0.80`.
- a non-default knob (e.g. `budget_N` changed) takes effect in SIMULATE without code edits.
- `budget_N` above `BUDGET_HARD_MAX` is clamped and logged as `guardrail_clamped` *(M-12)*.
- with `major_only_requires_human = false`, a PR with an unresolved `major` is **still** not
  auto-merge eligible *(M-06)*.
- `mode` unset / empty / unrecognized resolves to `simulate` *(M-13)*.
- a `skipUnless` fixture yields **zero** LANE 2 candidates *(M-01)*.
- a deprecation with an internal caller or an override-surface reference fails `automatability`
  *(M-02)*; `normalize_indexes` passes and `get_url_for_impersonation` does not.
- an implementer diff touching an assertion is rejected while a skip-marker-only diff is
  accepted; a LANE 2 test passing pre-fix yields `stale_skip` *(B-04)*.
- tier mapping at the threshold boundaries (`59/60`, `19/20`) and the `score_cap` clamp *(B-05)*.

**Added to close T-1 review round 2**

- `test_codeql_locator_separates_colocated_alerts` — the four `py/overly-large-range` fixture
  alerts on `add_chart_to_existing_dashboard.py:55` yield four distinct `candidate_id`s
  *(R2-M-01)*.
- `test_current_major_from_version_source` — resolves `6.1.0 → 6` from the bug-report form, and
  a form with no concrete release raises a startup config error rather than silently emptying
  LANE 3 *(R2-M-02)*.
- `test_burndown_reports_na_for_invalid_baseline_lane` *(R2-M-03)*.
- `test_enumerator_count_matches_baseline_totals` and `test_xfail_and_skipif_yield_no_candidates`
  *(R2-M-04)*.
- `test_retry_asserts_new_session` — attempt `> 1` returning `is_new_session == false` is a fatal
  orchestration error *(R2-M-05)*.
- `test_ci_wait_timeout_downgrades_evidence` — expiry → `ci_evidence_unavailable`, evidence
  `local`, not auto-merge eligible *(R2-M-06)*.
- `test_implementer_skip_marker_hunk_rejected` and
  `test_reviewer_owns_lane2_baseline_classification` (failed / passed / skipped → valid /
  `stale_skip` / `invalid_red_baseline`) *(R2-M-07)*.
- `test_frontend_alerts_gated_out` — the three JS/TS fixture alerts fail `verifiability_exists`
  with reason `out_of_scope_frontend` *(R2-m-04)*.
- `test_gsheet_sink_rejected_in_simulate` *(R2-m-07)*.
- `test_pr_comment_sink_state_transition_and_validation` *(R2-m-06)*.

**Added to close §19 gaps** *(M-11)*

- `test_crosslink_roundtrip` — issue number injected, `Closes #n` present, both artifacts carry
  the `candidate_id` marker.
- `test_resume_after_issue_created_pr_failed` — replay creates no second issue.
- `test_rate_limit_backoff` (429 + reset header → bounded sleep, one retry) and
  `test_session_ceiling_aborts_run`.
- `test_burndown_vs_baseline` against `fixtures/baseline.json`.
- `test_role_collision_raises_config_error` (build-time and runtime).
- `test_pr_title_matches_pr_lint_regex`, parameterized across lanes, using the regex loaded from
  `templates/superset/pr_title_regex.txt`.
- `test_generated_pr_contribution_compliance` (credential-free) — RAT header presence,
  ruff/mypy clean on generated files, `Signed-off-by` trailer present.
- `test_dispatch_preflight_aborts_when_issues_disabled` *(B-02)* and
  `test_local_ci_mode_forces_auto_merge_off` *(B-01)*.
- `test_invalid_red_baseline_signature_mismatch` against the §9.1 four-field contract *(M-08)*.

**Integration / smoke**

- full SIMULATE flow (fixture → gate → score → dispatch → artifacts → reports) makes no writes.
- Docker smoke: `docker compose up` runs SIMULATE end-to-end.
- optional credentials-gated live smoke (mocked by default).

---

## §18 Assumptions, dependencies, non-goals

- **Assumptions / dependencies** — Devin API availability + key; GitHub token with the §3 scopes.
  *(B-03)* The fork's capability state is **not assumed**: it is checked by §3 0d and was
  recorded in §3 0e (Issues, Actions, and CodeQL were all off and had to be enabled during
  Phase 0; CodeQL now yields 11 live alerts). Python toolchain (ruff/pylint/mypy/pytest)
  matching Superset's pinned versions.
- **Non-goals** (prevent scope creep) — no frontend lint-debt lane; no auto-merge of high-risk;
  no real-time dashboard (batch KPI rollup only); no adjudicating third agent; runtime flow has
  no plan-review step.
- **Failure & recovery** — specified in §14.1: the append-only `state/candidates.jsonl` store
  plus the embedded `candidate_id` marker cover mid-run crash, stuck/timed-out session, and
  partially-created artifacts (issue created but PR failed).

---

## §19 Success criteria / done when

- **BUILD-TIME** — plan-review gate ran before build; REPO A built via concurrent
  implementer-code / reviewer-tests split, pushed only after the loop converged with green CI;
  runtime vs build-time SDLC documented as separate in README.
- **RUNTIME (SIMULATE-demonstrable)** — three distinct role session IDs, NO plan-review step;
  reviewer test concurrent + red baseline recorded + red→green join; loop converges or cap →
  `needs-human-review` after 5 iterations; `major`-only → human; >10 → 10 dispatched + deferred;
  alerts on seeded bad metrics; KPI rollup at `reports/kpis.md`.
- **CONTRIBUTION COMPLIANCE** — qualified by `ci_evidence_mode` *(R2-m-08)*: under `github`, the
  reported contexts (`lint-check`, `license_check`, `pre-commit (current)`) are the evidence;
  under `local`, the credential-free proxies are — `test_generated_pr_contribution_compliance`
  (RAT header, ruff/mypy clean, `Signed-off-by` trailer) and
  `test_pr_title_matches_pr_lint_regex`, plus attached `pre-commit run --files` output. The
  criterion may not be claimed against contexts that never reported.
- **CONFIG** — every knob settable via the config surface without code edits; README config
  table lists default, allowed values, safety classification, and the non-configurable
  invariants; `docs/IMPLEMENTATION_PLAN.md` is under version control and referenced from the
  README.
- **REPO B (Superset fork)** — ≥1 real, verified remediation per first lane (a CodeQL-alert fix
  and a re-enabled skipped/flaky test) as cross-linked PR + convention-compliant companion issue
  with the CI gate stack green **under the recorded `ci_evidence_mode`** (§10.1), which
  `RESULTS.md` must name; `RESULTS.md` lists selected issues, gate/score rationale, and links.

---

## §20 Revision log

### Revision 2 — T-1 plan review round 1

Reviewer verdict `changes_required`: 5 blocking, 15 major, 10 minor, 2 nit. Disposition:

| Finding | Disposition |
|---|---|
| B-01 CI stack unsatisfiable | **Accepted + environment fixed.** Actions enabled on the fork (49 workflows registered); `ci_evidence_mode` added (§10.1) with `local` forcing auto-merge off. Phase 0 0d makes it a blocking precondition. |
| B-02 Issues disabled | **Accepted + environment fixed.** `has_issues` set true; 0d preflight + `issue_sink = pr_comment` degraded path (§7). |
| B-03 LANE 1 has no data | **Accepted + environment fixed.** CodeQL default setup configured; 11 live alerts captured to `fixtures/codeql_alerts.json`. `alert_source = api \| sarif_file` added (§5); §18 assumption replaced by recorded fact. |
| B-04 LANE 2 fix *is* a test edit | **Accepted.** Implementer restriction restated as scope-based with a diff classifier; `stale_skip` outcome added (§9.3). |
| B-05 no tier thresholds | **Accepted.** `score_cap = 200`, `tier_high_min = 60`, `tier_medium_min = 20`, worked example (§4). |
| M-01 `model_tests.py` | **Accepted.** Dropped; enumerator scope pinned (§5). |
| M-02 EOL underivable | **Accepted.** `eol_major_lag` rule + caller/override gate; demo candidate narrowed to `normalize_indexes` (§4.2). |
| M-03 PR sections wrong | **Accepted.** Real four-heading set + insertion points + drift test (§8). |
| M-04 CI stack misstated | **Accepted.** Split into pre-commit vs CI-only tables; DCO and license-check corrected (§10). |
| M-05 no security template | **Accepted.** `templates/issues/security_tracking.md` with locked detail-free sections (§8). |
| M-06 `major` knob defeats guardrail | **Accepted.** Restated as routing-only; the block is a §9 invariant. |
| M-07 idempotency unspecified | **Accepted.** `candidate_id` derivation, `state/candidates.jsonl`, state machine, body marker (§14.1). |
| M-08 red baseline unfalsifiable | **Accepted.** Four-field `expected_failure` contract (§9.1). |
| M-09 concurrency mechanics | **Accepted.** Branch ownership, push order, rebase-retry, signoff (§9.2). |
| M-10 role schemas | **Accepted.** Per-role `structured_output_schema` (§12.1). |
| M-11 untested §19 criteria | **Accepted.** Eight tests added (§17). |
| M-12 `budget_N` unbounded | **Accepted.** `BUDGET_HARD_MAX = 25` module constant + clamp test. |
| M-13 `mode` has no default | **Accepted.** Default `simulate`; unrecognized resolves to `simulate`. |
| M-14 "open questions EMPTY" false | **Accepted.** Resolved inline (rubrics §4.1, thresholds §4, failure/recovery §14.1, coverage subject §13); open questions below now lists only what genuinely remains. |
| M-15 gate/score double-count | **Accepted.** Gate = existence (`>= 2`), factor = quality (`2..5`) (§4.1). |
| m-01 … m-10, n-01, n-02 | **All accepted**, inline at the cited sections. |

No finding was rejected.

### Revision 3 — T-1 plan review round 2

Reviewer verdict `changes_required`: 0 blocking, 7 major, 8 minor, 2 nit (round-1 blockers all
confirmed closed, 26 of 32 findings resolved outright, none regressed). Disposition:

| Finding | Disposition |
|---|---|
| R2-M-01 LANE 1 locator collides | **Accepted.** `position_digest` added to the locator (§14.1) + separation test. The collision was real: 4 co-located `py/overly-large-range` alerts. |
| R2-M-02 EOL rule has no version source | **Accepted.** `version_source` knob reading the bug-report form's release dropdown → `6.1.0 / major 6 / threshold 4` (§4.2); qualifying-candidate table added; drift is a startup error. |
| R2-M-03 baseline predates CodeQL enablement | **Accepted.** Baseline regenerated post-enablement with the 11 alerts, `baseline_valid_lanes`, and an `n/a` burn-down rule (§3 0e, §11). |
| R2-M-04 "37 skips" ≠ enumerator output | **Accepted.** `xfail` and all conditional skips dropped from LANE 2 scope; count is now **34** included / 33 excluded-and-recorded, with an equality test (§3 0e, §5). |
| R2-M-05 `idempotent: true` blocks retries | **Accepted.** Attempt ordinal in prompt + tags; `is_new_session == true` asserted for attempts > 1 and logged (§12.2). |
| R2-M-06 Actions precondition too weak, no CI timeout | **Accepted.** 0d now requires a completed `pull_request`/`workflow_dispatch` run (the fork has none → `local`); `ci_wait_timeout_s = 5400` with an explicit downgrade path (§3 0d, §10.1). |
| R2-M-07 LANE 2 skip marker has two owners | **Accepted.** Skip-marker ownership moved wholly to the reviewer, which classifies at `base_sha`; implementer carve-out deleted and the classifier now rejects implementer skip hunks (§9.2, §9.3). |
| R2-m-01 … R2-m-08, R2-n-01, R2-n-02 | **All accepted**, inline at the cited sections (13-context list corrected, `db-engine-spec-metadata` scope, probe-status split, frontend alerts gated, `auto_merge_enabled` + `ci_wait_timeout_s` + `version_source` knob rows, `pr_comment` transition, `gsheet` startup error, mode-qualified compliance criterion, `security_issue_mode` demoted to a constant, CodeQL `schedule: weekly` verified with the owner token). |

No finding was rejected. All three former open questions are closed: OQ1/OQ2 by the 0d probe
resolving `ci_evidence_mode = local` and `auto_merge_enabled = false` (§10.1, §13), OQ3 by the
§4.2 candidate table.

---

## Open questions / decisions needing a human

None outstanding. The evidence run's operating point, decided by the plan rather than left open:

- `ci_evidence_mode = local` — the fork has never completed a `pull_request` workflow run, so
  §19's REPO B criterion is satisfied by local evidence (`pre-commit run --files` + targeted
  pytest, attached to the PR) and named as such in `RESULTS.md`.
- `auto_merge_enabled = false` — forced by the line above, and independently advisable: the fork
  has no branch protection, so an "auto-merge" would merge on any green result. Eligibility is
  demonstrated as a computed, logged decision, not an actual merge.
- LANE 3 ships one automatable candidate (`normalize_indexes`); the second EOL-passed site is
  human-routed as public API surface.
