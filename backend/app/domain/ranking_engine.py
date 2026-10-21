"""Hypothesis ranking engine for CLARITY Issue #7.

Implements the tiered transitive total ordering of candidate hypotheses based
on evaluation metrics, complexity, and stable ID tie-breaking.  Evidence
summaries inform PlausibilityStatus and explanations but do NOT re-participate
in the numeric sort key.

Tier ordering (best-first):
  1. VALID_HELD_OUT      — finite held-out primary metric, same dataset_id.
  2. VALID_TRAINING_ONLY — finite training primary metric, no held-out.
  3. INCOMPARABLE        — COMPLETED but non-finite or missing primary metric.
  4. EVALUATION_FAILED   — EvaluationResult.status == FAILED.
  5. UNEVALUATED         — no EvaluationResult available.

Within VALID_HELD_OUT and VALID_TRAINING_ONLY candidates that share a
dataset_id are ordered by:
  a. Primary metric score (direction-aware).
  b. If scores are within the complexity_window: lower complexity wins.
  c. If complexity is also equal: secondary metric score (held-out or training).
  d. Stable tie-breaker: lexicographic ascending hypothesis_id.

Cross-tier comparisons are disclosed in limitation notes.  Training-only and
held-out metrics are never compared as if equivalent.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    utc_now_iso,
)
from .evidence_tracker import (
    collect_evidence,
    derive_plausibility_status,
    summarize_evidence,
)
from .ranking_contracts import (
    EvaluationTier,
    PlausibilityStatus,
    RankedCandidate,
    RankingPolicyConfig,
    RankingResult,
)

# Increment this whenever the ordering strategy or explanation format changes
# in a way that would alter existing rankings.
RANKING_VERSION = "1.0.0"

# Tier priority for cross-tier ordering (lower value = better presentation tier)
_TIER_PRIORITY: Dict[EvaluationTier, int] = {
    EvaluationTier.VALID_HELD_OUT: 0,
    EvaluationTier.VALID_TRAINING_ONLY: 1,
    EvaluationTier.INCOMPARABLE: 2,
    EvaluationTier.EVALUATION_FAILED: 3,
    EvaluationTier.UNEVALUATED: 4,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_finite_metric(value: Optional[float]) -> bool:
    """Return True when value is a finite float usable in ranking comparisons."""
    return value is not None and math.isfinite(value)


def _get_metric(
    metrics: Optional[Dict[str, Optional[float]]],
    metric_name: str,
) -> Optional[float]:
    """Extract a named metric value from a metrics dict; return None if absent or non-finite."""
    if metrics is None:
        return None
    val = metrics.get(metric_name)
    if not _is_finite_metric(val):
        return None
    return val


def _determine_tier(
    eval_result: Optional[EvaluationResult],
    policy: RankingPolicyConfig,
) -> Tuple[EvaluationTier, Optional[float], Optional[float], Optional[float]]:
    """Classify a candidate into an EvaluationTier and extract comparable scores.

    Returns
    -------
    tier
        The assigned EvaluationTier.
    held_out_score
        Primary metric value from held_out_metrics (finite), or None.
    training_score
        Primary metric value from training metrics (finite), or None.
    secondary_held_out_score
        Secondary metric value from held_out_metrics (finite), or None.
    """
    if eval_result is None:
        return EvaluationTier.UNEVALUATED, None, None, None

    if eval_result.status == EvaluationStatus.FAILED:
        return EvaluationTier.EVALUATION_FAILED, None, None, None

    # Check for explicit non-finite primary metric in held-out or training metrics
    if eval_result.held_out_metrics is not None and policy.primary_metric in eval_result.held_out_metrics:
        raw_held_out = eval_result.held_out_metrics.get(policy.primary_metric)
        if not _is_finite_metric(raw_held_out):
            return EvaluationTier.INCOMPARABLE, None, None, None

    if eval_result.metrics is not None and policy.primary_metric in eval_result.metrics:
        raw_training = eval_result.metrics.get(policy.primary_metric)
        if not _is_finite_metric(raw_training):
            return EvaluationTier.INCOMPARABLE, None, None, None

    # Attempt VALID_HELD_OUT.
    held_out_score = _get_metric(eval_result.held_out_metrics, policy.primary_metric)
    training_score = _get_metric(eval_result.metrics, policy.primary_metric)
    secondary_held_out = _get_metric(eval_result.held_out_metrics, policy.secondary_metric)

    if held_out_score is not None:
        return EvaluationTier.VALID_HELD_OUT, held_out_score, training_score, secondary_held_out

    if training_score is not None:
        return EvaluationTier.VALID_TRAINING_ONLY, None, training_score, None

    # COMPLETED but no usable primary metric.
    return EvaluationTier.INCOMPARABLE, None, training_score, None


def _dataset_id(eval_result: Optional[EvaluationResult]) -> Optional[str]:
    """Extract dataset_id from evaluation result, if present."""
    if eval_result is None or eval_result.dataset_ids is None:
        return None
    ds = eval_result.dataset_ids.get("dataset_id")
    if isinstance(ds, str) and ds:
        return ds
    return None


def _complexity(
    eval_result: Optional[EvaluationResult],
    candidate: Optional[HypothesisRepresentation],
) -> Optional[int]:
    """Return complexity from EvaluationResult.complexity, falling back to candidate."""
    if eval_result is not None and eval_result.complexity is not None:
        return eval_result.complexity
    if candidate is not None:
        return candidate.complexity
    return None


def _sort_key(
    tier: EvaluationTier,
    held_out_score: Optional[float],
    training_score: Optional[float],
    secondary_held_out: Optional[float],
    complexity_val: Optional[int],
    hypothesis_id: str,
    policy: RankingPolicyConfig,
    best_score_in_group: Optional[float] = None,
) -> tuple:
    """Produce a sortable key representing the ranking order for one candidate.

    Lower key value = better rank (Python's sort is ascending).

    Key structure:
      (tier_priority, band, complexity_sort, metric_sort, secondary_sort, hypothesis_id)

    Candidates within the complexity_window relative to the group's best score
    share band=0, where lower complexity takes precedence.
    """
    tier_priority = _TIER_PRIORITY[tier]

    if tier == EvaluationTier.VALID_HELD_OUT and held_out_score is not None:
        active_score = held_out_score
    elif tier == EvaluationTier.VALID_TRAINING_ONLY and training_score is not None:
        active_score = training_score
    else:
        # For non-ranked tiers, use a stable sentinel so IDs break ties.
        return (tier_priority, 0, 0, 0.0, 0.0, hypothesis_id)

    direction = 1.0 if policy.metric_direction == "minimize" else -1.0
    metric_sort = direction * active_score

    # Compute equivalence band relative to best performer in the cohort
    band = 0
    if policy.complexity_window > 0.0 and best_score_in_group is not None:
        if policy.metric_direction == "minimize":
            delta = max(0.0, active_score - best_score_in_group)
        else:
            delta = max(0.0, best_score_in_group - active_score)
        denom = max(abs(best_score_in_group), 1e-9)
        rel_diff = delta / denom
        band = int(rel_diff // policy.complexity_window)

    # Complexity: lower is better (simpler model preferred when within band).
    complexity_sort = complexity_val if complexity_val is not None else 999999

    # Secondary metric sort (direction-aware).
    if secondary_held_out is not None and _is_finite_metric(secondary_held_out):
        secondary_sort = direction * secondary_held_out
    else:
        secondary_sort = direction * (1e308 if direction > 0 else -1e308)

    return (tier_priority, band, complexity_sort, metric_sort, secondary_sort, hypothesis_id)


def _build_explanation(
    hypothesis_id: str,
    tier: EvaluationTier,
    held_out_score: Optional[float],
    training_score: Optional[float],
    secondary_held_out: Optional[float],
    complexity_val: Optional[int],
    rank: int,
    status: PlausibilityStatus,
    policy: RankingPolicyConfig,
    contradicting_count: int,
    supporting_count: int,
    indeterminate_count: int,
    sample_count: int,
    held_out_sample_count: Optional[int],
    dataset_id: Optional[str],
    evaluation_id: Optional[str],
) -> str:
    """Build a deterministic, human-readable ranking explanation."""
    lines = [
        f"Rank {rank} | {hypothesis_id} | Status: {status.value} | Tier: {tier.value}"
    ]

    if evaluation_id:
        lines.append(f"Evaluation: {evaluation_id}")
    if dataset_id:
        lines.append(f"Dataset: {dataset_id}")

    if tier == EvaluationTier.VALID_HELD_OUT:
        lines.append(
            f"Ranking basis: held-out {policy.primary_metric} = {held_out_score:.6g} "
            f"({policy.metric_direction}), {held_out_sample_count or 'unknown'} held-out observations."
        )
        if secondary_held_out is not None:
            lines.append(f"Secondary (tie-break): held-out {policy.secondary_metric} = {secondary_held_out:.6g}.")
    elif tier == EvaluationTier.VALID_TRAINING_ONLY:
        lines.append(
            f"Ranking basis: training {policy.primary_metric} = {training_score:.6g} "
            f"({policy.metric_direction}), {sample_count} training observations. "
            f"No held-out evaluation was available."
        )
    elif tier == EvaluationTier.INCOMPARABLE:
        lines.append(
            "Ranked in INCOMPARABLE tier: evaluation completed but the primary "
            f"metric ({policy.primary_metric}) is absent or non-finite. "
            "No metric-based sort applied."
        )
    elif tier == EvaluationTier.EVALUATION_FAILED:
        lines.append("Ranked in EVALUATION_FAILED tier: evaluation did not complete.")
    elif tier == EvaluationTier.UNEVALUATED:
        lines.append("Ranked in UNEVALUATED tier: no evaluation result is available.")

    if complexity_val is not None:
        lines.append(f"Complexity: {complexity_val}.")

    # Evidence contribution to status (not to metric score).
    lines.append(
        f"Evidence: {supporting_count} supporting, {contradicting_count} contradicting, "
        f"{indeterminate_count} indeterminate observations."
    )
    if status == PlausibilityStatus.CONTRADICTED:
        lines.append(
            f"Status CONTRADICTED: {contradicting_count} contradiction(s) exceed the "
            f"tolerance threshold (max allowed: {policy.max_tolerated_contradictions})."
        )
    elif status == PlausibilityStatus.UNCERTAIN:
        if sample_count < policy.min_samples_for_plausibility:
            lines.append(
                f"Status UNCERTAIN: only {sample_count} training observations accepted "
                f"(minimum required for PLAUSIBLE: {policy.min_samples_for_plausibility})."
            )
        elif indeterminate_count > 0:
            lines.append(
                f"Status UNCERTAIN: {indeterminate_count} observation(s) yielded "
                "INDETERMINATE evidence (non-finite observed or predicted values)."
            )

    return " | ".join(lines)


def _build_limitations(
    tier: EvaluationTier,
    status: PlausibilityStatus,
    dataset_id: Optional[str],
    has_cross_tier_peers: bool,
) -> Tuple[tuple, tuple]:
    """Return (limitations, comparability_notes) tuples for a ranked candidate."""
    limitations: List[str] = []
    comparability_notes: List[str] = []

    if tier == EvaluationTier.VALID_TRAINING_ONLY:
        limitations.append(
            "Training-only metrics: generalization to unseen observations is not confirmed."
        )
        limitations.append(
            "Training MAE/RMSE may underestimate true prediction error due to overfitting."
        )

    if tier == EvaluationTier.INCOMPARABLE:
        limitations.append(
            f"Primary metric is absent or non-finite; no metric-based ordering was applied."
        )

    if tier in (EvaluationTier.EVALUATION_FAILED, EvaluationTier.UNEVALUATED):
        limitations.append(
            "No valid evaluation result: metric-based ranking is not possible."
        )

    if dataset_id is None and tier not in (
        EvaluationTier.UNEVALUATED, EvaluationTier.EVALUATION_FAILED
    ):
        limitations.append(
            "Dataset identifier absent: comparability with other candidates is unverified."
        )

    if status == PlausibilityStatus.CONTRADICTED:
        limitations.append(
            "Candidate has contradicting observations: it should not be treated as "
            "scientifically validated despite its metric position."
        )

    if status == PlausibilityStatus.UNEVALUABLE:
        limitations.append(
            "Evaluation failed: the fitting or metric pipeline reported an error."
        )

    if has_cross_tier_peers:
        comparability_notes.append(
            "Other candidates in this ranking are in different evaluation tiers. "
            "Held-out and training-only metrics are not compared as equivalents; "
            "cross-tier ordering reflects tier priority, not metric magnitude."
        )

    return tuple(limitations), tuple(comparability_notes)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rank_candidates(
    candidates: Sequence[HypothesisRepresentation],
    evaluation_results: Mapping[str, EvaluationResult],
    observations_by_hypothesis: Mapping[str, Sequence[Any]],
    policy: Optional[RankingPolicyConfig] = None,
    dataset_version: Optional[int] = None,
) -> RankingResult:
    """Rank candidate hypotheses and return a fully structured RankingResult.

    Parameters
    ----------
    candidates
        All candidate hypotheses to rank.  Candidates with no corresponding
        entry in evaluation_results are assigned to the UNEVALUATED tier.
    evaluation_results
        Map of hypothesis_id -> EvaluationResult.  Only complete or failed
        results are accepted; pending results should not be passed here.
    observations_by_hypothesis
        Map of hypothesis_id -> list of Observation objects used for each
        candidate's evaluation.  Used to build EvidenceSummaries.
    policy
        Ranking policy configuration.  Defaults to RankingPolicyConfig() when
        not specified.
    dataset_version
        Optional numeric dataset version for evidence provenance.

    Returns
    -------
    RankingResult
        Ordered tuple of RankedCandidate from rank 1 (best) downward.
    """
    if policy is None:
        policy = RankingPolicyConfig()

    now = utc_now_iso()
    overall_limitations: List[str] = []

    # ---- Per-candidate data extraction ------------------------------------ #
    class _CandidateData:
        __slots__ = (
            "hypothesis_id", "eval_result", "tier",
            "held_out_score", "training_score", "secondary_held_out",
            "complexity_val", "dataset_id_val", "evidence_summary",
            "plausibility", "eval_id", "training_sample_count",
            "held_out_sample_count",
        )

        def __init__(
            self,
            candidate: HypothesisRepresentation,
            eval_result: Optional[EvaluationResult],
            obs: Sequence[Any],
            policy: RankingPolicyConfig,
            dataset_version: Optional[int],
        ):
            self.hypothesis_id = candidate.hypothesis_id
            self.eval_result = eval_result

            self.tier, self.held_out_score, self.training_score, self.secondary_held_out = (
                _determine_tier(eval_result, policy)
            )
            self.complexity_val = _complexity(eval_result, candidate)
            self.dataset_id_val = _dataset_id(eval_result)

            # Build evidence summary.
            if eval_result is not None and obs:
                records = collect_evidence(
                    eval_result, list(obs), policy, dataset_version=dataset_version
                )
            else:
                records = []
            self.evidence_summary = summarize_evidence(self.hypothesis_id, records)
            self.plausibility = derive_plausibility_status(eval_result, self.evidence_summary, policy)

            self.eval_id = eval_result.evaluation_id if eval_result is not None else None
            self.training_sample_count = eval_result.sample_count if eval_result is not None else 0
            self.held_out_sample_count = (
                eval_result.held_out_sample_count if eval_result is not None else None
            )

    data_list: List[_CandidateData] = []
    for candidate in candidates:
        eval_result = evaluation_results.get(candidate.hypothesis_id)
        obs_list = observations_by_hypothesis.get(candidate.hypothesis_id, [])
        data = _CandidateData(candidate, eval_result, obs_list, policy, dataset_version)
        data_list.append(data)

    # Check for cross-tier heterogeneity.
    tiers_present = {d.tier for d in data_list}
    has_cross_tier = len(tiers_present) > 1

    # Check for mixed dataset_ids within comparable tiers.
    comparable_dataset_ids = {
        d.dataset_id_val
        for d in data_list
        if d.tier in (EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY)
        and d.dataset_id_val is not None
    }
    if len(comparable_dataset_ids) > 1:
        overall_limitations.append(
            f"Candidates were evaluated on different datasets "
            f"({', '.join(sorted(comparable_dataset_ids))}). "
            "Within-tier ordering by metric is preserved but cross-dataset comparisons "
            "reflect potentially incompatible evaluation conditions."
        )

    # Warn when training-only and held-out candidates coexist.
    if (
        EvaluationTier.VALID_HELD_OUT in tiers_present
        and EvaluationTier.VALID_TRAINING_ONLY in tiers_present
    ):
        overall_limitations.append(
            "Some candidates have held-out evaluation results and others have only "
            "training metrics. These groups are presented in separate tiers and "
            "their metric scores are NOT compared directly."
        )

    # Precompute best score per (tier, dataset_id) group for complexity banding
    best_scores: Dict[Tuple[EvaluationTier, Optional[str]], float] = {}
    for d in data_list:
        score = None
        if d.tier == EvaluationTier.VALID_HELD_OUT and d.held_out_score is not None:
            score = d.held_out_score
        elif d.tier == EvaluationTier.VALID_TRAINING_ONLY and d.training_score is not None:
            score = d.training_score
        if score is not None:
            group_key = (d.tier, d.dataset_id_val)
            if group_key not in best_scores:
                best_scores[group_key] = score
            else:
                if policy.metric_direction == "minimize":
                    if score < best_scores[group_key]:
                        best_scores[group_key] = score
                else:
                    if score > best_scores[group_key]:
                        best_scores[group_key] = score

    # ---- Sort ------------------------------------------------------------ #
    data_list.sort(
        key=lambda d: _sort_key(
            d.tier,
            d.held_out_score,
            d.training_score,
            d.secondary_held_out,
            d.complexity_val,
            d.hypothesis_id,
            policy,
            best_scores.get((d.tier, d.dataset_id_val)),
        )
    )

    # ---- Build RankedCandidate list --------------------------------------- #
    ranked: List[RankedCandidate] = []
    for rank_idx, d in enumerate(data_list, start=1):
        limitations, comparability_notes = _build_limitations(
            d.tier, d.plausibility, d.dataset_id_val, has_cross_tier
        )
        explanation = _build_explanation(
            hypothesis_id=d.hypothesis_id,
            tier=d.tier,
            held_out_score=d.held_out_score,
            training_score=d.training_score,
            secondary_held_out=d.secondary_held_out,
            complexity_val=d.complexity_val,
            rank=rank_idx,
            status=d.plausibility,
            policy=policy,
            contradicting_count=d.evidence_summary.contradicting_count,
            supporting_count=d.evidence_summary.supporting_count,
            indeterminate_count=d.evidence_summary.indeterminate_count,
            sample_count=d.training_sample_count,
            held_out_sample_count=d.held_out_sample_count,
            dataset_id=d.dataset_id_val,
            evaluation_id=d.eval_id,
        )

        rc = RankedCandidate(
            rank=rank_idx,
            hypothesis_id=d.hypothesis_id,
            status=d.plausibility,
            evaluation_tier=d.tier,
            primary_metric_name=policy.primary_metric,
            held_out_score=d.held_out_score,
            training_score=d.training_score,
            secondary_held_out_score=d.secondary_held_out,
            complexity=d.complexity_val,
            held_out_sample_count=d.held_out_sample_count,
            training_sample_count=d.training_sample_count,
            evidence_summary=d.evidence_summary,
            dataset_id=d.dataset_id_val,
            evaluation_id=d.eval_id,
            explanation=explanation,
            limitations=limitations,
            comparability_notes=comparability_notes,
        )
        ranked.append(rc)

    return RankingResult(
        ranked_candidates=tuple(ranked),
        policy_config=policy,
        ranking_version=RANKING_VERSION,
        ranked_at=now,
        limitations=tuple(overall_limitations),
    )


# ==============================================================================
# Metric Comparability Validator & Hypothesis Ranker Service
# ==============================================================================

class MetricComparabilityValidator:
    """Validates whether two or more evaluation results can be fairly compared."""

    def __init__(self, policy: Optional[RankingPolicyConfig] = None):
        self.policy = policy or RankingPolicyConfig()

    def classify_tier(self, eval_result: Optional[EvaluationResult]) -> EvaluationTier:
        """Determine the evaluation comparability tier for an evaluation result."""
        tier, _, _, _ = _determine_tier(eval_result, self.policy)
        return tier

    def validate_pairwise_comparability(
        self,
        eval_a: Optional[EvaluationResult],
        eval_b: Optional[EvaluationResult],
    ) -> Tuple[bool, List[str]]:
        """Check if two evaluation results are directly comparable.

        Returns (is_comparable, list_of_reasons).
        """
        reasons: List[str] = []
        if eval_a is None or eval_b is None:
            reasons.append("One or both candidates have no evaluation result.")
            return False, reasons

        if eval_a.status != EvaluationStatus.COMPLETED or eval_b.status != EvaluationStatus.COMPLETED:
            reasons.append("One or both evaluations did not complete successfully.")
            return False, reasons

        tier_a = self.classify_tier(eval_a)
        tier_b = self.classify_tier(eval_b)
        if tier_a != tier_b:
            reasons.append(
                f"Candidates belong to different presentation tiers ({tier_a.value} vs {tier_b.value})."
            )
            return False, reasons

        if tier_a not in (EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY):
            reasons.append(f"Tier {tier_a.value} is not a directly comparable evaluation tier.")
            return False, reasons

        # Check dataset comparability
        ds_a = _dataset_id(eval_a)
        ds_b = _dataset_id(eval_b)
        if ds_a != ds_b or ds_a is None:
            reasons.append(f"Dataset identifiers do not match or are absent ({ds_a!r} vs {ds_b!r}).")
            return False, reasons

        # Check metric definition/version compatibility if present
        if eval_a.metric_version and eval_b.metric_version and eval_a.metric_version != eval_b.metric_version:
            reasons.append(
                f"Metric versions differ ({eval_a.metric_version!r} vs {eval_b.metric_version!r})."
            )
            return False, reasons

        return True, []


class HypothesisRanker:
    """Service orchestrating candidate hypothesis ranking and reranking."""

    def __init__(self, policy: Optional[RankingPolicyConfig] = None):
        self.policy = policy or RankingPolicyConfig()
        self.validator = MetricComparabilityValidator(self.policy)

    def rank_candidates(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluation_results: Mapping[str, EvaluationResult],
        observations_by_hypothesis: Mapping[str, Sequence[Any]],
        dataset_version: Optional[int] = None,
    ) -> RankingResult:
        """Compute complete ranked cohort of candidate explanations."""
        return rank_candidates(
            candidates=candidates,
            evaluation_results=evaluation_results,
            observations_by_hypothesis=observations_by_hypothesis,
            policy=self.policy,
            dataset_version=dataset_version,
        )

    def rerank_on_new_observations(
        self,
        current_ranking: RankingResult,
        candidates: Sequence[HypothesisRepresentation],
        evaluation_results: Mapping[str, EvaluationResult],
        updated_observations_by_hypothesis: Mapping[str, Sequence[Any]],
        dataset_version: Optional[int] = None,
    ) -> RankingResult:
        """Rerank candidates following observation ingestion or new evaluation results."""
        return self.rank_candidates(
            candidates=candidates,
            evaluation_results=evaluation_results,
            observations_by_hypothesis=updated_observations_by_hypothesis,
            dataset_version=dataset_version,
        )

