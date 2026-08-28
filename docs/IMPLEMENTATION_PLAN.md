# Implementation Plan — Event-Driven Devin Remediation Pipeline for Apache Superset

> **Status:** version-controlled source of truth. This document is what the build-time
> plan-review step (T-1, §2) reviews against, and what every later change must be
> reconciled with. Referenced from the README.
>
> **Revision 9 (consolidated)** — the plan-review loop is closed: round 8 returned
> `approved_with_minor` with **0 blocking and 0 major**, and this revision folds in its three
> minor and three nit findings. Incorporates T-1 review rounds 1–8: round 1 (5 blocking / 15 major /
> 10 minor / 2 nit), round 2 (7 major / 8 minor / 2 nit), round 3 (2 major / 4 minor / 2 nit),
> round 4 (1 major / 2 minor / 3 nit), round 5 (1 major / 4 minor / 2 nit), round 6
> (1 major / 2 minor / 2 nit), round 7 (1 major / 4 minor / 4 nit) and round 8
> (0 major / 3 minor / 3 nit). See §20 for the
> revision log and the finding-by-finding disposition of every round.

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
- **35** unconditional skip decorator instances under `tests/` *(R4-n-03, R5-n-02 — the
  enumerator counts instances; at this HEAD the 35 included rows are 35 distinct nodeids while
  the 33 exclusions include multi-decorator nodes — see the §5 enumerator limits)*, **as produced
  by the §5 LANE 2 enumerator**
  *(R2-M-04, corrected by R3-M-02)*, plus **33** conditional sites recorded separately in
  `baseline.excluded_conditional_skips`, split by reason *(R3-m-03)*: **30**
  `conditional_environment_guard` (every `skipif`/`skipUnless` in the tree is an availability,
  backend, feature-flag or opt-in-suite guard *(R5-n-01)*) and **3** `expected_failure_xfail` (an expected failure is still collected, so
  it is not disabled coverage — see §5). The 35th included site is the alias-imported
  `@skip("Flaky")` at `tests/integration_tests/databases/commands_tests.py:118`, which a
  dotted-name-only matcher missed. Of the 35, **6** collect more than one item
  (`collects_single_item = 0`: 4 class-level skips and 2 parametrized functions) and **2** are
  nested inside another skipped class (`enclosing_skip_nodeid`) — see §9.1 and §9.2
  *(R6-M-01, R6-m-01)*.
- **Field semantics** *(R6-n-02)* — every baseline record reports `line` as the **definition**
  line (`def` / `class`) and `decorator_line` as the matched decorator's own line, in both LANE 2
  and LANE 3, so the two lanes are diff-comparable against grep output. The two differ wherever
  a decorator stack sits above the definition (e.g. `normalize_indexes`: decorator 1542,
  definition 1543).
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
   product judgment. For LANE 2 this is checked against the record's breadth *(R6-m-02)*: a
   `kind = class` candidate re-enables every method in the class at once (the live rows run from
   11 to 52 enclosed tests, under reasons that blame fixture/example-data incompatibility), which
   is not a well-scoped transformation and cannot be scored as a test-only diff at risk 1–2.
   `enclosed_tests > lane2_class_breadth_max` (default **5**) fails the gate with reason
   `class_scope_too_broad` and is human-routed; at or below the threshold the LANE 2 rubric adds
   +1 risk for any `kind = class` row. Because `enclosed_tests` counts direct-body test methods
   only *(R7-m-04)*, a `kind = class` row reporting `enclosed_tests = 0` is **also** human-routed
   (reason `class_breadth_unknown`) rather than treated as a single-item candidate — that shape
   means the class inherits its tests, and its true breadth is unknown to the enumerator.
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

- The **gate** asks whether *any* well-scoped transformation / pass-fail signal exists at all.
  It is **rubric `>= 2` AND every lane-specific hard condition** *(R7-m-03)*. The complete list
  at this revision *(R8-n-01)*: the LANE 1 scope check (§5, reason `out_of_scope_frontend`), the
  LANE 2 breadth check (§4 gate 2, reasons `class_scope_too_broad` / `class_breadth_unknown`),
  the LANE 2 overlap check (§9.2, reason `blocked_by_enclosing_skip`) and the LANE 3
  `no_internal_callers_and_no_override_surface` check (§4.2, reasons `public_api_surface` /
  `internal_caller`). A rubric `1` fails the gate, and so does any hard condition, each with its
  own recorded reason; a gate module implementing only the rubric threshold is wrong. A lane that
  adds a hard condition must add it here and to §17.
- The **score factor** grades *how cleanly*, on `2..5`. Because gate-failing `1`s are already
  removed, the factor carries only residual quality information and cannot double-count a
  no/yes decision.

Per-lane rubric tables are config data (`config/rubrics.yaml`), one table per lane per factor,
each row mapping an observable property to a value. Defaults:

| Lane | `business_impact` anchor | `signal_quality` anchor | `risk` anchor |
|---|---|---|---|
| 1 — CodeQL | alert `security_severity_level` (critical 5 … note 1) | rule precision + `updated_at` freshness | blast radius of the touched module |
| 2 — skipped test | breadth of the covered surface | skip `reason` specificity (a `TODO:` with a cause = 4; bare skip = 2) | test-only diff ⇒ 1–2, **+1 when `kind = class`** *(R6-m-02)* |
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
  `@pytest.mark.skip` and `@unittest.skip` — unconditional skips **only**. **Decorator names are
  resolved through the module's import bindings before matching** *(R3-M-02)*: `import`/`from …
  import … as …` statements are collected per file and the decorator's leading segment rewritten,
  so `from unittest import skip` + `@skip("Flaky")` and `from pytest import mark` +
  `@mark.skip` resolve to `unittest.skip` / `pytest.mark.skip` and are enumerated. Matching the
  written dotted name alone silently dropped a real candidate
  (`tests/integration_tests/databases/commands_tests.py:118`). Excluded, and counted separately
  under a two-value reason enum so the exclusion is auditable *(R3-m-03)*:
  `conditional_environment_guard` for `skipif` / `skipUnless` (every one of the target's sites is
  an availability, backend, feature-flag or opt-in-suite guard — `ocient_is_installed()`,
  hive/boto3/thrift/pyhive/pydruid availability, marshmallow-version,
  `is_feature_enabled("THUMBNAILS")`, the `SUPERSET_PERF_VALIDATION` opt-in suite — i.e. correct
  by design, not debt) *(R5-n-01)* and `expected_failure_xfail`
  for `xfail` (an expected failure that is still collected and reported, not disabled coverage).
  The enumerator and `scripts/build_baseline.py` share this definition. Because a shared
  definition makes a count-equality assertion tautological, the §17 guard is **fixture-based**
  *(R3-M-02)*: a checked-in mini-tree containing a bare aliased `@skip`, a fully qualified
  `@pytest.mark.skip`, a `skipUnless` guard and an `xfail` must classify as 2 included / 2
  excluded with the right reasons, and a separate drift test re-runs the enumerator over the
  target checkout and compares against `baseline.totals`.
  `tests/integration_tests/model_tests.py` is **not** a source: all 11 of its skips are
  `skipUnless(is_module_installed(...))` guards.
- **LANE 2 enumerator limits**, stated so the 35/33 split is not read as a complete inventory of
  every way a Superset test can be disabled *(R4-m-01, R4-m-02, R4-n-03)*:
  - The enumerator is **decorator-based**. Module- or class-level `pytestmark = pytest.mark.skip(…)`
    assignments and imperative in-body `pytest.skip()` / `self.skipTest()` calls are **out of
    scope** — in the target every such site is environment-conditional, so no backlog candidate
    is lost, but they appear in neither the included nor the excluded set.
  - **`enclosed_tests` counts direct-body test methods only** *(R7-m-04)* — it is a lower bound
    on the collected item count: inherited test methods and per-method `parametrize` expansion
    are not counted. At `a140e74` the four class rows' counts (52 / 25 / 13 / 11) are exact
    (their bases contribute no test methods and none of their methods is parametrized), but a
    future subclass that inherits its tests would report `0`, which is why §4 human-routes a
    `kind = class` row with `enclosed_tests = 0` instead of scoring it as a single-item
    candidate. **Precedence** *(R8-m-03)*: the gate uses the live
    `pytest --collect-only <nodeid>` count whenever a collection is obtainable, and the AST lower
    bound otherwise; `class_breadth_unknown` applies only to a `kind = class` row for which no
    live count could be obtained **and** the AST count is `0`. `collects_single_item` is derived
    from `enclosed_tests` and inherits the same caveat *(R8-n-03)* — it is not meaningful for a
    `kind = class` row reporting `enclosed_tests = 0`, and §9.1 never reads it for such a row
    because §4 gates it out first.
  - **Indirect mark aliases** — a name bound at module level to a `pytest.mark.*` object and
    re-exported (`only_postgresql = pytest.mark.skipif(…)` in `conftest.py`, used as
    `@only_postgresql`) — are out of scope **regardless of import style** *(R5-m-03)*: five of the
    six usages arrive by absolute import and *are* rewritten by binding resolution, but the
    rewritten target names a mark object, not a skip decorator, so it matches nothing; the sixth
    (`from .conftest import …`) is relative and stays unresolved. All six are `skipif`-derived and
    therefore correctly non-candidates, but they are **not** among the 30 recorded
    `conditional_environment_guard` rows.
  - The enumerator counts *decorator instances*, not distinct test nodes: a node carrying two
    conditional decorators contributes two rows. At this HEAD that only affects the **33**
    exclusions (e.g. `model_tests.py` lines 62, 103 and 279 each contribute two); the **35**
    included rows happen to be 35 distinct nodeids *(R5-n-02)*.
  - Each included record carries the fully qualified nodeid used as its §14.1 locator, including
    the enclosing class *(R5-M-01)*.
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
- `lane2_class_breadth_max = 5` — enclosed-test ceiling above which a class-level LANE 2 skip
  fails `automatability` *(R6-m-02)*; safety-relevant, raising it lets one PR re-enable a whole
  test class unattended.
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

**Multi-item nodeids** *(R6-M-01)* — a locator is not always one-to-one with a collected item:
a class-level LANE 2 skip collects every test method in the class (the live rows range from 11
(`TestDatasourceValidateExpressionApi`) to 52 (`TestPostChartDataApi`) *(R7-n-01)*) and a
parametrized function collects
one item per parameter tuple. `fixtures/baseline.json` records this per row as `enclosed_tests`,
`parametrized` and `collects_single_item`; **6 of the 35** live candidates collect more than one
item. For those rows the contract is evaluated **per collected item** and the run is a valid
baseline iff **at least one** collected item exits `FAILED` matching `exception_type` /
`message_pattern` **and no** collected item is `SKIPPED`; the planner's `expected_failure` may
name the representative item's nodeid, and the per-item outcome vector is logged. `PASSED` for
*every* item is `stale_skip`; any `SKIPPED` item is `invalid_red_baseline` — **except** an item
whose own node carries its own unconditional marker *(R7-n-04)*: those are excluded from the
aggregate and recorded as `still_skipped_descendants`, because the scratch patch lifts ancestors
only (§9.2) and a descendant marker is a separate candidate with its own dispatch. The §9.2
classifier
reads that aggregate, so single-item rows keep the plain `FAILED` / `PASSED` / `SKIPPED` mapping
as a special case of the same rule.

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
- **Nested markers** *(R6-m-01)* — an enumerated node can sit lexically inside another skipped
  node (2 of the 35 live candidates are methods of `TestPostChartDataApi`, itself a class-level
  candidate). The enumerator records `enclosing_skip_nodeid` for those rows; the reviewer's
  scratch patch MUST lift **every** unconditional marker on the path to the node before
  classifying, otherwise the run returns `SKIPPED` and a genuine backlog item is permanently
  misclassified `invalid_red_baseline`. Those lifts are **scratch-only and are never committed**
  *(R7-M-01)*: the committed test-path diff of a candidate contains markers **inside its own
  node** and nothing else. Otherwise a nested child — which passes the §4 breadth gate on its own
  `enclosed_tests = 0` — would ship a PR deleting its 52-test parent's class marker, routing
  around the very gate `lane2_class_breadth_max` exists to enforce.
- **LANE 2 overlap rule** *(R7-M-01, R7-m-01, R8-m-01)* — because the committed diff cannot touch
  the parent, the governing principle is general: **a child cannot be remediated independently
  while any ancestor marker would survive the merge**, since merging it would leave the test
  skipped and §19's "re-enabled test" claim would be false. Concretely, for a child carrying
  `enclosing_skip_nodeid`:
  - the enclosing candidate is **dispatched in this run** (it passed every gate and is high-tier)
    — the two overlap, so the orchestrator dispatches only the enclosing candidate and suppresses
    the child, recording `related_candidate_id` on both rows. Overlap is detected by **nodeid
    containment** over the **current-run candidate rows** *(R8-n-02 — deliberately not §14.1's
    `superseded_by`-filtered "active" set, which is LANE 1 drift vocabulary)*, not by the §14.1
    marker search, whose key `candidate_id = sha256(lane|repo|stable_locator)` differs between
    `path::Class` and `path::Class::method` and would therefore match nothing *(R7-m-01)*.
  - the enclosing candidate is **not dispatched in this run** for any reason — it failed the
    breadth gate (`class_scope_too_broad` / `class_breadth_unknown`), failed any other gate,
    scored below `tier_high_min`, or was deferred by `budget_N` — then the child fails the gate
    with reason `blocked_by_enclosing_skip` and is human-routed together with its parent. This
    branch is the default: it covers every state other than a confirmed same-run parent dispatch.
- **Suppressed / blocked lifecycle** *(R8-m-02)* — both outcomes above are **non-terminal**. The
  row is written as `suppressed_by_containment` or `blocked_by_enclosing_skip` with its
  `related_candidate_id`, and is re-evaluated from scratch on every subsequent run: once the
  ancestor marker is gone from the target, the child re-enumerates as an ordinary candidate and
  dispatches normally. Such rows count in the §11 burn-down **denominator** (the backlog item is
  still open) and never in the numerator, so suppression cannot inflate progress.
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
**`License Check`** *(R3-n-01 — the rendered check context is the job's `name:`, not its
`license_check:` job id)*: an Apache-RAT / `setup-java` workflow invoking
`./scripts/check_license.sh` — not a pre-commit hook either. Everywhere the plan matches
contexts against a PR head it uses the rendered strings.

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

**One-way upgrade** *(R3-m-02)* — 0d resolves the mode from *history*, so a fork whose Actions
were only just enabled would otherwise stay pinned to `local` forever even though the pipeline's
own first PR can trigger the contexts. After a generated PR head has any required context report
within `ci_wait_timeout_s`, the mode re-resolves `local → github` once, logged as an explicit
mode transition in the Layer 1 event log. The transition is one-way (never `github → local`
except via the timeout downgrade above, which is per candidate). A fork PR sitting in
**pending workflow approval** — GitHub's first-time-contributor / fork gate — reports no context
and therefore keeps the mode at `local`; that state is recorded as
`ci_evidence_unavailable: awaiting_workflow_approval` rather than treated as a failure.

---

## §11 Observability

Answers "how would an engineering leader know this is working?"

- **Layer 1 — structured JSONL event log (source of truth)** — per candidate: `run_id`, `lane`,
  `candidate_id`, gate result (+ which gate failed), score + factor breakdown, tier, action,
  Devin `session_id`s by role (planner/implementer/reviewer), iterations, PR/issue URLs,
  `test_added` / `test_paths` / `test_author` / `test_exempt_reason`, terminal outcome, plus
  *(R7-n-03)* `red_baseline.per_item_outcomes` (the multi-item vector of §9.1, including any
  `still_skipped_descendants`), the LANE 2 breadth fields (`enclosed_tests`, `parametrized`,
  `collects_single_item`), `lifted_markers` (the scratch-only ancestor lifts of §9.2, recorded so
  the never-committed rule is auditable) and `related_candidate_id`.
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
- **Suppressed LANE 2 rows** *(R8-m-02)* — a candidate held in `blocked_by_enclosing_skip` or
  `suppressed_by_containment` (§9.2) counts in the burn-down **denominator** only, never as
  progress, and is re-evaluated on every run.
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
request. The orchestrator then verifies, for any attempt `> 1`, that the response really is a new
session. `is_new_session` is typed `boolean | null`, so the check is **tri-state** *(R3-m-01)*:
`true` → proceed; `false` → dedupe hit → fatal orchestration error; `null`/absent → unknown, fall
back to comparing the returned `session_id` against the id recorded for the previous attempt
(equal → fatal, different → proceed). A missing field must not abort a legitimate retry. Both the
raw tri-state value and the resolved decision are recorded in the Layer 1 event log.

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
| `ci_evidence_mode` | resolved by §3 0d, then subject to the §10.1 one-way `local → github` re-resolution *(R4-n-02)* | `github`, `local` | `local` forces `auto_merge_enabled = false` |
| `ci_wait_timeout_s` | `5400` | `> 0` | on expiry → `ci_evidence_unavailable`, evidence downgraded to `local`, never auto-merge *(R2-M-06)* |
| `auto_merge_enabled` | `false` | `true`, `false` | **safety-relevant** *(R2-m-05)* — forced `false` whenever `ci_evidence_mode = local`, and never sufficient on its own (§6 tier, §9 invariants and §10 gates all still apply) |
| `issue_sink` | `issues` | `issues`, `pr_comment` | `pr_comment` tags the run `artifact_degraded` |
| `version_source` | `.github/ISSUE_TEMPLATE/bug-report.yml` | repo-relative path | drift-tested; yielding no concrete release is a startup error, not an empty lane *(R2-M-02)* |
| `lane2_class_breadth_max` | `5` | integer `>= 1` | **safety-relevant** *(R7-m-02)* — raising it lets one PR re-enable a whole test class unattended; `enclosed_tests` above it fails `automatability` as `class_scope_too_broad` (§4) |

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
| 2 — skipped test | the **fully qualified, collectable** pytest nodeid *(R5-M-01)* — `path::Class::method` for a class-nested test, `path::Class` for a class-level skip, `path::function` only for a module-level test. A nodeid that omits the enclosing class does not collect (`no tests ran`), which §9.1 would then read as a collection error and classify `invalid_red_baseline`; 28 of the 35 live LANE 2 candidates are class-nested, so this is the common case, not an edge case. `scripts/build_baseline.py` carries class scope and `fixtures/baseline.json` records it as `class_scope`. The locator is collectable but **not** necessarily one-to-one with a collected item — see §9.1 on the 6 multi-item rows *(R6-M-01)*. |
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

**Cross-commit drift** *(R3-m-04)*. Absolute line/column means an unrelated edit *above* the
alert shifts the digest and would mint a new `candidate_id` for the same underlying alert —
inflating the burn-down denominator and re-dispatching handled work. Dedupe is therefore not
keyed on `candidate_id` alone: before dispatching a LANE 1 candidate the orchestrator (1) runs
the `<!-- devin-remediation-id: … -->` marker search over existing issues/PRs, and (2) attempts a
**drift match** against `state/candidates.jsonl`. A hit on either links the shifted alert to its
prior candidate record (`superseded_by: <new candidate_id>` on the old row, `supersedes` on the
new one) and suppresses re-dispatch. The positional digest remains the primary identity; these
are the drift safety nets.

**The drift match must not undo the positional discriminator** *(R4-M-01)*. `(rule_id,
file_path, normalized_symbol)` is exactly the key shown above to collide on live data, so using
it alone would suppress three of the four co-located `py/overly-large-range` alerts as false
drift hits. The match is therefore two-condition and lossy-by-default-off:

1. **Unambiguity** — the weak key must have multiplicity **1** on both sides: exactly one alert
   in the current scan and exactly one **active** state row carry it. "Active" means rows that do
   **not** carry `superseded_by` *(R5-m-01)* — otherwise the append-only store would reach
   multiplicity 2 after the first successful link and permanently disable its own drift net. Any
   multiplicity > 1 disables the drift path for that key; every alert under it is treated as
   distinct and dispatched on its `position_digest`.
2. **Content anchor** — even when unambiguous, the candidates must agree on
   `region_digest = sha256(<the alert's source region text, whitespace-normalized>)[:12]`, or
   failing that on the offset of the alert region relative to the enclosing symbol's start line.
   A pure line shift preserves both; a genuinely different alert does not. Both
   `region_digest` and `symbol_relative_offset`, plus the `base_sha` they were computed at, are
   **written into the state row at dispatch** *(R5-m-02)*: the comparison reads persisted values
   and never re-reads source at a prior commit.

Neither condition can be relaxed by config.

**Residual cost, accepted deliberately** *(R5-m-04)*. Condition 1 means a co-located group can
never drift-link: any edit above line 55 of `add_chart_to_existing_dashboard.py` re-dispatches all
four `py/overly-large-range` alerts as new candidates, and the marker search cannot catch it
because the marker is keyed on the changed `candidate_id`. That is the safe direction — duplicate
artifacts are visible and closable, silent suppression of a real security alert is not — and it is
bounded by `budget_N` and human review. A one-to-one pairing on `(region_digest, within-line
column ordinal)` inside a multiplicity > 1 group is recorded as a possible later refinement, not
shipped in v1. §17 asserts both directions: the four co-located
fixture alerts are all dispatched (none suppressed as drift), and a single alert re-read after a
pure line shift is linked rather than re-dispatched.

`state/candidates.jsonl` (append-only, last-write-wins by `candidate_id`) is the **dedupe and
resume source of truth** — distinct from the Layer 1 observability log. States:

```
enumerated → gated → scored → dispatching → issue_created → pr_created → converged → terminal
                                          └→ pr_created → comment_created → …   (issue_sink = pr_comment)
         └→ blocked_by_enclosing_skip | suppressed_by_containment   (LANE 2 overlap; NON-terminal,
                                                                     re-evaluated every run — §9.2)
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
- an **implementer** diff touching an assertion **or a skip marker** is rejected, while the
  **reviewer's** skip-marker-only diff is accepted *(B-04, corrected by R3-M-01 — one specified
  behaviour per role, matching §9.2/§9.3)*; a LANE 2 test passing pre-fix yields `stale_skip`.
- tier mapping at the threshold boundaries (`59/60`, `19/20`) and the `score_cap` clamp *(B-05)*.

**Added to close T-1 review round 2**

- `test_codeql_locator_separates_colocated_alerts` — the four `py/overly-large-range` fixture
  alerts on `add_chart_to_existing_dashboard.py:55` yield four distinct `candidate_id`s
  *(R2-M-01)*.
- `test_current_major_from_version_source` — resolves `6.1.0 → 6` from the bug-report form, and
  a form with no concrete release raises a startup config error rather than silently emptying
  LANE 3 *(R2-M-02)*.
- `test_burndown_reports_na_for_invalid_baseline_lane` *(R2-M-03)*.
- `test_enumerator_classifies_skip_fixture_tree` — the checked-in mini-tree (aliased bare
  `@skip`, qualified `@pytest.mark.skip`, `skipUnless`, `xfail`) yields 2 included and 2 excluded
  with reasons `conditional_environment_guard` / `expected_failure_xfail` *(R2-M-04, R3-M-02,
  R3-m-03)*, plus `test_enumerator_matches_baseline_totals` as the drift check against the
  target checkout.
- `test_retry_asserts_new_session` — attempt `> 1` returning `is_new_session == false` is a fatal
  orchestration error; `null` falls back to the session-id comparison and only a **repeated**
  session id is fatal *(R2-M-05, R3-m-01)*.
- `test_ci_wait_timeout_downgrades_evidence` — expiry → `ci_evidence_unavailable`, evidence
  `local`, not auto-merge eligible *(R2-M-06)*.
- `test_implementer_skip_marker_hunk_rejected` and
  `test_reviewer_owns_lane2_baseline_classification` (failed / passed / skipped → valid /
  `stale_skip` / `invalid_red_baseline`) *(R2-M-07)*.
- `test_frontend_alerts_gated_out` — the three JS/TS fixture alerts fail `verifiability_exists`
  with reason `out_of_scope_frontend` *(R2-m-04)*.
- `test_gsheet_sink_rejected_in_simulate` *(R2-m-07)*.
- `test_pr_comment_sink_state_transition_and_validation` *(R2-m-06)*.

**Added to close T-1 review round 3**

- `test_ci_evidence_mode_upgrades_once` — a required context reporting on a generated PR head
  flips `local → github` exactly once and logs the transition; a pending workflow-approval state
  leaves it at `local` *(R3-m-02)*.
- `test_shifted_alert_reuses_prior_candidate` — an alert whose only change is a line offset is
  linked to its prior `candidate_id` via the marker search / drift match and is not re-dispatched
  *(R3-m-04)*.

**Added to close T-1 review rounds 4–6** *(R7-n-02)*

- `test_colocated_alerts_all_dispatched` — all four `py/overly-large-range` fixture alerts are
  dispatched; none is suppressed as a drift match, because their weak key has multiplicity 4
  *(R4-M-01)*. Paired with `test_drift_match_requires_region_digest`, where an unambiguous weak
  key but a differing `region_digest` does **not** link.
- `test_lane2_nodeids_are_collectable` — every enumerated LANE 2 nodeid resolves to **at least
  one** collected item (`pytest --collect-only <nodeid>`) and every record with a non-null
  `class_scope` contains the `::<Class>::` segment *(R5-M-01)*. One-to-one is **not** asserted:
  6 of the 35 live rows collect many items *(R6-M-01)*, and the test instead asserts that each
  such row carries `collects_single_item = 0` with a non-zero `enclosed_tests` or `parametrized`.
- `test_multi_item_red_baseline_classification` — for a multi-item locator, a run where one item
  FAILs with the expected signature and the rest pass is a valid baseline; the same run with any
  item `SKIPPED` is `invalid_red_baseline` and all-pass is `stale_skip` *(R6-M-01)*.
- `test_nested_skip_requires_lifting_parent` — a candidate carrying `enclosing_skip_nodeid` whose
  scratch patch lifts only its own marker is rejected before classification rather than recorded
  as `invalid_red_baseline` *(R6-m-01)*.
- `test_broad_class_skip_is_human_routed` — a `kind = class` candidate with
  `enclosed_tests > lane2_class_breadth_max` fails automatability with reason
  `class_scope_too_broad`; one with `enclosed_tests = 0` fails with `class_breadth_unknown`
  *(R6-m-02, R7-m-04)*.

**Added to close T-1 review rounds 7–8**

- `test_nested_child_commits_only_own_marker` — the committed test-path diff of a nested
  candidate contains no marker outside its own node, even though its scratch patch lifted the
  parent's *(R7-M-01)*.
- `test_child_under_gate_failed_parent_not_dispatched` — a child whose `enclosing_skip_nodeid`
  names a breadth-gate failure is gated out as `blocked_by_enclosing_skip`, not dispatched
  *(R7-M-01)*; where the parent passes, containment suppresses the child and records
  `related_candidate_id` on both rows *(R7-m-01)*.
- `test_gate_applies_hard_conditions_beyond_rubric` — a candidate with `automatability` rubric 4
  still fails the gate on a lane-specific hard condition, with the condition's own reason
  *(R7-m-03)*.
- `test_descendant_marker_excluded_from_aggregate` — a class-row baseline whose only `SKIPPED`
  items carry their own markers is valid, and those items are logged as
  `still_skipped_descendants` *(R7-n-04)*.
- `test_child_blocked_when_parent_not_dispatched_for_any_reason` — a child is gated
  `blocked_by_enclosing_skip` when its parent is dropped, low-tier or budget-deferred, not only
  when the parent fails the breadth gate *(R8-m-01)*.
- `test_blocked_child_redispatches_once_ancestor_marker_gone` — the blocked/suppressed row is
  non-terminal: a later run with the ancestor marker removed dispatches it, and while blocked it
  sits in the burn-down denominator only *(R8-m-02)*.
- `test_live_collection_count_overrides_ast_lower_bound` — the breadth gate prefers
  `pytest --collect-only`; `class_breadth_unknown` fires only when no live count is obtainable
  and the AST count is 0 *(R8-m-03)*.
- `test_drift_survives_repeated_shifts` — a second consecutive line shift of the same alert still
  links, because state-side multiplicity counts **active** rows only *(R5-m-01)*.
- `test_enumerator_scope_limits` — `pytestmark` assignments, imperative `pytest.skip()` bodies
  and relative-import mark aliases yield neither candidates nor exclusion rows, matching the §5
  documented scope *(R4-m-01, R4-m-02)*.

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
  reported contexts (`lint-check`, `License Check`, `pre-commit (current)`) are the evidence;
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
  `RESULTS.md` must name. Each candidate is labelled with the mode in force **at its own gate
  evaluation** *(R4-n-02)*, so a candidate evidenced before a mid-run `local → github` upgrade is
  reported as `local`; the transition itself is a separate logged event. `RESULTS.md` lists selected issues, gate/score rationale, and links.

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

### Revision 4 — T-1 plan review round 3

Reviewer verdict `changes_required`: 0 blocking, 2 major, 4 minor, 2 nit (all seven round-2
majors confirmed closed, none regressed). Disposition:

| Finding | Disposition |
|---|---|
| R3-M-01 §17 bullet contradicts §9.2/§9.3 on skip markers | **Accepted.** The stale bullet accepted an *implementer* skip-marker-only diff, which R2-M-07 had already made a rejection. Rewritten per role: implementer assertion **or** skip-marker hunk → rejected; reviewer skip-marker-only diff → accepted (§17). |
| R3-M-02 enumerator misses alias-imported skips | **Accepted.** The matcher compared written dotted names, so `from unittest import skip` + `@skip("Flaky")` (`tests/integration_tests/databases/commands_tests.py:118`) was dropped. `scripts/build_baseline.py` now resolves decorator names through per-module import bindings; baseline regenerated: **35** included (was 34). The tautological count-equality test is replaced by a fixture-tree classification test plus a drift check (§3 0e, §5, §17). |
| R3-m-01 `is_new_session` may be `null` | **Accepted.** Retry check made tri-state with a `session_id` comparison fallback so a missing field cannot abort a legitimate retry (§12.2). |
| R3-m-02 `ci_evidence_mode` pinned to `local` with no upgrade path | **Accepted.** One-way `local → github` re-resolution once a generated PR head reports a required context, logged as a mode transition; pending workflow approval recorded as `awaiting_workflow_approval` and keeps `local` (§10.1). |
| R3-m-03 all exclusions labelled `conditional_environment_guard` | **Accepted.** Two-value reason enum: 30 `conditional_environment_guard` + 3 `expected_failure_xfail` (§3 0e, §5, enumerator). |
| R3-m-04 positional digests drift across commits | **Accepted.** Drift documented with marker search + weaker `(rule_id, path, normalized_symbol)` match and `supersedes` linkage before dispatch (§14.1) + test. |
| R3-n-01 `license_check` is the job id | **Accepted.** Rendered context `License Check` used wherever contexts are matched (§10, §19). |
| R3-n-02 stale counts/fixture name in `docs/PHASE0_DISCOVERY.md` | **Accepted.** Raw pre-scoping count annotated as such, final counts corrected to 35 / 33 with the xfail split, fixture reference corrected to `fixtures/baseline.json`. |

No finding was rejected.

### Revision 5 — T-1 plan review round 4

Reviewer verdict `changes_required`: 0 blocking, 1 major, 2 minor, 3 nit. All eight round-3
findings confirmed resolved against the real tree (the reviewer re-ran
`scripts/build_baseline.py` itself and reproduced `fixtures/baseline.json`). The one major was a
**regression introduced by Revision 4 itself**. Disposition:

| Finding | Disposition |
|---|---|
| R4-M-01 the R3-m-04 drift net re-introduces the R2-M-01 collision | **Accepted.** Correct and serious: `(rule_id, path, normalized_symbol)` is the very key §14.1 documents as colliding, so three of the four co-located `py/overly-large-range` alerts would have been suppressed as false drift hits. The drift match is now two-condition — weak-key multiplicity must be 1 on both sides, **and** a `region_digest` (or symbol-relative offset) must agree — neither relaxable by config, plus a §17 test asserting all four co-located alerts are dispatched (§14.1, §17). |
| R4-m-01 relative-import mark aliases resolve to bare names | **Accepted.** `from .conftest import only_postgresql` is out of the enumerator's absolute-import resolution, so its six usages are in neither set. They are `skipif`-derived and so correctly non-candidates; §5 now states the limit explicitly and no longer implies `only_postgresql` is among the 30 recorded guards. |
| R4-m-02 `pytestmark` and imperative skips invisible | **Accepted.** §5 now names module/class-level `pytestmark` and in-body `pytest.skip()` / `skipTest()` as out of the decorator-based scope, with a §17 test. |
| R4-n-01 header still says "Revision 3" | **Accepted.** Header revision and round list corrected (and kept current each revision since). |
| R4-n-02 §13 row and §19 silent on the mode upgrade | **Accepted.** §13 row cites the §10.1 one-way re-resolution; §19 states each candidate is labelled with the mode in force at its own gate evaluation. |
| R4-n-03 35/33 are decorator instances | **Accepted.** Stated as decorator instances in §3 0e, §5 and the discovery doc. |

No finding was rejected.

### Revision 6 — T-1 plan review round 5

Reviewer verdict `changes_required`: 0 blocking, 1 major, 4 minor, 2 nit. All six round-4
findings confirmed resolved against the real tree, including an independent re-derivation of the
four co-located alerts' columns and a reproduction of `fixtures/baseline.json`. The major is a
pre-existing defect in the LANE 2 locator that four prior rounds missed. Disposition:

| Finding | Disposition |
|---|---|
| R5-M-01 LANE 2 nodeids omit the enclosing class | **Accepted.** Verified: 28 of the 35 candidates are class-nested methods, and `path::method` does not collect — §9.1 would have classified 28 of 35 as `invalid_red_baseline`. `collect_skipped_tests` now carries class scope, `fixtures/baseline.json` is regenerated with fully qualified nodeids plus a `class_scope` field (totals unchanged at 35/33), §14.1 states the locator must be collectable, and §17 adds a collectability assertion. |
| R5-m-01 append-only state defeats the multiplicity-1 condition | **Accepted.** Multiplicity is now computed over **active** rows (no `superseded_by`), with a §17 case for a second consecutive shift. |
| R5-m-02 `region_digest` never persisted | **Accepted.** `region_digest`, `symbol_relative_offset` and the `base_sha` they were computed at are written into the state row at dispatch; the anchor comparison reads persisted values only. |
| R5-m-03 mark-alias gap misattributed to relative imports | **Accepted.** Restated: indirect mark aliases are out of scope regardless of import style because the resolved target is a mark object, not a skip decorator; relative imports merely add a second reason. |
| R5-m-04 co-located groups can never drift-link | **Accepted as a documented tradeoff.** The residual cost (duplicate re-dispatch after an edit above the group) is now stated explicitly, with the rationale that duplication is the safe failure direction and bounded by `budget_N` + review; one-to-one pairing within a group is recorded as a later refinement. |
| R5-n-01 "availability or backend guard" overreaches | **Accepted.** Widened to availability / backend / feature-flag / opt-in-suite, with the thumbnails flag and the perf-suite env var named. |
| R5-n-02 the 35 are distinct nodes | **Accepted.** The instance-vs-node caveat is scoped to the 33 exclusions; the 35 are noted as 35 distinct nodeids at this HEAD. |

No finding was rejected.

### Revision 7 — T-1 plan review round 6

Reviewer verdict `changes_required`: 0 blocking, 1 major, 2 minor, 2 nit. All seven round-5
findings confirmed resolved against a fresh clone at `a140e74`, with `fixtures/baseline.json`
reproduced byte-identically apart from `captured_at`, every class-qualified nodeid cross-checked
against the AST at its recorded line, and the new scope-based `_iter_definitions` confirmed to
introduce no enumeration regression (an `ast.walk` variant yields the identical 35/33 set). The
major is the second-order consequence of the R5-M-01 fix. Disposition:

| Finding | Disposition |
|---|---|
| R6-M-01 nodeids are collectable but not one-to-one | **Accepted.** Verified: 4 class-level rows enclose 11–52 tests and 2 module-level rows are parametrized, so 6 of 35 locators collect >1 item, making the §17 one-to-one assertion false and §9.1's single-signature contract undefined. Took the reviewer's option (b) plus instrumentation: the enumerator now emits `enclosed_tests`, `parametrized` and `collects_single_item`; §9.1 defines the multi-item contract (valid iff ≥1 item FAILs with the expected signature and no item is SKIPPED, per-item outcomes logged), §17's assertion is reworded to ≥1 collected item, and two §17 cases cover the aggregate classifier. |
| R6-m-01 nested markers make child candidates permanently unclassifiable | **Accepted.** Verified: the two `TestPostChartDataApi` methods sit inside a class-level skip. The enumerator records `enclosing_skip_nodeid` (2 live rows) and §9.2 requires the reviewer's scratch patch to lift every unconditional marker on the path to the node before classifying. |
| R6-m-02 class-level skips are not well-scoped transformations | **Accepted.** `lane2_class_breadth_max = 5` added as a safety-classified knob: `enclosed_tests` above it fails `automatability` with reason `class_scope_too_broad` and is human-routed; at or below it the LANE 2 rubric adds +1 risk for `kind = class`. |
| R6-n-01 round-5 tests filed under a round-4 heading | **Accepted.** Heading is now "Added to close T-1 review rounds 4–5". |
| R6-n-02 `line` means different things per lane | **Accepted.** Both lanes now emit `line` (definition) **and** `decorator_line`; the convention is stated in §3 0e. |

No finding was rejected.

### Revision 8 — T-1 plan review round 7

Reviewer verdict `changes_required`: 0 blocking, 1 major, 4 minor, 4 nit. All five round-6
findings confirmed resolved against a fresh clone at `a140e74` — `fixtures/baseline.json`
reproduced byte-identically apart from `captured_at`, the four class rows independently counted
at 52 / 25 / 13 / 11 direct test methods (their base classes contribute none), the two
parametrized rows and the two nested rows confirmed, and `line` / `decorator_line` correct in
both lanes. The major is again second-order: Revision 7's own nested-marker rule and breadth gate
collided. Disposition:

| Finding | Disposition |
|---|---|
| R7-M-01 nested child either bypasses the breadth gate or ships a still-skipped test | **Accepted.** §9.2 now states that ancestor lifts are **scratch-only and never committed** (a candidate's committed diff carries markers inside its own node only), and adds the LANE 2 overlap rule: a child whose `enclosing_skip_nodeid` names a breadth-gate failure is gated out as `blocked_by_enclosing_skip` and human-routed with its parent; where the parent passes, containment suppresses the child. Two §17 cases added. |
| R7-m-01 marker search cannot dedupe overlapping LANE 2 candidates | **Accepted.** Verified: `path::Class` and `path::Class::method` hash to different `candidate_id`s, and §14.1's drift net is LANE 1-only. Replaced with explicit **nodeid containment** against active state rows; `related_candidate_id` added to the §11 event fields and the state row. |
| R7-m-02 `lane2_class_breadth_max` missing from the §13 knob table | **Accepted.** Added with default 5, allowed `>= 1`, classified safety-relevant. |
| R7-m-03 §4.1 says the gate is "equivalent to rubric >= 2" | **Accepted.** Reworded: the gate is rubric `>= 2` **and** every lane-specific hard condition (LANE 2 breadth, LANE 2 overlap, LANE 3 caller/override), each with its own reason; a rubric-only gate module is wrong. |
| R7-m-04 `enclosed_tests` is a lower bound | **Accepted.** Documented as a §5 enumerator limit (direct-body test methods only; inherited and parametrized items uncounted), `pytest --collect-only` supersedes it where available, and §4 human-routes a `kind = class` row reporting `enclosed_tests = 0` as `class_breadth_unknown` instead of treating it as single-item. |
| R7-n-01 "extremes" misstated | **Accepted.** §9.1 now says the live rows range 11–52. |
| R7-n-02 round-6 tests under a rounds-4–5 heading | **Accepted.** Heading widened to rounds 4–6 and a round-7 block added. |
| R7-n-03 new logged data absent from §11 | **Accepted.** Layer 1 extended with `red_baseline.per_item_outcomes`, the breadth fields, `lifted_markers` and `related_candidate_id`. |
| R7-n-04 descendant markers leave a class row unclassifiable | **Accepted.** §9.1 excludes items carrying their own unconditional marker from the aggregate and logs them as `still_skipped_descendants`, consistent with §9.2 lifting ancestors only. |

No finding was rejected.

### Revision 9 (consolidated) — T-1 plan review round 8, loop closed

Reviewer verdict `approved_with_minor`: **0 blocking, 0 major**, 3 minor, 3 nit. All nine round-7
findings confirmed resolved against real repository facts: `fixtures/baseline.json` reproduced
byte-identically from a fresh clone at `a140e74` apart from `captured_at`; the four class breadths
independently re-derived from the AST as 52 / 25 / 13 / 11 with the base classes
(`BaseTestChartDataApi`, `SupersetTestCase`) confirmed to contribute no test methods, so the counts
are exact at this HEAD; the two nested rows confirmed as `TestPostChartDataApi::test_chart_data_dttm_filter`
and `::test_chart_data_async`; and no included row found inside a conditionally-skipped class, so
the new `blocked_by_enclosing_skip` path is exercised on exactly those two rows and 29 function-level
candidates remain reachable for the §19 LANE 2 evidence. Disposition of round 8:

| Finding | Disposition |
|---|---|
| R8-m-01 overlap rule covered only the breadth-gate branch | **Accepted.** §9.2 now states the general principle — a child cannot be remediated while **any** ancestor marker survives the merge — and enumerates two exhaustive branches: parent dispatched this run (containment-suppress the child) versus parent not dispatched *for any reason* (gate the child `blocked_by_enclosing_skip`), the latter being the default. §17 case added. |
| R8-m-02 no lifecycle for a suppressed/blocked child | **Accepted.** Both outcomes are explicitly non-terminal states in the §14.1 state machine, re-evaluated every run and re-dispatched once the ancestor marker is gone; §11 counts them in the burn-down denominator only. §17 case added. |
| R8-m-03 AST count vs live collection precedence undefined | **Accepted.** §5 states the precedence: live `pytest --collect-only` where obtainable, AST lower bound otherwise, and `class_breadth_unknown` only when no live count is obtainable and the AST count is 0. §17 case added. |
| R8-n-01 §4.1 hard-condition list incomplete | **Accepted.** LANE 1 `out_of_scope_frontend` and `class_breadth_unknown` added; the list is declared complete and must be extended with any new lane condition. |
| R8-n-02 "active state rows" overloads §14.1's drift vocabulary | **Accepted.** LANE 2 containment now reads **current-run candidate rows**, explicitly not the `superseded_by`-filtered set. |
| R8-n-03 `collects_single_item` inherits the lower-bound caveat | **Accepted.** Noted in §5, with the observation that §4 gates such a row out before §9.1 reads the field. |

**Cap note** — §2 bounds T-1 at `iteration_cap = 5`. The loop ran to 8 because rounds 5–7 each
surfaced a genuine major that was a second-order consequence of the previous round's fix, all
inside LANE 2 skip-marker semantics. Per §2 the overrun was escalated to the human owner, who
authorised one final round with the standing instruction that any remaining major settleable only
by writing the enumerator be carried into implementation rather than looped again. Round 8 raised
none.

No finding was rejected. **The plan-review loop is closed at round 8** with 0 blocking and 0 major;
blocking has been 0 since round 1. This revision is the consolidated plan handed to the human owner
for final approval before T2–T13 implementation begins.

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
