"""§17 packaging smoke — Docker runs SIMULATE end-to-end and bakes in no credentials.

The container is never started here (a test suite must stay network- and daemon-free), so the
smoke is the static contract: the image entrypoint runs SIMULATE, the compose service injects
credentials at runtime only, and the §13 coverage subjects are the ones the bar is measured on.
"""

from __future__ import annotations

import tomllib

import yaml

from tests.conftest import REPO_ROOT

COVERAGE_SUBJECTS = (
    "src/pipeline/gate.py",
    "src/pipeline/score.py",
    "src/pipeline/dispatch.py",
    "src/pipeline/dedupe.py",
    "src/pipeline/templates/render.py",
    "src/pipeline/observability/kpis.py",
)


def test_dockerfile_runs_simulate_by_default() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM" in dockerfile
    assert "simulate" in dockerfile.lower()
    assert "ENTRYPOINT" in dockerfile or "CMD" in dockerfile


def test_dockerfile_bakes_in_no_secrets() -> None:
    """§14 — no secrets in the image; credentials are injected at runtime."""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    for line in dockerfile.splitlines():
        if line.strip().startswith(("ENV", "ARG")):
            assert "TOKEN=" not in line.upper().replace(" ", "")
            assert "API_KEY=" not in line.upper().replace(" ", "")
        assert "ghp_" not in line


def test_compose_service_runs_the_pipeline_in_simulate() -> None:
    """§17 — `docker compose up` runs SIMULATE end-to-end."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))

    assert isinstance(compose, dict)
    services = compose["services"]
    assert isinstance(services, dict) and services != {}
    service = next(iter(services.values()))
    rendered = yaml.safe_dump(service)
    assert "simulate" in rendered.lower()
    assert "ghp_" not in rendered


def test_coverage_configuration_measures_the_plan_subjects() -> None:
    """§13 — the 80% bar is enforced on exactly the six pure-logic modules."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = pyproject["tool"]["coverage"]

    assert tuple(coverage["run"]["include"]) == COVERAGE_SUBJECTS
    assert coverage["report"]["fail_under"] >= 80
