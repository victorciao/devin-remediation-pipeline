# Phase 0 — Discovery Report (blocking gate per §3)

Date: 2026-08-28. Target REPO B: `victorciao/superset` (fork of `apache/superset`, default branch `master`, HEAD `a140e74`).

## 0a — Devin API (RESOLVED, built against confirmed shapes)

| Concern | Confirmed finding |
|---|---|
| Session create | `POST https://api.devin.ai/v1/sessions` |
| Auth | `Authorization: Bearer <key>`; Personal API Key (`apk_user_*`) or Service API Key (`apk_*`). v3 alternative: `POST https://api.devin.ai/v3/organizations/{org_id}/sessions` with `cog_*` service-user key + `create_as_user_id`. |
| Create payload | `{prompt (required), title, idempotent, tags[], knowledge_ids[], secret_ids[], session_secrets[], snapshot_id, playbook_id, max_acu_limit, structured_output_schema, unlisted}` |
| Create response | `{session_id, url, is_new_session}` |
| Poll | `GET /v1/sessions/{session_id}` → `{status, status_enum ∈ working\|blocked\|expired\|finished\|suspend_requested\|resume_requested\|resumed, structured_output, pull_request, messages[], created_at}` |
| Design consequence | `structured_output_schema` is how planner/implementer/reviewer sessions return machine-readable results (acceptance criteria, PR URL, review severity list) instead of prose scraping. `max_acu_limit` is the per-session cost ceiling required by §14. `idempotent` + `tags` back the §14 idempotency key. |

## 0b — GitHub auth & scopes (SHORTFALL — this is the review-pause trigger)

Session token identity: `devin-ai-integration[bot]` (GitHub App installation token), routed through Devin's git proxy.

| Capability needed (§3 0b) | Status | Evidence |
|---|---|---|
| Read repo metadata | OK | `GET /repos/victorciao/superset` → 200 |
| Git push to REPO B | OK | `git push --dry-run origin HEAD:refs/heads/devin/perm-probe` → `* [new branch]` (proxy-authenticated) |
| PR create on REPO B | OK (via builtin PR tooling over the same proxy) | — |
| `security_events` (read CodeQL alerts) | **MISSING** | `GET /repos/victorciao/superset/code-scanning/alerts` → **403 "Resource not accessible by integration"** |
| `issues` write (companion issues, §7) | **UNPROVEN / likely missing** | installation `permissions` block reports `admin/maintain/push/pull/triage = false`; `GET /user` → 403 |
| `actions` read (CI history for the flake lane) | **EMPTY** | `GET /actions/runs` → `total_count: 0`; `GET /actions/workflows` → `total_count: 0` — **Actions is disabled/never-run on the fork** |
| Create REPO A (new public repo) | **BLOCKED** | `gh repo create victorciao/devin-remediation-pipeline --public` → `GraphQL: Resource not accessible by integration (createRepository)` |
| Devin API key | **ABSENT** | no secrets provisioned in this session |

## 0c — Live enumeration & baseline

| Lane | Live signal on the fork | Verdict |
|---|---|---|
| LANE 1 — CodeQL | `.github/workflows/codeql-analysis.yml` exists and is the assumed anchor (`schedule: cron "0 4 * * *"`, `security-events: write`, matrix `python`/`javascript`). But **the workflow has never run on this fork** (0 Actions runs) and the alerts API is 403, so **the live alert count is currently unknowable and is very likely zero**. Forks do not inherit the upstream's code-scanning alerts. | **Scope shift.** §3's "no CodeQL alerts present" pause condition is met. |
| LANE 2 — skipped/flaky tests | **97** skip sites under `tests/` — a raw pre-scoping grep over *all* skip forms (unconditional, `skipif`/`skipUnless`, `xfail`), superseded by the §5 enumerator scope below.  Confirmed rich seam in the two named files: `tests/integration_tests/sqllab_tests.py` has 11 `@pytest.mark.skip(...)` decorators (lines 84, 136, 152, 206, 223, 236, 362, 557, 603, 626, 867); `tests/integration_tests/model_tests.py` has 11 `@unittest.skipUnless(...)` (env-conditional, not backlog). | **Healthy — viable demo lane today.** |
| LANE 3 — EOL `@deprecated` | 4 parseable sites: `superset/db_engine_specs/base.py:1542` `normalize_indexes` (`deprecated_in="3.0"`), `:2325` `get_url_for_impersonation` (`6.0.0`), `:2347` `update_impersonation_config` (`6.0.0`), `superset/databases/api.py:976` (`4.0`). Verifier path `tests/unit_tests/db_engine_specs/` exists. | **Healthy.** |
| Open issues | `GET /issues` returns empty — forks have Issues disabled by default. | **Blocks §7 companion issues until enabled.** |

Baseline snapshot for burn-down will be captured as `fixtures/baseline.json` (skip-site inventory + deprecation inventory + empty CodeQL set), and the SIMULATE fixture derived from it.

## Consequences for §5 lane priority

Lane 2 (skipped tests) and Lane 3 (EOL deprecations) are demonstrable against live data **now**. Lane 1 (CodeQL) is implementable and unit-testable against a SARIF fixture, but cannot produce live evidence on this fork until code scanning is enabled and the token can read `security_events`.

## What is needed to clear the pause

1. **REPO A must be created by a human or by a token with `repo` create scope** — the installation token cannot create repositories.
2. **A GitHub PAT** with `repo`, `security_events`, `workflow`, and issues write on `victorciao/superset`.
3. **Enable Issues + Actions + Code scanning on the fork** (Settings → General → Features → Issues; Settings → Actions → Allow all; Security → Code scanning → CodeQL default setup) so Lane 1 has live alerts and §7 companion issues can be opened.
4. **A Devin API key** (`apk_user_*` / `apk_*`, or a `cog_*` service key + org id) for §12 session orchestration.

Without 1–4 the pipeline is fully buildable and SIMULATE-demonstrable, but §19's REPO B evidence criterion cannot be satisfied.

## Post-remediation capability state (owner PAT, after items 1–4 were supplied)

All four blockers above were cleared during Phase 0. Read back with the owner token:

| Probe | Result |
|---|---|
| `GET /repos/victorciao/superset` | `has_issues: true` |
| `GET /actions/workflows` | `total_count: 49`, `allowed_actions: all` |
| `GET /actions/runs` | `total_count: 1` — `CodeQL Setup`, event `dynamic`, `success`. **No `pull_request` or `workflow_dispatch` run has ever completed**, so plan §3 0d resolves `ci_evidence_mode = local` |
| `GET /code-scanning/default-setup` | `state: configured`, `query_suite: default`, `schedule: weekly`, languages `javascript`, `javascript-typescript`, `python`, `typescript` |
| `GET /code-scanning/alerts` | `200`, **11 open alerts** → `fixtures/codeql_alerts.json` |

The `schedule: weekly` value is the authoritative cadence for the fork's CodeQL runs; the
upstream `codeql-analysis.yml` cron (`0 4 * * *`) does not govern default setup. Alert freshness
is taken from `alert.updated_at` regardless (plan §5).

The baseline was regenerated after these fixes (`scripts/build_baseline.py <repo> fixtures
fixtures/codeql_alerts.json`), so `fixtures/baseline.json` records
`baseline_valid_lanes: [codeql, skipped_tests, deprecations]`, `current_release: 6.1.0`, **35**
LANE 2 candidate decorator instances (the enumerator counts decorator instances; at this HEAD the
35 included rows are 35 distinct nodeids, while the 33 exclusions include multi-decorator nodes.
It is decorator-based, so `pytestmark` assignments, imperative in-body skips and indirect
`pytest.mark.*` aliases are outside its scope — see plan §5). Absolute-import bindings *are* resolved, so the
alias-imported `@skip("Flaky")` at `tests/integration_tests/databases/commands_tests.py:118`
counts. Each candidate carries the fully qualified, collectable pytest nodeid
(`path::Class::method` where the test is class-nested — 28 of the 35 are) plus its `class_scope`,
`enclosed_tests`, `parametrized`, `collects_single_item` and `enclosing_skip_nodeid`: 6 of the 35
locators collect more than one item (4 class-level skips, 2 parametrized functions) and 2 sit
inside another skipped class. Every record reports `line` as the definition line and
`decorator_line` as the matched decorator's line, in both LANE 2 and LANE 3.
It also records 33 excluded conditional instances split by reason — 30 `conditional_environment_guard` and
3 `expected_failure_xfail` — and 2 EOL-passed deprecations.
