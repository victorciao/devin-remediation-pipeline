"""Candidate factories for the REPO A test suite.

Candidates carry **observables** only: §4.1 makes `pipeline.rubric.resolve_factors` the single
owner of the observable -> rubric-row -> factor-value resolution, so a factory that pre-set
`business_impact` and friends would let a test pass while the gate reads an unresolved field.
"""

from __future__ import annotations

from typing import Any

from pipeline.schemas import Candidate, DefinitionKind, Lane

TARGET_REPO = "victorciao/superset"


def make_candidate(
    *,
    lane: Lane,
    candidate_id: str = "cand-0",
    stable_locator: str = "locator",
    repo: str = TARGET_REPO,
    **fields: Any,  # noqa: ANN401
) -> Candidate:
    """Build a `Candidate` with the identity fields filled in."""
    return Candidate(
        candidate_id=candidate_id,
        lane=lane,
        repo=repo,
        stable_locator=stable_locator,
        **fields,
    )


def codeql_candidate(
    *,
    candidate_id: str = "codeql-0",
    file_path: str = "superset/mcp_service/dashboard/tool/add_chart_to_existing_dashboard.py",
    rule_id: str | None = "py/overly-large-range",
    normalized_symbol: str = "build_range",
    position_digest: str = "aaaaaaaaaaaa",
    region_digest: str = "bbbbbbbbbbbb",
    symbol_relative_offset: int = 3,
    security_severity_level: str | None = "high",
    targeted_test_signal: str | None = "collectable",
    transformation_scope: str | None = "scoped_python_transform",
    rule_precision: str | None = "precise",
    updated_at_fresh: bool | None = False,
    blast_radius: str | None = "bounded_module",
    **fields: Any,  # noqa: ANN401
) -> Candidate:
    """A LANE 1 candidate whose observables resolve to the §4.1 worked example (4/4/4/4 ÷ 2)."""
    return make_candidate(
        lane=Lane.CODEQL,
        candidate_id=candidate_id,
        stable_locator=fields.pop(
            "stable_locator",
            f"{rule_id}|{file_path}|{normalized_symbol}",
        ),
        rule_id=rule_id,
        file_path=file_path,
        normalized_symbol=normalized_symbol,
        position_digest=position_digest,
        region_digest=region_digest,
        symbol_relative_offset=symbol_relative_offset,
        security_severity_level=security_severity_level,
        targeted_test_signal=targeted_test_signal,
        transformation_scope=transformation_scope,
        rule_precision=rule_precision,
        updated_at_fresh=updated_at_fresh,
        blast_radius=blast_radius,
        **fields,
    )


def lane2_candidate(
    *,
    candidate_id: str = "lane2-0",
    nodeid: str = "tests/integration_tests/sqllab_tests.py::TestSqlLab::test_run_sync_query",
    kind: DefinitionKind | None = DefinitionKind.FUNCTION,
    class_scope: str | None = "TestSqlLab",
    enclosed_tests: int | None = 1,
    live_enclosed_tests: int | None = None,
    parametrized: bool = False,
    collects_single_item: bool = True,
    enclosing_skip_nodeid: str | None = None,
    skip_reason: str | None = "broken since the api/v1 migration",
    scope_is_test_only: bool | None = True,
    targeted_test_signal: str | None = "targeted",
    transformation_scope: str | None = "single_test",
    **fields: Any,  # noqa: ANN401
) -> Candidate:
    """A LANE 2 candidate whose observables resolve above every gate threshold."""
    return make_candidate(
        lane=Lane.SKIPPED_TESTS,
        candidate_id=candidate_id,
        stable_locator=fields.pop("stable_locator", nodeid),
        nodeid=nodeid,
        kind=kind,
        class_scope=class_scope,
        enclosed_tests=enclosed_tests,
        live_enclosed_tests=live_enclosed_tests,
        parametrized=parametrized,
        collects_single_item=collects_single_item,
        enclosing_skip_nodeid=enclosing_skip_nodeid,
        skip_reason=skip_reason,
        scope_is_test_only=scope_is_test_only,
        targeted_test_signal=targeted_test_signal,
        transformation_scope=transformation_scope,
        **fields,
    )


def lane3_candidate(
    *,
    candidate_id: str = "lane3-0",
    module: str = "superset.db_engine_specs.base",
    qualname: str = "BaseEngineSpec.normalize_indexes",
    deprecated_in: str | None = "3.0",
    removed_in: str | None = None,
    current_major: int | None = 6,
    caller_count: int | None = 0,
    override_count: int | None = 0,
    public_api_surface: bool | None = False,
    internal_caller: bool | None = False,
    override_surface: bool | None = False,
    targeted_test_signal: str | None = "collectable",
    transformation_scope: str | None = "scoped_removal",
    **fields: Any,  # noqa: ANN401
) -> Candidate:
    """A LANE 3 candidate modelled on the plan's qualifying `normalize_indexes` site."""
    return make_candidate(
        lane=Lane.DEPRECATIONS,
        candidate_id=candidate_id,
        stable_locator=fields.pop("stable_locator", f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        deprecated_in=deprecated_in,
        removed_in=removed_in,
        current_major=current_major,
        caller_count=caller_count,
        override_count=override_count,
        public_api_surface=public_api_surface,
        internal_caller=internal_caller,
        override_surface=override_surface,
        targeted_test_signal=targeted_test_signal,
        transformation_scope=transformation_scope,
        **fields,
    )
