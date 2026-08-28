# Implementation Plan — Event-Driven Devin Remediation Pipeline for Apache Superset

> **Status:** version-controlled source of truth. This document is what the build-time
> plan-review step (T-1, §2) reviews against, and what every later change must be
> reconciled with. Referenced from the README.

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
   BEFORE any build. Blocking/major findings on the plan loop back to the planner/human;
   revisions are committed back to `docs/IMPLEMENTATION_PLAN.md` so history is auditable.
   Bounded by `iteration_cap = 5`, then human escalation.
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
score = business_impact × verifiability × automatability × signal_quality / risk
```

- Each factor on a 1–5 rubric (define per-lane rubrics in config).
- `risk` is floored at 1 and the composite capped, so a near-zero risk estimate can't make a
  trivial change outrank a genuine security fix.

---

## §5 Lanes (first two are the required demo lanes)

- **LANE 1 — CodeQL security alerts** (via `GET /repos/{owner}/{repo}/code-scanning/alerts`);
  trigger anchor: `.github/workflows/codeql-analysis.yml` runs daily 04:00 UTC.
- **LANE 2 — skipped/flaky-test backlog** (e.g. `tests/integration_tests/model_tests.py`,
  `sqllab_tests.py`); re-enable and verify.
- **LANE 3 — EOL-passed `@deprecated` removals** (e.g. `get_url_for_impersonation`,
  `normalize_indexes` in `superset/db_engine_specs/base.py`); verify via targeted
  `tests/unit_tests/db_engine_specs/`.
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
  merging auto-closes the manager-facing issue). Create the issue first to get its number, or
  patch cross-links after both exist.
- Medium tier (no PR): the issue is the standalone artifact.
- PR body carries a scaled `### IMPLEMENTATION PLAN` section (candidate-level, planner-authored),
  distinct from the manager issue and run report.

---

## §8 Issue & PR templates (Superset conventions, locked & versioned)

- Use SUPERSET's OWN conventions for BOTH issues and PRs (no invented high-level schema); keep
  the EM/PM register by writing non-technical prose WITHIN the conventional shapes.
- **PR** — conform to `.github/PULL_REQUEST_TEMPLATE.md` (SUMMARY, TESTING INSTRUCTIONS,
  ADDITIONAL INFORMATION, CHECKLIST), plus the added `### IMPLEMENTATION PLAN`, a `Tests`
  section, and (config-gated) `### AUTOMATION METADATA`. PR title must match the
  Conventional-Commits regex enforced by `pr-lint.yml`.
- **Issue templates mapped PER LANE:**
  - flaky-test / defect lanes → `bug-report.yml` shape.
  - public-API deprecation removals → `sip.md` shape.
  - SECURITY lane (CodeQL) → CANNOT use `bug-report.yml` (it explicitly bans GitHub issues for
    security problems). Use `security_issue_mode = generic_tracking`: a generic
    remediation-tracking issue with NO vulnerability/exploit detail.
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

**Test-inclusion policy** — a red→green test is REQUIRED for behavioral lanes (CodeQL,
deprecation) and self-satisfied for the skipped-test lane (re-enablement IS the test). A
genuinely-broken (not merely flaky) skipped test whose re-enablement requires editing the test →
`needs-human-review`.

**Structural invariants (NOT configurable)** — reviewer≠implementer separation, red-baseline
requirement, no-auto-merge-without-green-CI, no third adjudicating agent, criterion-mapped
tests. This runtime flow has NO plan-review step.

**Additional safeguards against reviewer-as-sole-test-author risk** — each reviewer test maps to
a planner acceptance criterion (unmapped → escalate); a fix+test that breaks an existing suite
test blocks auto-merge regardless of the reviewer's own test being green.

---

## §10 CI gate stack (Superset sign-off gates the generated PRs must pass)

Beyond unit tests, auto-merge eligibility requires the full stack green (verify via
`pre-commit run --all-files` and CI):

- Formatting: `ruff format`.
- Lint: `ruff check`, plus `pylint` with Superset's custom plugins.
- Typing: `mypy`.
- Frontend (if touched): `tsc` / eslint per `superset-frontend`.
- License headers: `license-check`.
- PR title: Conventional-Commits via `pr-lint.yml`.
- DCO sign-off (`git commit --signoff`).
- Respect `.github/CODEOWNERS`.

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
  Sheet optional via `kpi_sink`; precedent: `.github/workflows/tech-debt.yml` pushes lint-stats
  to a Sheet). KPIs: task-state counts (active/completed), PR merge rate (merged-clean vs edited
  vs rejected), verification pass rate, backlog burn-down (vs Phase 0c baseline), test-inclusion
  rate, criterion-coverage rate, expected-reason match rate on red baselines,
  `disagreement_unresolved` rate, sessions-per-candidate by role, implementer-test-edit
  violation rate (~0 expected).
- **ALERTING** (visually distinct in the rollup): merge rate < `merge_rate_floor = 0.50` → flag;
  session-failure rate > `session_failure_ceiling = 0.30` → flag.
- Merge-rate / burn-down are lagging and only meaningful after several runs.

---

## §12 Session management

For each dispatched candidate, call the Devin API (per Phase 0 findings) to create the role
sessions with scoped prompts (file paths, alert/test/deprecation context, fix + verify
instructions, emit BOTH artifacts using the templates). Poll status; record state transitions +
timing into the Layer 1 event log; collect PR + issue URLs.

---

## §13 Config knobs (tunable; locked values = shipped DEFAULTS, not hardcoded)

All live on one config surface (env vars / config file), changeable without editing logic. The
README ships a config reference table with default, allowed values, and safety classification.

| Knob | Default |
|---|---|
| `iteration_cap` | `5` |
| `coverage_bar` | `0.80` (test coverage on REPO A's pure-logic modules) |
| `budget_N` | `10` |
| `merge_rate_floor` | `0.50` |
| `session_failure_ceiling` | `0.30` |
| `kpi_sink` | `local` (`local` \| `gsheet`) |
| `major_only_requires_human` | `true` |
| `security_issue_mode` | `generic_tracking` |
| `mode` | `simulate` \| `live` |

Also configurable: target `owner/repo`, GitHub token, Devin API key, factor rubrics, artifact
templates.

- **Safety-relevant knobs** (marked in README): `major_only_requires_human`, `budget_N` —
  loosening weakens guardrails; change deliberately.
- Structural invariants (§9) are NOT knobs.

---

## §14 Guardrails / constraints

- SIMULATE mode makes NO writes (no PRs/issues/merges); only `live` writes.
- No secrets in the Docker image; credentials injected at runtime.
- Idempotency/dedupe: re-runs must not open duplicate PRs/issues for the same candidate (stable
  idempotency key ties artifacts + sessions).
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
- No issue/PR opened unless its body validates against the selected Superset-conformant template
  (section presence + order) and, for PRs, the title matches the Conventional-Commits regex.

---

## §15 REPO A layout

- `docs/IMPLEMENTATION_PLAN.md` (this document — version-controlled source of truth; referenced
  from README; what the build-time plan-review step reviews against).
- pipeline modules (lanes, gate, score, dispatch, session client, observability), `templates/`,
  `tests/`, `reports/`, `Dockerfile`, `docker-compose.yml`, `README.md`, `RESULTS.md`.

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
- a non-converging loop hits `needs-human-review` after 5 iterations (not 3).
- KPI rollup raises merge-rate alert when < 0.50 and session-failure alert when > 0.30.
- security-lane issue never exposes vulnerability detail; issues/PRs validate against locked
  templates.
- pure-logic module coverage ≥ `coverage_bar = 0.80`.
- a non-default knob (e.g. `budget_N` changed) takes effect in SIMULATE without code edits.

**Integration / smoke**

- full SIMULATE flow (fixture → gate → score → dispatch → artifacts → reports) makes no writes.
- Docker smoke: `docker compose up` runs SIMULATE end-to-end.
- optional credentials-gated live smoke (mocked by default).

---

## §18 Assumptions, dependencies, non-goals

- **Assumptions / dependencies** — Devin API availability + key; GitHub token with the §3 scopes;
  the fork has CodeQL enabled and a skipped-test backlog; Python toolchain
  (ruff/pylint/mypy/pytest) matching Superset's pinned versions.
- **Non-goals** (prevent scope creep) — no frontend lint-debt lane; no auto-merge of high-risk;
  no real-time dashboard (batch KPI rollup only); no adjudicating third agent; runtime flow has
  no plan-review step.
- **Failure & recovery** — define resume/cleanup for mid-run crash, stuck/timed-out session,
  partially-created artifacts (issue created but PR failed) — tied together by the idempotency
  key.

---

## §19 Success criteria / done when

- **BUILD-TIME** — plan-review gate ran before build; REPO A built via concurrent
  implementer-code / reviewer-tests split, pushed only after the loop converged with green CI;
  runtime vs build-time SDLC documented as separate in README.
- **RUNTIME (SIMULATE-demonstrable)** — three distinct role session IDs, NO plan-review step;
  reviewer test concurrent + red baseline recorded + red→green join; loop converges or cap →
  `needs-human-review` after 5 iterations; `major`-only → human; >10 → 10 dispatched + deferred;
  alerts on seeded bad metrics; KPI rollup at `reports/kpis.md`.
- **CONTRIBUTION COMPLIANCE** — a sample generated PR passes `pr-lint.yml`, `license-check`,
  `pre-commit` (ruff/pylint/mypy), license headers, DCO.
- **CONFIG** — every knob settable via the config surface without code edits; README config
  table lists default, allowed values, safety classification, and the non-configurable
  invariants; `docs/IMPLEMENTATION_PLAN.md` is under version control and referenced from the
  README.
- **REPO B (Superset fork)** — ≥1 real, verified remediation per first lane (a CodeQL-alert fix
  and a re-enabled skipped/flaky test) as cross-linked PR + convention-compliant companion issue
  with the full CI gate stack green; `RESULTS.md` lists selected issues, gate/score rationale,
  and links.

---

## Open questions / decisions needing a human

EMPTY — all decisions are locked.
