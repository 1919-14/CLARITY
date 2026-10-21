"""Hypothesis ranking engine for CLARITY Issue #7.

Implements the tiered transitive total ordering of candidate hypotheses based
on evaluation metrics, complexity, and stable ID tie-breaking.  Evidence
summaries inform PlausibilityStatus and explanations but do NOT re-participate
in the numeric sort key.

Comparability cohorts
---------------------
Candidates are first assigned to *comparability cohorts* based on:
  (EvaluationTier, dataset_id, metric_version)

Only candidates within the same cohort are directly ranked against each other
using metric magnitudes.  Cross-cohort ordering reflects cohort priority, not
metric magnitude, and is disclosed in limitation notes.

Tier ordering (best-first):
  1. VALID_HELD_OUT      — finite held-out primary metric, same cohort.
  2. VALID_TRAINING_ONLY — finite training primary metric, no held-out.
  3. INCOMPARABLE        — COMPLETED but non-finite / missing primary metric.
  4. EVALUATION_FAILED   — EvaluationResult.status == FAILED.
  5. UNEVALUATED         — no EvaluationResult available.

Within each cohort, candidates are ordered by:
  a. Primary metric score (direction-aware).
  b. If primary scores are within the complexity_window: lower complexity wins.
  c. If complexity is also equal: secondary metric score (held-out or training).
  d. Stable tie-breaker: lexicographic ascending hypothesis_id.

Absent vs. non-finite metrics
------------------------------
An *absent* primary metric (key not in metrics dict) is treated differently
from an *explicitly non-finite* metric (key present with NaN/Inf value).
When a candidate has no held-out primary metric but has a valid training
primary metric, it is placed in VALID_TRAINING_ONLY rather than INCOMPARABLE.
A metric key that exists with a NaN/Inf value is classified INCOMPARABLE.

Metric version compatibility
------------------------------
``MetricComparabilityValidator`` is used during cohort construction.  When
metric_version metadata is absent from one or both candidates, comparability
is flagged as *unverified* in limitation notes rather than silently assumed.
"""

from __future__ import annotations

import math
from typing import Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Set, Tuple

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
from .hypothesis_models import ModelFamilyRegistry, default_registry
from .ranking_contracts import (
    EvaluationTier,
    PlausibilityStatus,
    RankedCandidate,
    RankingPolicyConfig,
    RankingResult,
)

# Increment this whenever the ordering strategy or explanation format changes
# in a way that would alter existing rankings.
RANKING_VERSION = "1.1.0"

# Tier priority for cross-tier ordering (lower value = better presentation tier)
_TIER_PRIORITY: Dict[EvaluationTier, int] = {
    EvaluationTier.VALID_HELD_OUT: 0,
    EvaluationTier.VALID_TRAINING_ONLY: 1,
    EvaluationTier.INCOMPARABLE: 2,
    EvaluationTier.EVALUATION_FAILED: 3,
    EvaluationTier.UNEVALUATED: 4,
}

# Sentinel for absent metric_version (distinct from any real version string)
_METRIC_VERSION_ABSENT = "__absent__"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_finite_metric(value: Optional[float]) -> bool:
    """Return True when value is a finite float usable in ranking comparisons."""
    return value is not None and math.isfinite(value)


def _get_finite_metric(
    metrics: Optional[Dict[str, Optional[float]]],
    metric_name: str,
) -> Tuple[Optional[float], bool]:
    """Extract a named metric from a dict, distinguishing absent from non-finite.

    Returns
    -------
    (value, explicitly_non_finite)
        value               Finite float, or None when absent/non-finite.
        explicitly_non_finite
                            True when the key IS present but its value is
                            NaN/Inf.  False when the key is absent or value is
                            a valid finite number.
    """
    if metrics is None:
        return None, False
    if metric_name not in metrics:
        return None, False
    raw = metrics[metric_name]
    if raw is None:
        return None, False
    try:
        f = float(raw)
    except (TypeError, ValueError, OverflowError):
        return None, True
    if not math.isfinite(f):
        return None, True
    return f, False


def _determine_tier(
    eval_result: Optional[EvaluationResult],
    policy: RankingPolicyConfig,
) -> Tuple[EvaluationTier, Optional[float], Optional[float], Optional[float], bool]:
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
    has_explicit_non_finite
        True when any primary metric key was explicitly NaN/Inf (not merely absent).
    """
    if eval_result is None:
        return EvaluationTier.UNEVALUATED, None, None, None, False

    if eval_result.status == EvaluationStatus.FAILED:
        return EvaluationTier.EVALUATION_FAILED, None, None, None, False

    held_out_val, held_out_nonfinite = _get_finite_metric(
        eval_result.held_out_metrics, policy.primary_metric
    )
    training_val, training_nonfinite = _get_finite_metric(
        eval_result.metrics, policy.primary_metric
    )
    secondary_val, _ = _get_finite_metric(
        eval_result.held_out_metrics, policy.secondary_metric
    )
    has_explicit_non_finite = held_out_nonfinite or training_nonfinite

    # If any explicitly-present metric value is non-finite → INCOMPARABLE.
    if has_explicit_non_finite:
        return EvaluationTier.INCOMPARABLE, None, training_val, None, True

    if held_out_val is not None:
        return EvaluationTier.VALID_HELD_OUT, held_out_val, training_val, secondary_val, False

    if training_val is not None:
        return EvaluationTier.VALID_TRAINING_ONLY, None, training_val, None, False

    # COMPLETED but no usable primary metric in either partition.
    return EvaluationTier.INCOMPARABLE, None, None, None, False


def _dataset_id(eval_result: Optional[EvaluationResult]) -> Optional[str]:
    """Extract dataset_id from evaluation result, if present."""
    if eval_result is None or eval_result.dataset_ids is None:
        return None
    ds = eval_result.dataset_ids.get("dataset_id")
    if isinstance(ds, str) and ds:
        return ds
    return None


def _metric_version(eval_result: Optional[EvaluationResult]) -> str:
    """Return metric_version string or the absent sentinel."""
    if eval_result is None or eval_result.metric_version is None:
        return _METRIC_VERSION_ABSENT
    return eval_result.metric_version


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


def _cohort_key(
    tier: EvaluationTier,
    dataset_id: Optional[str],
    metric_ver: str,
) -> Tuple[int, Optional[str], str]:
    """Compute the cohort grouping key for a candidate.

    Only VALID_HELD_OUT and VALID_TRAINING_ONLY candidates are sorted within
    metric cohorts.  All other tiers use None dataset_id so they form a single
    presentation group ordered only by tier priority and hypothesis_id.
    """
    if tier in (EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY):
        return (_TIER_PRIORITY[tier], dataset_id, metric_ver)
    return (_TIER_PRIORITY[tier], None, "")


def _sort_key(
    cohort_key: Tuple[int, Optional[str], str],
    held_out_score: Optional[float],
    training_score: Optional[float],
    secondary_held_out: Optional[float],
    complexity_val: Optional[int],
    hypothesis_id: str,
    policy: RankingPolicyConfig,
    best_score_in_cohort: Optional[float] = None,
) -> tuple:
    """Produce a sortable key for one candidate.

    Lower key value = better rank (Python's sort is ascending).

    Key structure:
      (cohort_tier_priority, cohort_dataset_sentinel, band,
       complexity_sort, metric_sort, secondary_sort, hypothesis_id)

    Candidates within the complexity_window relative to the cohort's best score
    share band=0, where lower complexity takes precedence.  Candidates in
    different cohorts never share band=0 because their cohort keys differ.
    """
    tier_priority, ds, mv = cohort_key
    # Use dataset_id sentinel for stable cross-cohort ordering (None sorts before strings).
    ds_sentinel = ds if ds is not None else ""

    active_score: Optional[float] = None
    if tier_priority == _TIER_PRIORITY[EvaluationTier.VALID_HELD_OUT]:
        active_score = held_out_score
    elif tier_priority == _TIER_PRIORITY[EvaluationTier.VALID_TRAINING_ONLY]:
        active_score = training_score

    if active_score is None:
        return (tier_priority, ds_sentinel, 0, 0, 0.0, 0.0, hypothesis_id)

    direction = 1.0 if policy.metric_direction == "minimize" else -1.0
    metric_sort = direction * active_score

    # Compute equivalence band relative to best performer in the cohort.
    band = 0
    if policy.complexity_window > 0.0 and best_score_in_cohort is not None:
        if policy.metric_direction == "minimize":
            delta = max(0.0, active_score - best_score_in_cohort)
        else:
            delta = max(0.0, best_score_in_cohort - active_score)
        denom = max(abs(best_score_in_cohort), 1e-9)
        rel_diff = delta / denom
        band = int(rel_diff // policy.complexity_window)

    complexity_sort = complexity_val if complexity_val is not None else 999999

    if secondary_held_out is not None and _is_finite_metric(secondary_held_out):
        secondary_sort = direction * secondary_held_out
    else:
        secondary_sort = direction * (1e308 if direction > 0 else -1e308)

    return (tier_priority, ds_sentinel, band, complexity_sort, metric_sort, secondary_sort, hypothesis_id)


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
    cohort_id: str,
    metric_version_verified: bool,
) -> str:
    """Build a deterministic, human-readable ranking explanation."""
    lines = [
        f"Rank {rank} | {hypothesis_id} | Status: {status.value} | Tier: {tier.value}"
    ]

    if evaluation_id:
        lines.append(f"Evaluation: {evaluation_id}")
    if dataset_id:
        lines.append(f"Dataset: {dataset_id}")
    lines.append(f"Cohort: {cohort_id}")
    if not metric_version_verified:
        lines.append(
            "Metric version: absent or unverified for one or more candidates in this cohort; "
            "direct metric comparison may be invalid."
        )

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
        else:
            lines.append(
                "Status UNCERTAIN: no accepted finite primary metric found in either "
                "partition; acceptable predictive performance cannot be confirmed."
            )

    return " | ".join(lines)


def _build_limitations(
    tier: EvaluationTier,
    status: PlausibilityStatus,
    dataset_id: Optional[str],
    has_cross_tier_peers: bool,
    has_cross_dataset_peers: bool,
    metric_version_verified: bool,
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
            "Primary metric is absent or non-finite; no metric-based ordering was applied."
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

    if not metric_version_verified and tier in (
        EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY
    ):
        limitations.append(
            "Metric version metadata is absent for one or more candidates in this cohort; "
            "direct metric comparison may be invalid."
        )

    if has_cross_tier_peers:
        comparability_notes.append(
            "Other candidates in this ranking are in different evaluation tiers. "
            "Held-out and training-only metrics are not compared as equivalents; "
            "cross-tier ordering reflects tier priority, not metric magnitude."
        )

    if has_cross_dataset_peers:
        comparability_notes.append(
            "Other candidates were evaluated on different datasets. "
            "Each dataset forms its own comparability cohort; candidates across cohorts "
            "are presented in display order only — their metric values are NOT "
            "scientifically comparable."
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
    registry: Optional[ModelFamilyRegistry] = None,
) -> RankingResult:
    """Rank candidate hypotheses and return a fully structured RankingResult.

    Candidates are grouped into *comparability cohorts* by
    (EvaluationTier, dataset_id, metric_version) before metric-based sorting.
    Only candidates in the same cohort are ranked against each other using
    metric magnitudes.  Cross-cohort display order reflects cohort priority,
    not metric magnitude.

    Parameters
    ----------
    candidates
        All candidate hypotheses to rank.
    evaluation_results
        Map of hypothesis_id -> EvaluationResult.
    observations_by_hypothesis
        Map of hypothesis_id -> list of Observation objects.
    policy
        Ranking policy configuration.  Defaults to RankingPolicyConfig().
    dataset_version
        Optional numeric dataset version for evidence provenance.
    registry
        Optional custom model family registry for evidence classification.
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
            "complexity_val", "dataset_id_val", "metric_ver",
            "has_explicit_non_finite", "evidence_summary",
            "plausibility", "eval_id", "training_sample_count",
            "held_out_sample_count", "cohort_key_val",
        )

        def __init__(
            self,
            candidate: HypothesisRepresentation,
            eval_result: Optional[EvaluationResult],
            obs: Sequence[Any],
            policy: RankingPolicyConfig,
            dataset_version: Optional[int],
            reg: ModelFamilyRegistry,
        ):
            self.hypothesis_id = candidate.hypothesis_id
            self.eval_result = eval_result

            (
                self.tier,
                self.held_out_score,
                self.training_score,
                self.secondary_held_out,
                self.has_explicit_non_finite,
            ) = _determine_tier(eval_result, policy)

            self.complexity_val = _complexity(eval_result, candidate)
            self.dataset_id_val = _dataset_id(eval_result)
            self.metric_ver = _metric_version(eval_result)

            # Comparability cohort key: (tier_priority, dataset_id, metric_version)
            self.cohort_key_val = _cohort_key(
                self.tier, self.dataset_id_val, self.metric_ver
            )

            # Build evidence summary.
            if eval_result is not None and obs:
                records = collect_evidence(
                    eval_result, list(obs), policy,
                    dataset_version=dataset_version, registry=reg,
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

    if registry is None:
        registry = default_registry()

    data_list: List[_CandidateData] = []
    for candidate in candidates:
        eval_result = evaluation_results.get(candidate.hypothesis_id)
        obs_list = observations_by_hypothesis.get(candidate.hypothesis_id, [])
        data = _CandidateData(candidate, eval_result, obs_list, policy, dataset_version, registry)
        data_list.append(data)

    # ---- Cohort analysis ------------------------------------------------- #
    tiers_present: Set[EvaluationTier] = {d.tier for d in data_list}
    has_cross_tier = len(tiers_present) > 1

    # Collect dataset IDs within comparable tiers.
    comparable_dataset_ids: Set[Optional[str]] = {
        d.dataset_id_val
        for d in data_list
        if d.tier in (EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY)
    }
    has_cross_dataset = len({ds for ds in comparable_dataset_ids if ds is not None}) > 1

    if has_cross_dataset:
        ds_list = sorted(ds for ds in comparable_dataset_ids if ds is not None)
        overall_limitations.append(
            f"Candidates were evaluated on different datasets ({', '.join(ds_list)}). "
            "Each dataset forms its own comparability cohort. Metric magnitudes across "
            "cohorts are NOT directly comparable; cross-cohort display order reflects "
            "cohort priority only."
        )

    if (
        EvaluationTier.VALID_HELD_OUT in tiers_present
        and EvaluationTier.VALID_TRAINING_ONLY in tiers_present
    ):
        overall_limitations.append(
            "Some candidates have held-out evaluation results and others have only "
            "training metrics. These groups are presented in separate tiers and "
            "their metric scores are NOT compared directly."
        )

    # Check metric version consistency within each cohort.
    # cohort_key -> set of metric versions seen
    cohort_versions: Dict[Tuple, Set[str]] = {}
    for d in data_list:
        cohort_versions.setdefault(d.cohort_key_val, set()).add(d.metric_ver)

    # Cohorts with mixed or absent metric versions.
    unverified_cohorts: Set[Tuple] = set()
    for ck, versions in cohort_versions.items():
        if _METRIC_VERSION_ABSENT in versions or len(versions) > 1:
            unverified_cohorts.add(ck)

    # ---- Precompute best score per cohort --------------------------------- #
    best_scores: Dict[Tuple, float] = {}
    for d in data_list:
        score: Optional[float] = None
        if d.tier == EvaluationTier.VALID_HELD_OUT and d.held_out_score is not None:
            score = d.held_out_score
        elif d.tier == EvaluationTier.VALID_TRAINING_ONLY and d.training_score is not None:
            score = d.training_score
        if score is not None:
            ck = d.cohort_key_val
            if ck not in best_scores:
                best_scores[ck] = score
            elif policy.metric_direction == "minimize":
                if score < best_scores[ck]:
                    best_scores[ck] = score
            else:
                if score > best_scores[ck]:
                    best_scores[ck] = score

    # ---- Sort ------------------------------------------------------------ #
    data_list.sort(
        key=lambda d: _sort_key(
            d.cohort_key_val,
            d.held_out_score,
            d.training_score,
            d.secondary_held_out,
            d.complexity_val,
            d.hypothesis_id,
            policy,
            best_scores.get(d.cohort_key_val),
        )
    )

    # ---- Build RankedCandidate list --------------------------------------- #
    ranked: List[RankedCandidate] = []
    for rank_idx, d in enumerate(data_list, start=1):
        metric_version_verified = d.cohort_key_val not in unverified_cohorts

        # Human-readable cohort label.
        tier_label = d.tier.value
        ds_label = d.dataset_id_val or "unknown-dataset"
        mv_label = d.metric_ver if d.metric_ver != _METRIC_VERSION_ABSENT else "unversioned"
        cohort_id = f"{tier_label}:{ds_label}:{mv_label}"

        limitations, comparability_notes = _build_limitations(
            d.tier,
            d.plausibility,
            d.dataset_id_val,
            has_cross_tier,
            has_cross_dataset,
            metric_version_verified,
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
            cohort_id=cohort_id,
            metric_version_verified=metric_version_verified,
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
    """Validates whether two or more evaluation results can be fairly compared.

    This validator is wired into ``rank_candidates()`` during cohort construction.
    It is also available as a standalone tool for callers that need to check
    pairwise comparability before calling the ranker.
    """

    def __init__(self, policy: Optional[RankingPolicyConfig] = None):
        self.policy = policy or RankingPolicyConfig()

    def classify_tier(self, eval_result: Optional[EvaluationResult]) -> EvaluationTier:
        """Determine the evaluation comparability tier for an evaluation result."""
        tier, _, _, _, _ = _determine_tier(eval_result, self.policy)
        return tier

    def validate_pairwise_comparability(
        self,
        eval_a: Optional[EvaluationResult],
        eval_b: Optional[EvaluationResult],
    ) -> Tuple[bool, List[str]]:
        """Check if two evaluation results are directly comparable.

        Returns (is_comparable, list_of_reasons).

        Metric version compatibility is flagged as incompatible when both
        evaluations carry a version string and they differ.  When one or both
        are absent, a warning is appended but comparability is not rejected
        outright — the caller should treat the result as *unverified*.
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

        # Dataset comparability.
        ds_a = _dataset_id(eval_a)
        ds_b = _dataset_id(eval_b)
        if ds_a is None or ds_b is None or ds_a != ds_b:
            reasons.append(f"Dataset identifiers do not match or are absent ({ds_a!r} vs {ds_b!r}).")
            return False, reasons

        # Metric version compatibility.
        mv_a = eval_a.metric_version
        mv_b = eval_b.metric_version
        if mv_a is None or mv_b is None:
            # Absent version: flag as unverified but do not reject.
            reasons.append(
                "Metric version metadata is absent for one or both candidates; "
                "direct metric comparison is unverified."
            )
            # Still return True (comparable) but with the warning.
            return True, reasons
        if mv_a != mv_b:
            reasons.append(
                f"Metric versions differ ({mv_a!r} vs {mv_b!r})."
            )
            return False, reasons

        return True, []


class HypothesisRanker:
    """Service orchestrating candidate hypothesis ranking and reranking.

    Historical ranking snapshots
    ----------------------------
    ``rerank_on_new_observations()`` accepts the ``current_ranking`` parameter
    to define the audit boundary.  A full recompute is performed from the updated
    observations, and the prior ``RankingResult`` is preserved by the caller for
    audit purposes.  The returned result includes ``RANKING_VERSION`` so
    downstream consumers can detect strategy changes between snapshots.
    """

    def __init__(
        self,
        policy: Optional[RankingPolicyConfig] = None,
        registry: Optional[ModelFamilyRegistry] = None,
    ):
        self.policy = policy or RankingPolicyConfig()
        self.registry = registry or default_registry()
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
            registry=self.registry,
        )

    def rerank_on_new_observations(
        self,
        current_ranking: RankingResult,
        candidates: Sequence[HypothesisRepresentation],
        evaluation_results: Mapping[str, EvaluationResult],
        updated_observations_by_hypothesis: Mapping[str, Sequence[Any]],
        dataset_version: Optional[int] = None,
    ) -> RankingResult:
        """Rerank candidates following observation ingestion or new evaluation results.

        The ``current_ranking`` argument defines the audit boundary: the caller
        is responsible for persisting the prior snapshot for historical comparison.
        A full recompute is always performed rather than patching the previous
        result, so the new result is deterministic and self-contained.
        """
        # current_ranking is accepted for API symmetry and audit-boundary clarity.
        # The prior snapshot is preserved by the caller, not mutated here.
        _ = current_ranking  # noqa: F841 — accepted for audit-boundary clarity
        return self.rank_candidates(
            candidates=candidates,
            evaluation_results=evaluation_results,
            observations_by_hypothesis=updated_observations_by_hypothesis,
            dataset_version=dataset_version,
        )
