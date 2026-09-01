# Devin remediation pipeline

This repository contains an event-driven remediation pipeline for Apache Superset. It
enumerates three independently auditable lanes—CodeQL alerts, unconditional skipped tests,
and end-of-life deprecations—then applies the shared gate, rubric, score, deterministic
dispatch, session, review, artifact, and observability contracts.

The implementation repository (REPO A) is this repository. The target checkout (REPO B) is
Apache Superset, normally `/home/ubuntu/repos/superset`, at the revision captured by
`fixtures/baseline.json`. REPO A does not modify REPO B during a SIMULATE run.

The source of truth for behavior and requirements is
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md). This README is an operator
guide, not a replacement for that plan.

## Setup

Python 3.11 or newer is required. Install the package and development tools in a virtual
environment. Editable installation is unavailable in this checkout; use `PYTHONPATH=src`
when invoking the source tree:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install '.[dev]'
```

The package has no required network service for SIMULATE. The checked-in CodeQL fixture and
Phase 0c baseline are used for the credential-free path.

## Credential-free SIMULATE

Every invocation defaults to SIMULATE. Run the complete local pipeline with:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --repo-path /home/ubuntu/repos/superset \
  --output-dir . \
  --baseline fixtures/baseline.json \
  --alert-source sarif_file
```

If the target checkout is absent, the entrypoint uses the baseline records for the skipped
test and deprecation lanes and still runs from the checked-in fixtures. SIMULATE creates no
remote writes: the GitHub transport seam rejects mutation before a transport method can be
called. It renders the artifacts that a live run would publish. SIMULATE labels session counts
as `(simulated)` and prefixes alert lines with `SIMULATED`; verification and publication-safety
alerts remain visible.

Configuration precedence is configuration file, then `PIPELINE_*` environment variables,
then command-line options. For example:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --mode=simulate --budget-N=5 --output-dir ./run-output
```

The CLI generates a fresh `run_id` for each run and returns a non-zero exit code for
configuration errors, blocking capability preconditions, or hard session/cost ceilings.

## LIVE

LIVE is deliberately guarded. A constrained LIVE run against `victorciao/superset` created
30 tracking issues, one remediation branch, and one Devin session; it settled its candidate
without creating a pull request or merging anything. Runtime credentials must be supplied through the
environment only; they are never accepted from a configuration file, Docker build argument,
image layer, source file, or log:

```bash
export DEVIN_API_KEY='...'
export GITHUB_PAT_REMEDIATION='...'
```

The configuration loader also accepts the explicit `PIPELINE_DEVIN_API_KEY` and
`PIPELINE_GITHUB_TOKEN` names. Credentials are required together with `--mode=live`.
The target token must have the repository, issues/PR, Actions, and Code Scanning capabilities
required by §3 0d. Issues, Actions history, Code Scanning, and token identity are blocking
capability probes; an unmet probe is recorded as `capability_unavailable` or
`token_capability_missing`, rather than becoming an empty lane. Actions history resolves
`ci_evidence_mode`; the pipeline never merges pull requests.
The optional `session_snapshot_id` setting pins role sessions to a Devin snapshot
prepared for the target repository; it is never hard-coded.

Before LIVE can resolve `ci_evidence_mode`, the target repository must have at least one
completed `pull_request` Actions run. A freshly created fork does not satisfy this
precondition until its first pull request exists; otherwise LIVE fails closed with
`ci_evidence_unavailable`.

After obtaining explicit approval for a target run, provide the Devin-created branch and run:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --mode=live --head-branch devin/remediation \
  --repo-path /home/ubuntu/repos/superset \
  --output-dir ./live-output \
  --baseline fixtures/baseline.json
```

The command-line entrypoint constructs guarded stdlib HTTP transports for LIVE, performs a
read-only GitHub capability preflight before candidate work, and then runs Devin sessions and
ordered GitHub publication only when the preconditions pass. A missing credential, unreadable
capability, unavailable required service, missing `--head-branch`, or hard runtime ceiling
causes a non-zero abort before the relevant work. The constrained run demonstrated capability
preflight, issue and branch publication, Devin session execution, and candidate settlement;
it did not demonstrate PR creation, merge behavior, or an independently verified remediation.

## Configuration reference (§13)

| Name | Default | Range / allowed values | Safety behavior |
|---|---:|---|---|
| `mode` | `simulate` | `simulate`, `live` | `live` is explicit and credential-gated; unset values default to simulate |
| `coverage_bar` | `0.80` | `0.0..1.0` | Coverage threshold used by review policy |
| `budget_N` | `5` | `1..25` (`BUDGET_HARD_MAX=25`) | Dispatch overflow is deferred; values above 25 are clamped and recorded as `guardrail_clamped` |
| `score_cap` | `200` | `>0` | Caps calculated scores |
| `tier_high_min` | `60` | `> tier_medium_min` | High-tier PR routing threshold |
| `tier_medium_min` | `20` | `>0` | Medium-tier issue routing threshold |
| `eol_major_lag` | `2` | `>=1` | Major-version age required for EOL |
| `merge_rate_floor` | `0.50` | `0.0..1.0` | KPI alert threshold |
| `verification_pass_rate_floor` | `0.80` | `0.0..1.0` | KPI alert threshold |
| `session_failure_ceiling` | `0.30` | `0.0..1.0` | KPI alert threshold and run safety signal |
| `verification_pass_rate_alert` | derived | `0` or `1` | Alerts when verification pass rate is below its floor |
| `publication_safety_alert` | derived | `0` or `1` | Alerts when publication safety is undetermined |
| `max_sessions` | `8` | `>=1` | Per-run hard session ceiling; exceeding it aborts |
| `session_timeout_s` | `5400.0` | `>0` | Bounds one Devin session |
| `max_total_acu` | `500.0` | `>0` | Per-run hard ACU ceiling; exceeding it aborts |
| `alert_source` | `code_scanning_api` | `code_scanning_api`, `sarif_file` | LANE 1 reads the fork's alerts for `master` and requires the latest CodeQL analysis to sit on `base_sha`; `sarif_file` is the SIMULATE input |
| `alert_fixture_path` | `fixtures/codeql_alerts.json` | Path | Captured CodeQL/SARIF input |
| `alert_analysis_wait_s` | `2700.0` | `>0` | Bounds CodeQL analysis polling |
| `ci_evidence_mode` | `local` | `actions`, `local` | LIVE may resolve this from Actions history; evidence never authorizes an automated merge |
| `suite_check_context` | `unit-tests-required` | Non-empty string | Named Actions check context used for suite evidence |
| `ci_wait_timeout_s` | `5400` | `>0` | Bounds GitHub evidence waiting |
| `required_contexts_min` | `pre-commit (current)` | Non-empty context names | Required completed Actions context for LIVE preflight |
| `only_lanes` | `()` | Comma-separated `codeql`, `skipped_tests`, `deprecations` | Restricts dispatch eligibility without changing candidate discovery or reporting |
| `has_issues` | `true` | `true`, `false` | False aborts before writes unless degraded PR-comment sink is selected |
| `issue_sink` | `issues` | `issues`, `pr_comment` | `pr_comment` marks artifacts/run degraded |
| `marker_search_enabled` | `true` | `true`, `false` | Enables durable marker reconciliation |
| `version_source` | `.github/ISSUE_TEMPLATE/bug-report.yml` | Repo-relative path | No concrete release is a startup error |
| `lane2_class_breadth_max` | `5` | `>=1` | Wider skipped classes fail automatability |
| `target_owner` | `victorciao` | Non-empty string | GitHub target owner |
| `target_repo` | `superset` | Non-empty string | GitHub target repository |
| `rubrics_path` | `config/rubrics.yaml` | Path | Observable rubric tables |
| `templates_dir` | `templates` | Path | Vendored issue/PR templates |
| `session_snapshot_id` | unset | Optional string | Devin snapshot for target-repository role sessions |
| `github_token` | unset | Runtime secret | Environment-only; required by LIVE |
| `devin_api_key` | unset | Runtime secret | Environment-only; required by LIVE |

`SECURITY_ISSUE_MODE=generic_tracking` and `BUDGET_HARD_MAX=25` are constants, not knobs.
`only_lanes` accepts a comma-separated lane list from `--only-lanes=...` or
`PIPELINE_ONLY_LANES`; it restricts dispatch eligibility only, so other lanes remain
enumerated, gated, scored, and represented in the run report.
Security issues are always detail-free. The single remediation session is responsible for
implementation, while the orchestrator independently verifies the declared criterion,
publishes artifacts, and evaluates CI evidence before handing the pull request to a human
for merge.

## Docker and Compose smoke

The image uses Python 3.11, copies only the package and `config/`, `templates/`, and
`fixtures/`, and runs as a non-root user. The target checkout is mounted read-only at
`/target-repo`; override `SUPERSET_CHECKOUT` when the clone is elsewhere. Bind-mounted output
must be writable by the container user:

```bash
mkdir -p docker-output
PIPELINE_UID="$(id -u)" PIPELINE_GID="$(id -g)" \
  docker compose run --rm remediation
```

Compose uses `network_mode: none`, mounts `./docker-output` at `/output`, and mounts the
target checkout read-only. LIVE credentials, if ever used by an embedding deployment, are
runtime environment values and are not Docker build inputs.

## Observability and artifacts

The output directory is the root for three observability layers:

* `state/candidates.jsonl` — append-only, last-write-wins lifecycle state used for resume and
  deduplication.
* `reports/events.jsonl` — append-only Layer 1 source-of-truth events, including run ID,
  gate/factor evidence, dispatch, session, review, artifact, and terminal fields.
* `reports/run-<run_id>.md` — Layer 2 per-run summary with candidates, gate outcomes,
  dispatch/defer counts, and artifact links.
* `reports/kpis.md` — Layer 3 local KPI rollup and visually distinct threshold alerts.
* `reports/issues/<candidate_id>.md` — rendered manager-facing issue body.
* `reports/prs/<candidate_id>.md` — rendered engineer/AI-reviewer PR body for PR actions.

The Phase 0c baseline is used for burn-down denominators. A lane absent from
`baseline_valid_lanes` is represented as typed `n/a (capability_unavailable)`, never as zero.

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

Remediation runs can start from a successful CodeQL `workflow_run` dispatch in the Superset
fork, the weekly scheduled workflow, or a manual `workflow_dispatch` with lane, budget,
session, and threshold inputs. The pipeline repository requires repository secrets
`DEVIN_API_KEY` and `REMEDIATION_GITHUB_PAT` (GitHub rejects secret names prefixed
`GITHUB_`, so the workflow maps that secret onto the `GITHUB_PAT_REMEDIATION` environment
variable the configuration reads); the Superset fork requires `REMEDIATION_DISPATCH_TOKEN`
with repository-dispatch rights on the pipeline repository.
