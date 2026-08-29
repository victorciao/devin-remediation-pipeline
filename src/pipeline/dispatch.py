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


def tier_for_score(score: float, config: PipelineConfig) -> Tier:
    """Map scores at or above thresholds to high, medium, or low tiers."""
    if score >= config.tier_high_min:
        return Tier.HIGH
    if score >= config.tier_medium_min:
        return Tier.MEDIUM
    return Tier.LOW


def _parent_for(candidate: Candidate, candidates: Sequence[Candidate]) -> Candidate | None:
    if candidate.enclosing_skip_nodeid is None:
        return None
    for possible_parent in candidates:
        if possible_parent.nodeid == candidate.enclosing_skip_nodeid:
            return possible_parent
    return None


def _ordered(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Order parents first, then score descending and candidate ID ascending."""
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.enclosing_skip_nodeid is not None,
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
            "failed_gate": GateName.LANE2_OVERLAP,
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
    return candidate.model_copy(
        update={
            "action": Action.HUMAN_REVIEW,
            "state": CandidateState.GATED,
            "auto_merge_eligible": False,
        }
    )


def dispatch_candidates(candidates: Sequence[Candidate], config: PipelineConfig) -> list[Candidate]:
    """Dispatch scored candidates deterministically, deferring budget overflow.

    Parents are ordered before children. Remaining candidates are sorted by descending
    score and ascending ``candidate_id``; the latter is the tie-break for budget overflow.
    """
    source = list(candidates)
    decisions: dict[str, Candidate] = {}
    dispatched_count = 0
    for candidate in _ordered(source):
        parent = _parent_for(candidate, source)
        if candidate.enclosing_skip_nodeid is not None:
            parent_decision = decisions.get(parent.candidate_id) if parent is not None else None
            if (
                parent_decision is None
                or parent_decision.action is not Action.OPEN_PR
                or parent_decision.tier is not Tier.HIGH
            ):
                child_decision = _blocked_child(candidate)
            else:
                child_decision = _suppressed_child(candidate)
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


dispatch = dispatch_candidates
