# Devin remediation pipeline

This repository contains an event-driven remediation pipeline for Apache Superset. It checks
three independently auditable lanes—CodeQL alerts, unconditional skipped tests, and end-of-life
deprecations—then applies consistent safety, dispatch, review, artifact, and reporting rules.

The target checkout is Apache Superset at the revision captured by `fixtures/baseline.json`.
SIMULATE does not modify that checkout.

The source of truth for behavior and requirements is
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md). This README is an operator
guide, not a replacement for that plan.

## Prerequisites

Use Python 3.11+, git, Docker with the Compose v2 plugin (`docker compose`), and a POSIX
shell. Windows users should use WSL2: these commands use POSIX `VAR=value cmd`, `export`,
`.venv/bin/...`, and `id -u` syntax.

## Setup

Python 3.11 may be named `python3` on some systems. Set the target checkout once, pinned to
the `head_sha` in `fixtures/baseline.json`, then install the package and development tools:

```bash
export SUPERSET_CHECKOUT="$HOME/src/superset"
git clone https://github.com/victorciao/superset.git "$SUPERSET_CHECKOUT"
git -C "$SUPERSET_CHECKOUT" checkout a140e74f5f54b2ada25e7558d884812facd3375d
python3.11 -m venv .venv  # or: python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
```

The package has no required network service for SIMULATE. The checked-in CodeQL fixture and
the fixed snapshot of the fork's CodeQL alerts, skipped tests, and EOL deprecations taken
before any pipeline code ran ([discovery](docs/PHASE0_DISCOVERY.md)) are used for the
credential-free path and its burn-down denominators.

## Credential-free SIMULATE

Every invocation defaults to SIMULATE. Run the complete local pipeline with:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --repo-path "$SUPERSET_CHECKOUT" \
  --output-dir . \
  --baseline fixtures/baseline.json \
  --alert-source sarif_file
```

If the target checkout is absent, the pipeline falls back to baseline records for the skipped-test
and deprecation lanes. The run still works, but it is degraded. SIMULATE makes no remote writes;
it produces the local artifacts a LIVE run would publish, labels session counts as simulated, and
keeps verification and publication-safety alerts visible.

Configuration precedence is configuration file, then `PIPELINE_*` environment variables,
then command-line options. For example:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --mode=simulate --budget-N=5 --output-dir ./run-output
```

Each run receives a fresh identifier. The command returns a non-zero exit code for configuration
errors, blocking capability preconditions, or hard session and cost ceilings.

## LIVE

LIVE is deliberately guarded. Supply runtime credentials through the environment only. They are
never accepted from a configuration file, Docker build argument, image layer, source file, or log.

```bash
export DEVIN_API_KEY='...'
export GITHUB_PAT_REMEDIATION='...'
```

The configuration loader also accepts `PIPELINE_DEVIN_API_KEY` and `PIPELINE_GITHUB_TOKEN`.
LIVE requires:

* explicit `--mode=live`;
* both credentials;
* a target checkout;
* a `--head-branch` value; and
* at least one completed `pull_request` Actions run in the target repository.

Before candidate work begins, LIVE also checks that the target token can:

* access the repository;
* read and publish issues and pull requests;
* read Actions history;
* read Code Scanning data; and
* identify the authenticated account.

If a required check is unavailable, LIVE stops instead of treating that lane as empty. The
optional `session_snapshot_id` setting selects a Devin snapshot prepared for the target repository;
it is never hard-coded. The pipeline never merges pull requests.

After obtaining explicit approval for a target run, provide the Devin-created branch and run:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --mode=live --head-branch devin/remediation \
  --repo-path "$SUPERSET_CHECKOUT" \
  --output-dir ./live-output \
  --baseline fixtures/baseline.json
```

The command checks the LIVE requirements before candidate work, then runs sessions and ordered
publication only when those requirements pass. A missing credential, unavailable capability or
service, missing `--head-branch`, or hard runtime ceiling causes a non-zero abort before the
relevant work.

## Configuration reference

| Name | What it controls | Default | Range / allowed values | Safety behavior |
|---|---|---:|---|---|
| `mode` | Selects SIMULATE or LIVE execution | `simulate` | `simulate`, `live` | `live` is explicit and credential-gated; unset values default to simulate |
| `budget_N` | Caps high-tier Devin dispatches per run, highest score first | `5` | `1..25` (`BUDGET_HARD_MAX=25`) | Dispatch overflow is deferred; values above 25 are clamped and recorded as `guardrail_clamped` |
| `score_cap` | Caps each calculated candidate score | `200` | `>0` | Caps calculated scores |
| `tier_high_min` | Sets the score cutoff for high-tier routing | `60` | `> tier_medium_min` | High-tier PR routing threshold |
| `tier_medium_min` | Sets the score cutoff for medium-tier routing | `20` | `>0` | Medium-tier issue routing threshold |
| `eol_major_lag` | Sets the major-version lag required to classify EOL | `2` | `>=1` | Major-version age required for EOL |
| `merge_rate_floor` | Sets the merge-rate KPI alert floor | `0.50` | `0.0..1.0` | KPI alert threshold |
| `verification_pass_rate_floor` | Sets the verification pass-rate KPI alert floor | `0.80` | `0.0..1.0` | KPI alert threshold |
| `session_failure_ceiling` | Sets the session-failure KPI alert ceiling | `0.30` | `0.0..1.0` | KPI alert threshold and run safety signal |
| `verification_pass_rate_alert` | Reports the derived verification-rate alert | derived | `0` or `1` | Alerts when verification pass rate is below its floor |
| `publication_safety_alert` | Reports the derived publication-safety alert | derived | `0` or `1` | Alerts when publication safety is undetermined |
| `max_sessions` | Caps Devin sessions created in one run | `8` | `>=1` | Per-run hard session ceiling; exceeding it aborts |
| `session_timeout_s` | Bounds the duration of one Devin session | `5400.0` | `>0` | Bounds one Devin session |
| `max_total_acu` | Caps cumulative Devin ACU in one run | `500.0` | `>0` | Per-run hard ACU ceiling; exceeding it aborts |
| `alert_source` | Selects CodeQL API or SARIF alert input | `code_scanning_api` | `code_scanning_api`, `sarif_file` | LANE 1 reads the fork's alerts for `master` and requires the latest CodeQL analysis to sit on `base_sha`; `sarif_file` is the SIMULATE input |
| `alert_fixture_path` | Locates the captured CodeQL/SARIF input | `fixtures/codeql_alerts.json` | Path | Captured CodeQL/SARIF input |
| `alert_analysis_wait_s` | Bounds CodeQL analysis polling | `2700.0` | `>0` | Bounds CodeQL analysis polling |
| `ci_evidence_mode` | Selects local or Actions criterion evidence | `local` | `actions`, `local` | LIVE may resolve this from Actions history; evidence never authorizes an automated merge |
| `suite_check_context` | Names the suite check used for evidence | `unit-tests-required` | Non-empty string | Named Actions check context used for suite evidence |
| `ci_wait_timeout_s` | Bounds GitHub evidence polling | `5400` | `>0` | Bounds GitHub evidence waiting |
| `required_contexts_min` | Specifies required completed contexts | `pre-commit (current)` | Non-empty context names | Required completed Actions context for LIVE preflight |
| `only_lanes` | Restricts high-tier dispatch to selected lanes | `()` | Comma-separated `codeql`, `skipped_tests`, `deprecations` | Restricts dispatch eligibility without changing candidate discovery or reporting |
| `has_issues` | Records whether issue publication is available | `true` | `true`, `false` | When issues are disabled on the fork, the run defers rather than writing |
| `marker_search_enabled` | Enables marker reconciliation searches | `true` | `true`, `false` | Enables durable marker reconciliation |
| `version_source` | Locates the repository's release declaration | `.github/ISSUE_TEMPLATE/bug-report.yml` | Repo-relative path | No concrete release is a startup error |
| `lane2_class_breadth_max` | Caps skipped-test class breadth | `5` | `>=1` | Wider skipped classes fail automatability |
| `target_owner` | Selects the GitHub target owner | `victorciao` | Non-empty string | GitHub target owner |
| `target_repo` | Selects the GitHub target repository | `superset` | Non-empty string | GitHub target repository |
| `rubrics_path` | Locates observable rubric tables | `config/rubrics.yaml` | Path | Observable rubric tables |
| `templates_dir` | Locates vendored issue and PR templates | `templates` | Path | Vendored issue/PR templates |
| `session_snapshot_id` | Selects the Devin target-repository snapshot | unset | Optional string | Devin snapshot for target-repository role sessions |
| `github_token` | Supplies the GitHub API credential | unset | Runtime secret | Environment-only; required by LIVE |
| `devin_api_key` | Supplies the Devin API credential | unset | Runtime secret | Environment-only; required by LIVE |

`only_lanes` accepts a comma-separated lane list from `--only-lanes=...` or
`PIPELINE_ONLY_LANES`; it restricts dispatch eligibility only, so other lanes remain
enumerated, gated, scored, and represented in the run report.
Security issues are always detail-free. The system verifies the declared criterion and CI
evidence before handing a pull request to a human for merge.

## Docker and Compose smoke

The image uses Python 3.11, copies only the package and `config/`, `templates/`, and
`fixtures/`, and runs as a non-root user. Compose mounts `$SUPERSET_CHECKOUT` read-only at
`/target-repo`; the container uses that mounted path for `--repo-path`. Bind-mounted
`./docker-output` (relative to the repository root) must be writable by the container user:

```bash
mkdir -p docker-output
PIPELINE_UID="$(id -u)" PIPELINE_GID="$(id -g)" \
  SUPERSET_CHECKOUT="$SUPERSET_CHECKOUT" docker compose run --rm remediation
```

Compose uses `network_mode: none`, mounts `./docker-output` at `/output`, and mounts the
target checkout read-only. LIVE credentials, if ever used by an embedding deployment, are
runtime environment values and are not Docker build inputs.

## Observability and artifacts

The output directory contains:

* `state/candidates.jsonl` — lifecycle state used for resuming work and avoiding duplicates.
* `reports/events.jsonl` — the event log with gate, dispatch, session, review, artifact, and
  terminal evidence.
* `reports/run-<run_id>.md` — the per-run summary with candidate outcomes and artifact links.
* `reports/kpis.md` — the KPI rollup and threshold alerts.
* `reports/issues/<candidate_id>.md` — rendered issue bodies.
* `reports/prs/<candidate_id>.md` — rendered pull-request bodies.

The fixed snapshot of the fork's CodeQL alerts, skipped tests, and EOL deprecations taken before
any pipeline code ran is used for burn-down denominators. If a lane has no usable baseline data,
the report shows `n/a` rather than zero.

## Verification status

Verified in this workstream: package import, static checks, baseline reproduction against the
captured Superset revision (identical except `captured_at`), and a credential-free SIMULATE run
per lane in Docker, each reaching its designed outcome at the default threshold (`codeql`
81/high and `deprecations` 200/high run the full simulated loop; `skipped_tests` is medium and
therefore issue-only).

All three lanes have also completed LIVE against the Superset fork, with independent criterion
verification at the pull request head rather than trust in the session's own report:

* `codeql` — issue [#44](https://github.com/victorciao/superset/issues/44), pull request
  [#45](https://github.com/victorciao/superset/pull/45), merged.
* `skipped_tests` — issue [#4](https://github.com/victorciao/superset/issues/4), pull request
  [#42](https://github.com/victorciao/superset/pull/42).
* `deprecations` — issue [#30](https://github.com/victorciao/superset/issues/30), pull request
  [#46](https://github.com/victorciao/superset/pull/46).

The event-driven path is verified end to end on hosted runners: a merge to the fork's `master`
produced a CodeQL completion, the fork's dispatch workflow sent `codeql-scan-completed`, and the
resulting `repository_dispatch` run adopted 30 existing issues by marker without creating
duplicates, created one session, pushed one branch with the remediation pull request token,
opened a pull request, verified the criterion, observed authoritative required checks, and
settled at `awaiting_human_merge`.

Merging is never performed by this system. A verified candidate settles at
`awaiting_human_merge` for a human to merge; automated merging is not part of the system. A merge
performed by automation is therefore absent from the evidence on purpose.

## Automated triggers and secrets

### CodeQL repository dispatch

A push or merge to the fork's `master` completes CodeQL; its workflow must send
`codeql-scan-completed` as a `repository_dispatch` to this repository. The fork needs
`REMEDIATION_DISPATCH_TOKEN` with repository-dispatch rights here. This repository needs
`DEVIN_API_KEY` and `REMEDIATION_GITHUB_PAT` secrets (GitHub rejects secret names prefixed
`GITHUB_`, so the workflow maps that secret onto the `GITHUB_PAT_REMEDIATION` environment
variable the configuration reads).

### Manual workflow dispatch

In the Actions UI, select **Remediation**, choose **Run workflow**, and provide the optional
inputs `only_lanes` (empty or a comma-separated list of `codeql`, `skipped_tests`, and
`deprecations`), `budget_n` (default `1`), `max_sessions` (default `1`), `tier_high_min`
(empty for the configured default), and `max_total_acu` (empty for the configured default).
The equivalent command is:

```bash
gh workflow run remediation.yml --repo victorciao/devin-remediation-pipeline --ref main \
  -f only_lanes= -f budget_n=1 -f max_sessions=1 -f tier_high_min= -f max_total_acu=
```

### Weekly schedule

The workflow runs every Monday at 03:17 UTC (`17 3 * * 1`).

### Local CLI

For a local LIVE run, use the instructions in [LIVE](#live).

Run artifacts are uploaded under `remediation-<run_id>`. A successful publication also commits
the run directory under `history/` and refreshes `RESULTS.md`; publication failures are
annotated without failing remediation. The `remediation-pipeline` concurrency group queues a
new trigger while another run is active instead of racing it.
