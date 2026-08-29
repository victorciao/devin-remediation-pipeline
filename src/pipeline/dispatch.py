"""Pure tier mapping and per-run dispatch decisions."""

from collections.abc import Sequence

from pipeline.config import CiEvidenceMode, PipelineConfig
from pipeline.schemas import (
    NEEDS_HUMAN_REVIEW_LABEL,
    Action,
    Candidate,
    CandidateState,
    GateName,
    ReasonCode,
    Tier,
)

HUMAN_ROUTED_REASONS = frozenset(
    {
        ReasonCode.CLASS_SCOPE_TOO_BROAD,
        ReasonCode.CLASS_BREADTH_UNKNOWN,
        ReasonCode.BLOCKED_BY_ENCLOSING_SKIP,
        ReasonCode.PUBLIC_API_SURFACE,
        ReasonCode.INTERNAL_CALLER,
    }
)
DROPPED_REASONS = frozenset(
    {
        ReasonCode.OUT_OF_SCOPE_FRONTEND,
        ReasonCode.NOT_EOL,
        ReasonCode.TRIGGER_MISSING,
        ReasonCode.AUTOMATABILITY_LOW,
        ReasonCode.VERIFIABILITY_MISSING,
    }
)


def tier_for_score(score: float, config: PipelineConfig) -> Tier:
    """Map scores at or above thresholds to high, medium, or low tiers."""
    if score >= config.tier_high_min:
        return Tier.HIGH
    if score >= config.tier_medium_min:
        return Tier.MEDIUM
    return Tier.LOW


class ContainmentError(ValueError):
    """Raised when current-run containment rows are incomplete or cyclic."""


def _containment_depths(candidates: Sequence[Candidate]) -> dict[str, int]:
    by_nodeid: dict[str, Candidate] = {}
    for candidate in candidates:
        if candidate.nodeid is not None:
            if candidate.nodeid in by_nodeid:
                raise ContainmentError(f"duplicate containment nodeid: {candidate.nodeid}")
            by_nodeid[candidate.nodeid] = candidate

    depths: dict[str, int] = {}
    for candidate in candidates:
        depth = 0
        current = candidate
        visited = {candidate.candidate_id}
        while current.enclosing_skip_nodeid is not None:
            parent = by_nodeid.get(current.enclosing_skip_nodeid)
            if parent is None:
                raise ContainmentError(
                    f"missing enclosing candidate: {current.enclosing_skip_nodeid}"
                )
            if parent.candidate_id in visited:
                raise ContainmentError(f"containment cycle involving {parent.candidate_id}")
            visited.add(parent.candidate_id)
            depth += 1
            current = parent
        depths[candidate.candidate_id] = depth
    return depths


def _ordered(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Order by containment depth, then score descending and candidate ID ascending."""
    depths = _containment_depths(candidates)
    return sorted(
        candidates,
        key=lambda candidate: (
            depths[candidate.candidate_id],
            -(candidate.score if candidate.score is not None else -1),
            candidate.candidate_id,
        ),
    )


def _with_label(candidate: Candidate, label: str) -> list[str]:
    return [*candidate.labels] if label in candidate.labels else [*candidate.labels, label]


def _blocked_child(candidate: Candidate) -> Candidate:
    return candidate.model_copy(
        update={
            "action": Action.HUMAN_REVIEW,
            "state": CandidateState.BLOCKED_BY_ENCLOSING_SKIP,
            "gate_passed": False,
            "failed_gate": GateName.AUTOMATABILITY,
            "reason": ReasonCode.BLOCKED_BY_ENCLOSING_SKIP,
            "labels": _with_label(candidate, NEEDS_HUMAN_REVIEW_LABEL),
            "auto_merge_eligible": False,
        }
    )


def _suppressed_child(candidate: Candidate) -> Candidate:
    return candidate.model_copy(
        update={
            "action": Action.DEFERRED,
            "state": CandidateState.SUPPRESSED_BY_CONTAINMENT,
            "reason": ReasonCode.SUPPRESSED_BY_CONTAINMENT,
            "auto_merge_eligible": False,
        }
    )


def _gated_out(candidate: Candidate) -> Candidate:
    reason = candidate.reason
    if reason is None and candidate.failed_gate is not None:
        failed_result = candidate.gate_results.get(candidate.failed_gate)
        reason = failed_result.reason if failed_result is not None else None
    if reason in HUMAN_ROUTED_REASONS:
        return candidate.model_copy(
            update={
                "action": Action.HUMAN_REVIEW,
                "state": CandidateState.GATED,
                "reason": reason,
                "auto_merge_eligible": False,
                "labels": _with_label(candidate, NEEDS_HUMAN_REVIEW_LABEL),
            }
        )
    if reason is not None and reason not in DROPPED_REASONS:
        raise ValueError(f"no gate route defined for reason: {reason.value}")
    return candidate.model_copy(
        update={
            "action": Action.LOG_ONLY,
            "state": CandidateState.TERMINAL,
            "reason": reason,
            "auto_merge_eligible": False,
        }
    )


def dispatch_candidates(candidates: Sequence[Candidate], config: PipelineConfig) -> list[Candidate]:
    """Dispatch scored candidates deterministically, deferring budget overflow.

    Candidates are ordered by containment depth so every ancestor is decided first,
    then by descending score and ascending ``candidate_id``. The candidate ID is the
    tie-break for budget overflow among otherwise equal candidates.
    """
    source = list(candidates)
    decisions: dict[str, Candidate] = {}
    dispatched_count = 0
    for candidate in _ordered(source):
        parent = next(
            (
                possible_parent
                for possible_parent in source
                if possible_parent.nodeid == candidate.enclosing_skip_nodeid
            ),
            None,
        )
        if candidate.enclosing_skip_nodeid is not None:
            parent_decision = decisions.get(parent.candidate_id) if parent is not None else None
            parent_supports_containment = parent_decision is not None and (
                (parent_decision.action is Action.OPEN_PR and parent_decision.tier is Tier.HIGH)
                or parent_decision.state is CandidateState.SUPPRESSED_BY_CONTAINMENT
            )
            if parent_supports_containment:
                child_decision = _suppressed_child(candidate)
            else:
                child_decision = _blocked_child(candidate)
            if parent is not None:
                child_decision = child_decision.model_copy(
                    update={"related_candidate_id": parent.candidate_id}
                )
                if parent_decision is not None and parent_decision.related_candidate_id is None:
                    decisions[parent.candidate_id] = parent_decision.model_copy(
                        update={"related_candidate_id": candidate.candidate_id}
                    )
            decisions[candidate.candidate_id] = child_decision
            continue

        if candidate.gate_passed is not True:
            decisions[candidate.candidate_id] = _gated_out(candidate)
            continue
        if candidate.score is None or candidate.risk is None:
            raise ValueError(f"candidate {candidate.candidate_id} must be scored before dispatch")

        tier = tier_for_score(candidate.score, config)
        if tier is Tier.LOW:
            decisions[candidate.candidate_id] = candidate.model_copy(
                update={
                    "tier": tier,
                    "action": Action.LOG_ONLY,
                    "state": CandidateState.TERMINAL,
                    "auto_merge_eligible": False,
                }
            )
            continue
        if dispatched_count >= config.budget_N:
            decisions[candidate.candidate_id] = candidate.model_copy(
                update={
                    "tier": tier,
                    "action": Action.DEFERRED,
                    "state": CandidateState.DEFERRED,
                    "auto_merge_eligible": False,
                }
            )
            continue

        labels = list(candidate.labels)
        if tier is Tier.HIGH and (candidate.risk >= 3 or candidate.unresolved_major):
            labels = _with_label(candidate, NEEDS_HUMAN_REVIEW_LABEL)
        auto_merge = (
            tier is Tier.HIGH
            and candidate.risk <= 2
            and config.auto_merge_enabled
            and config.ci_evidence_mode is not CiEvidenceMode.LOCAL
            and not candidate.unresolved_major
        )
        if candidate.unresolved_major and config.major_only_requires_human:
            labels = _with_label(candidate, NEEDS_HUMAN_REVIEW_LABEL)
        decisions[candidate.candidate_id] = candidate.model_copy(
            update={
                "tier": tier,
                "action": Action.OPEN_PR if tier is Tier.HIGH else Action.OPEN_ISSUE,
                "state": CandidateState.DISPATCHING,
                "auto_merge_eligible": auto_merge,
                "labels": labels,
            }
        )
        dispatched_count += 1

    return [decisions[candidate.candidate_id] for candidate in source]
