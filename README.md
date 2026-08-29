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
environment:

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
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
called. It renders the artifacts that a live run would publish.

Configuration precedence is configuration file, then `PIPELINE_*` environment variables,
then command-line options. For example:

```bash
PYTHONPATH=src .venv/bin/python -m pipeline \
  --mode=simulate --budget-N=5 --output-dir ./run-output
```

The CLI generates a fresh `run_id` for each run and returns a non-zero exit code for
configuration errors, blocking capability preconditions, or hard session/cost ceilings.

## LIVE

LIVE is deliberately guarded and has not been run or proven in this repository. Runtime
credentials must be supplied through the environment only; they are never accepted from a
configuration file, Docker build argument, image layer, source file, or log:

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
`ci_evidence_mode`; local evidence hard-disables auto-merge.

The command-line entrypoint currently refuses LIVE before remote work unless a guarded Devin
and GitHub transport is supplied by an embedding runtime. Consequently, no LIVE capability
probe or remote artifact write is claimed as verified here.

## Configuration reference (§13)

| Name | Default | Range / allowed values | Safety behavior |
|---|---:|---|---|
| `mode` | `simulate` | `simulate`, `live` | `live` is explicit and credential-gated; unset values default to simulate |
| `iteration_cap` | `5` | `1..10` | Bounds the implementer/reviewer loop |
| `coverage_bar` | `0.80` | `0.0..1.0` | Coverage threshold used by review policy |
| `budget_N` | `10` | `1..25` (`BUDGET_HARD_MAX=25`) | Dispatch overflow is deferred; above 25 requires the explicit acknowledgment flag and is clamped |
| `score_cap` | `200` | `>0` | Caps calculated scores |
| `tier_high_min` | `60` | `> tier_medium_min` | High-tier PR routing threshold |
| `tier_medium_min` | `20` | `>0` | Medium-tier issue routing threshold |
| `eol_major_lag` | `2` | `>=1` | Major-version age required for EOL |
| `merge_rate_floor` | `0.50` | `0.0..1.0` | KPI alert threshold |
| `session_failure_ceiling` | `0.30` | `0.0..1.0` | KPI alert threshold and run safety signal |
| `max_sessions` | `20` | `>=1` | Per-run hard session ceiling; exceeding it aborts |
| `max_total_acu` | `500.0` | `>0` | Per-run hard ACU ceiling; exceeding it aborts |
| `kpi_sink` | `local` | `local`, `gsheet` | `gsheet` is rejected in SIMULATE |
| `major_only_requires_human` | `true` | `true`, `false` | Routing label only; unresolved majors remain ineligible for auto-merge |
| `alert_source` | `api` | `api`, `sarif_file` | SIMULATE uses the checked-in fixture |
| `alert_fixture_path` | `fixtures/codeql_alerts.json` | Path | Captured CodeQL/SARIF input |
| `ci_evidence_mode` | resolved by §3 0d | `github`, `local` | `local` forces auto-merge off |
| `ci_wait_timeout_s` | `5400` | `>0` | Bounds GitHub evidence waiting |
| `auto_merge_enabled` | `false` | `true`, `false` | Never sufficient alone; forced off for local evidence |
| `has_issues` | `true` | `true`, `false` | False aborts before writes unless degraded PR-comment sink is selected |
| `issue_sink` | `issues` | `issues`, `pr_comment` | `pr_comment` marks artifacts/run degraded |
| `version_source` | `.github/ISSUE_TEMPLATE/bug-report.yml` | Repo-relative path | No concrete release is a startup error |
| `lane2_class_breadth_max` | `5` | `>=1` | Wider skipped classes fail automatability |
| `target_owner` | `victorciao` | Non-empty string | GitHub target owner |
| `target_repo` | `superset` | Non-empty string | GitHub target repository |
| `rubrics_path` | `config/rubrics.yaml` | Path | Observable rubric tables |
| `templates_dir` | `templates` | Path | Vendored issue/PR templates |
| `github_token` | unset | Runtime secret | Environment-only; required by LIVE |
| `devin_api_key` | unset | Runtime secret | Environment-only; required by LIVE |

`SECURITY_ISSUE_MODE=generic_tracking` and `BUDGET_HARD_MAX=25` are constants, not knobs.
Security issues are always detail-free. Structural role separation and reviewer ownership
are not configurable.

## Docker and Compose smoke

The image uses Python 3.11, copies only the package and `config/`, `templates/`, and
`fixtures/`, and runs as a non-root user. No network is needed for the smoke:

```bash
mkdir -p docker-output
docker compose run --rm remediation
```

Compose uses `network_mode: none` and mounts `./docker-output` at `/output`. LIVE credentials,
if ever used by an embedding deployment, are runtime environment values and are not Docker
build inputs.

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
captured Superset revision (identical except `captured_at`), credential-free SIMULATE, and the
Docker Compose smoke when a Docker daemon is available. The implementation has not run LIVE;
therefore live GitHub/Devin probes, remote writes, CI evidence, merge behavior, and production
PR lifecycle outcomes are not proven. Reviewer-owned files under `tests/` are intentionally
not modified or run.
