# Devin remediation pipeline

This program finds long-standing problems in a fork of
[Apache Superset](https://github.com/apache/superset) and gets Devin to fix them, one problem
per Devin session. It looks for three kinds of problem:

* **Security alerts** that GitHub's CodeQL scanner reports on the repository.
* **Disabled tests** — tests switched off permanently with `@pytest.mark.skip`, which no longer
  protect anything.
* **Deprecated code** — calls to APIs the project already announced it would remove, where the
  announced removal version has passed.

For each problem it finds, the program:

1. Decides whether the problem is safe to fix automatically at all, and drops it if not.
2. Scores what is left, so the most valuable problems are handled first.
3. Opens a tracking issue for it on the fork.
4. For the highest-scoring problems, starts a Devin session with a single, checkable success
   condition — for example, "this specific test passes without being skipped".
5. Checks that condition itself, against the code Devin actually pushed, rather than believing
   the session's own report.
6. Opens a pull request and waits for the fork's own CI to pass on it.
7. Stops there. **The program never merges anything**; a human reviews and merges.

Every run writes down what it did, so a run that is interrupted can be started again without
creating a second issue, branch, session, or pull request for the same problem.

The detailed requirements and design are in
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md); this README covers running the
program and reading its output.

## Two ways to run it: SIMULATE and LIVE

**SIMULATE** is the default and needs no credentials. It reads code and recorded data from your
own machine, writes nothing to GitHub, and creates no Devin sessions. It still produces the full
set of reports, so you can see which problems were found, how they were scored, and what would
have been done about them. Use it to try the program out, to check a configuration change, or in
CI.

**LIVE** needs a GitHub token and a Devin API key, and really does the work: it creates issues,
branches, Devin sessions, and pull requests on the fork. Use it when you want the fixes made.

Finding and scoring problems works identically in both; the only difference is whether anything
is created outside your machine.

## Words used in this README

* **Candidate** — one problem the program found, tracked individually from discovery to a merged
  fix.
* **Lane** — one of the three kinds of problem above. Their names in commands and reports are
  `codeql`, `skipped_tests`, and `deprecations`.
* **Run** — one invocation of the program. Each run gets its own identifier and writes its
  reports to its own directory.
* **Target checkout** — a local clone of the fork. The program reads its source code to find
  disabled tests and deprecated calls.
* **Baseline** ([`fixtures/baseline.json`](fixtures/baseline.json)) — stored offline input for
  SIMULATE and fallback operation: an inventory of every problem present in the fork at one fixed
  commit, recorded before any of this program existed (see
  [the discovery report](docs/PHASE0_DISCOVERY.md)).

## Prerequisites

Python 3.11+, git, Docker with the Compose v2 plugin (`docker compose`), and a POSIX shell.
On Windows, use WSL2: the commands below use POSIX syntax (`VAR=value cmd`, `export`,
`.venv/bin/...`, `id -u`).

## Setup

Start by cloning this repository. Run every command below from its root directory. Clone the
fork separately; that is the source tree this program reads.

On some systems Python 3.11 is installed as `python3` rather than `python3.11`.

```bash
git clone https://github.com/victorciao/devin-remediation-pipeline.git remediation-pipeline
cd remediation-pipeline
export SUPERSET_CHECKOUT="$HOME/src/superset"
git clone https://github.com/victorciao/superset.git "$SUPERSET_CHECKOUT"
python3.11 -m venv .venv  # or: python3 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
```

## Running in SIMULATE

This is the whole program, end to end, on your machine:

```bash
.venv/bin/python -m pipeline \
  --repo-path "$SUPERSET_CHECKOUT" \
  --output-dir ./run-output \
  --baseline fixtures/baseline.json
```

It writes its reports under the output directory (see
[Output files](#output-files)) and leaves the target checkout untouched.

If you skip the clone and leave out `--repo-path`, the program still runs, but it cannot read any
source code: instead of finding disabled tests and deprecated calls itself, it lists the ones
recorded in the baseline. The reports say so. Anything that changed in the fork since the
baseline commit will be missing, so clone the fork if you want a true picture.

The baseline records the problems found before this program ran. SIMULATE uses those records when
the target checkout is unavailable.

Settings can come from a configuration file, from `PIPELINE_*` environment variables, or from
command-line options, in that order of increasing precedence — so a command-line option always
wins. For example, to raise how many problems get a Devin session in one run:

```bash
.venv/bin/python -m pipeline \
  --mode=simulate --budget-N 5 \
  --repo-path "$SUPERSET_CHECKOUT" \
  --output-dir ./run-output \
  --baseline fixtures/baseline.json
```

The program exits with a non-zero status if the configuration is invalid, if something it needs
is unavailable, or if a run hits one of its session or cost limits.

## Running with Docker Compose

Docker Compose runs the same SIMULATE command inside a container. Run it from this repository's
root directory. The image build needs network access; the container run has no network access.
You need a Docker daemon and permission to use it. The `id -u` and `id -g` prefixes are for Linux;
Docker Desktop users do not need them. The results are written to `./docker-output`, relative to
the repository root.

```bash
mkdir -p docker-output
PIPELINE_UID="$(id -u)" PIPELINE_GID="$(id -g)" \
  SUPERSET_CHECKOUT="$SUPERSET_CHECKOUT" docker compose run --rm remediation
```

The target checkout is mounted read-only. If `SUPERSET_CHECKOUT` is unset, Compose stops with a
message asking you to set it.

## Running in LIVE

LIVE is deliberately hard to start by accident. Credentials are read from the environment only —
never from a configuration file, a Docker build argument, an image layer, a source file, or a log.

```bash
export DEVIN_API_KEY='...'
export GITHUB_PAT_REMEDIATION='...'
```

LIVE requires:

* explicit `--mode=live`;
* both credentials;
* a target checkout;
* a `--head-branch` branch-name prefix; and
* at least one completed `pull_request` Actions run in the target repository.

The program creates one branch per problem, named
`<head-branch>/<candidate-id>`. The hosted workflow uses `devin/remediation` as the prefix.

Before candidate work begins, LIVE also checks that the target token can:

* access the repository;
* read and publish issues and pull requests;
* read Actions history;
* read Code Scanning data; and
* identify the authenticated account.

If a required check is unavailable, LIVE stops instead of treating that lane as empty. The
optional `session_snapshot_id` setting selects a Devin snapshot prepared for the target repository;
it is never hard-coded. The pipeline never merges pull requests.

For a local LIVE run, restore prior history before starting the pipeline:

```bash
.venv/bin/python -m pipeline.history seed \
  --history-dir history \
  --out ./live-output/state/candidates-live.jsonl
```

This lets the run adopt existing issues instead of filing duplicates. Skipping it can make a local
LIVE run file issues that already exist.

After obtaining explicit approval for a target run, run:

```bash
.venv/bin/python -m pipeline \
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

Set a value in a YAML file passed with `--config <path>`, an environment variable named
`PIPELINE_` followed by the upper-cased setting name, or a command-line option using dashes.
These are applied in that order, so command-line options take precedence. For example:

```bash
PIPELINE_MAX_SESSIONS=2 .venv/bin/python -m pipeline \
  --config config/example.yaml --max-sessions 4
```

Credentials are environment-only: use `GITHUB_PAT_REMEDIATION` and `DEVIN_API_KEY`. Naming either
credential in the configuration file is rejected. An unrecognised `PIPELINE_*` variable aborts
startup. Some settings are available only through particular routes; derived rows below are
computed outputs and cannot be set.

| Name | What it controls | Default | Range / allowed values | Safety behavior |
|---|---|---:|---|---|
| `mode` | Selects SIMULATE or LIVE execution | `simulate` | `simulate`, `live` | `live` is explicit and credential-gated; unset values default to simulate |
| `budget_N` | Caps high-tier Devin session dispatches per run, highest score first | `5` | `1..25` (`BUDGET_HARD_MAX=25`) | High-tier overflow is deferred; medium-tier problems can still receive tracking issues |
| `score_cap` | Sets the maximum calculated candidate score | `200` | `>0` | — |
| `tier_high_min` | Sets the score cutoff for high-tier routing | `60` | `> tier_medium_min` | Rejected at startup unless greater than `tier_medium_min` |
| `tier_medium_min` | Sets the score cutoff for medium-tier routing | `20` | `>0` | Rejected at startup unless below `tier_high_min` |
| `eol_major_lag` | Sets the major-version lag required to classify EOL | `2` | `>=1` | — |
| `merge_rate_floor` | Sets the merge-rate KPI alert floor | `0.50` | `0.0..1.0` | Alerts when observed merge rate falls below the floor |
| `verification_pass_rate_floor` | Sets the verification pass-rate KPI alert floor | `0.80` | `0.0..1.0` | Alerts when observed pass rate falls below the floor |
| `session_failure_ceiling` | Sets the session-failure KPI alert ceiling | `0.30` | `0.0..1.0` | Alerts when observed failure rate exceeds the ceiling |
| `verification_pass_rate_alert` | Reports the derived verification-rate status | derived | `0` or `1` | `1` when pass rate is below its configured floor; otherwise `0` |
| `publication_safety_alert` | Reports the derived publication-safety status | derived | `0` or `1` | `1` when publication safety is undetermined; otherwise `0` |
| `max_sessions` | Sets the per-run Devin session limit | `8` | `>=1` | The run aborts if the ceiling is exceeded |
| `session_timeout_s` | Sets the maximum duration of one Devin session | `5400.0` | `>0` | A timed-out session is recorded as a failure |
| `max_total_acu` | Sets the per-run Devin ACU limit | `500.0` | `>0` | The run aborts if cumulative ACU exceeds the ceiling |
| `alert_source` | Selects CodeQL API or SARIF alert input | `code_scanning_api` | `code_scanning_api`, `sarif_file` | LIVE requires the latest CodeQL analysis on `base_sha`; SARIF is the SIMULATE input |
| `alert_fixture_path` | Locates the captured CodeQL/SARIF input | `fixtures/codeql_alerts.json` | Path | — |
| `alert_analysis_wait_s` | Sets how long to wait for CodeQL analysis | `2700.0` | `>0` | Expiry leaves alert evidence unavailable |
| `ci_evidence_mode` | Selects local or Actions criterion evidence | `local` | `actions`, `local` | — |
| `suite_check_context` | Names the suite check used for evidence | `unit-tests-required` | Non-empty string | — |
| `ci_wait_timeout_s` | Sets how long to wait for GitHub evidence | `5400` | `>0` | Expiry leaves CI evidence unavailable |
| `required_contexts_min` | Lists the required completed contexts | `pre-commit (current)` | Non-empty context names | LIVE stops if the list is empty or required contexts do not pass |
| `only_lanes` | Restricts high-tier dispatch to selected lanes | `()` | Comma-separated `codeql`, `skipped_tests`, `deprecations` | Only selected high-tier lanes can dispatch; other lanes remain reported |
| `has_issues` | Records whether issue publication is available | `true` | `true`, `false` | When issues are disabled on the fork, the run defers rather than writing |
| `marker_search_enabled` | Enables marker reconciliation searches | `true` | `true`, `false` | No marker lookup is performed when false |
| `version_source` | Locates the repository's release declaration | `.github/ISSUE_TEMPLATE/bug-report.yml` | Repo-relative path | Startup fails if no concrete release is found |
| `lane2_class_breadth_max` | Sets the maximum skipped-test class breadth | `5` | `>=1` | Candidates wider than the ceiling fail automatability |
| `target_owner` | Selects the GitHub target owner | `victorciao` | Non-empty string | — |
| `target_repo` | Selects the GitHub target repository | `superset` | Non-empty string | — |
| `rubrics_path` | Locates observable rubric tables | `config/rubrics.yaml` | Path | — |
| `templates_dir` | Locates vendored issue and PR templates | `templates` | Path | — |
| `session_snapshot_id` | Selects the Devin target-repository snapshot | unset | Optional string | — |

`only_lanes` accepts a comma-separated lane list from `--only-lanes=...` or
`PIPELINE_ONLY_LANES`; it restricts high-tier dispatch eligibility, while other lanes remain
represented in the run report.
Security tracking issues do not describe the vulnerability or how to exploit it. They contain
only a summary, affected scope, status, verification, and rule ID. The system checks the stated
success condition and CI evidence before handing a pull request to a human for review.

## Output files

After a run, look in the output directory for:

* `state/candidates.jsonl` — append-only state; the newest row for a problem is its current state,
  and this lets a re-run resume instead of duplicating work.
* `reports/events.jsonl` — the per-step audit trail, including session IDs and final outcomes.
* `reports/run-<run_id>.md` — an account of every problem the run saw and why it was or was not
  acted on, with artifact links.
* `reports/kpis.md` — pass and failure rates plus the four threshold alerts.
* `reports/issues/<candidate_id>.md` — the tracking-issue body; in SIMULATE, this is exactly the
  text a LIVE run would publish.
* `reports/prs/<candidate_id>.md` — the pull-request body; in SIMULATE, this is exactly the text a
  LIVE run would publish.

Run artifacts are uploaded under `remediation-<run_id>`. A successful publication also commits
the run directory under `history/` and refreshes `RESULTS.md`; publication failures are
annotated without failing remediation. The `remediation-pipeline` concurrency group queues a
new trigger while another run is active instead of racing it.

## How do I know whether it worked?

Exit code 0 means the run completed. Exit code 1 means it aborted; the reason is printed to
standard error. The final `summary:` line reports the mode, number of problems, scores,
dispatches, and deferrals. Read `reports/run-<run_id>.md` to see what happened to each problem
and why it was or was not acted on. Read `reports/kpis.md` for pass and failure rates and the four
threshold alerts.

In LIVE, completion does not mean that a pull request was merged. The program never merges;
the end state is `awaiting_human_merge`, and human review and acceptance of the pull request are
what success means.

## Automated triggers and secrets

### CodeQL repository dispatch

In the fork, `.github/workflows/remediation-dispatch.yml` runs after a successful CodeQL
`workflow_run` on `master`. It sends a POST request with
`event_type: codeql-scan-completed` to this repository's `dispatches` endpoint. Give the fork the
`REMEDIATION_DISPATCH_TOKEN` secret with permission to send that request. Give this repository
the `DEVIN_API_KEY` and `REMEDIATION_GITHUB_PAT` secrets. GitHub rejects secret names beginning
with `GITHUB_`, so the workflow maps `REMEDIATION_GITHUB_PAT` to the
`GITHUB_PAT_REMEDIATION` environment variable.

### Manual workflow dispatch

In this repository's Actions UI, select **Remediation**, choose **Run workflow**, and set any of
these inputs:

* `only_lanes`
* `budget_n` (default `1`)
* `max_sessions` (default `1`)
* `tier_high_min`
* `max_total_acu`

The equivalent command is:

```bash
gh workflow run remediation.yml --repo victorciao/devin-remediation-pipeline --ref main \
  -f only_lanes= -f budget_n=1 -f max_sessions=1 -f tier_high_min= -f max_total_acu=
```

### Weekly schedule

The workflow runs every Monday at 03:17 UTC (`17 3 * * 1`).

The run uploads an artifact named `remediation-<run_id>`. It also publishes the run directory
under `history/` and refreshes `RESULTS.md` when publication succeeds. The resulting reports and
history are the hosted evidence for the run.
