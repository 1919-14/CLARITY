"""Hypothesis ranking engine for CLARITY Issue #7.

Comparability cohorts
---------------------
Candidates are grouped into *comparability cohorts* keyed by:
  (EvaluationTier, dataset_id, metric_version, metric_def)

where metric_def is the definition string of the primary metric extracted from
``EvaluationResult.metric_definitions`` (Finding #5).  Candidates sharing a
cohort key have identical evaluation protocol and can be ranked by metric
magnitude.  Cross-cohort ordering uses cohort priority only, never raw metric
magnitudes.

Missing dataset identity (Finding #2)
--------------------------------------
``_determine_tier()`` can return VALID_HELD_OUT or VALID_TRAINING_ONLY based
solely on metric availability.  After tier determination, ``_CandidateData``
overrides the tier to INCOMPARABLE and clears scores when ``dataset_id`` is
None for a comparable tier.  This prevents unknown-dataset candidates from
being grouped together and compared by metric magnitude.  A policy override
is not provided: the dataset_id must be explicitly present to permit metric
comparison.

Display position vs. within-cohort rank (Finding #3)
------------------------------------------------------
``RankedCandidate.rank`` is a **display position** (global 1-based integer,
presentation order only).  ``RankedCandidate.within_cohort_rank`` is the
**scientific rank** within the candidate's comparability cohort (1-based, or
None when the candidate is in a non-rankable tier such as INCOMPARABLE,
EVALUATION_FAILED, or UNEVALUATED).

API consumers must use ``within_cohort_rank`` for any scientific ordering
claim.  ``rank`` is a convenience display number only.

Metric definition comparability (Finding #5)
---------------------------------------------
``EvaluationResult.metric_definitions`` is consulted to extract the primary
metric's definition string.  Two candidates with the same metric_version but
different definition strings for the primary metric are placed in different
cohorts and cannot be compared by magnitude.  When metric_definitions is
absent for a candidate, the definition sentinel ``"__undefined__"`` is used,
which forms its own cohort separate from any versioned definition.
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

RANKING_VERSION = "1.2.0"

_TIER_PRIORITY: Dict[EvaluationTier, int] = {
    EvaluationTier.VALID_HELD_OUT: 0,
    EvaluationTier.VALID_TRAINING_ONLY: 1,
    EvaluationTier.INCOMPARABLE: 2,
    EvaluationTier.EVALUATION_FAILED: 3,
    EvaluationTier.UNEVALUATED: 4,
}

_METRIC_VERSION_ABSENT = "__absent__"
_METRIC_DEF_ABSENT = "__undefined__"

# Tiers eligible for within-cohort metric ranking.
_RANKABLE_TIERS = frozenset({EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_finite_metric(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def _get_finite_metric(
    metrics: Optional[Dict[str, Optional[float]]],
    metric_name: str,
) -> Tuple[Optional[float], bool]:
    """Return (finite_value_or_None, explicitly_non_finite_flag)."""
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
    """Classify a candidate into an EvaluationTier and extract scores.

    Returns
    -------
    (tier, held_out_score, training_score, secondary_held_out_score,
     has_explicit_non_finite)

    Note: this function does NOT check dataset_id.  Missing dataset identity
    is enforced in ``_CandidateData.__init__`` after tier determination.
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

    if has_explicit_non_finite:
        return EvaluationTier.INCOMPARABLE, None, training_val, None, True

    # Finding #2: Missing dataset identity prevents direct metric ranking.
    ds = _dataset_id(eval_result)
    if ds is None:
        return EvaluationTier.INCOMPARABLE, None, None, None, False

    if held_out_val is not None:
        return EvaluationTier.VALID_HELD_OUT, held_out_val, training_val, secondary_val, False
    if training_val is not None:
        return EvaluationTier.VALID_TRAINING_ONLY, None, training_val, None, False
    return EvaluationTier.INCOMPARABLE, None, None, None, False


def _dataset_id(eval_result: Optional[EvaluationResult]) -> Optional[str]:
    if eval_result is None or eval_result.dataset_ids is None:
        return None
    ds = eval_result.dataset_ids.get("dataset_id")
    if isinstance(ds, str) and ds:
        return ds
    return None


def _metric_version(eval_result: Optional[EvaluationResult]) -> str:
    if eval_result is None or eval_result.metric_version is None:
        return _METRIC_VERSION_ABSENT
    return eval_result.metric_version


def _primary_metric_def(
    eval_result: Optional[EvaluationResult],
    policy: RankingPolicyConfig,
) -> str:
    """Extract the definition string for the primary metric (Finding #5).

    When metric_definitions is absent or the primary metric key is missing,
    returns the ``_METRIC_DEF_ABSENT`` sentinel so candidates with undefined
    definitions form their own cohort, separate from any with explicit
    definitions.
    """
    if eval_result is None or eval_result.metric_definitions is None:
        return _METRIC_DEF_ABSENT
    defn = eval_result.metric_definitions.get(policy.primary_metric)
    if defn is None:
        return _METRIC_DEF_ABSENT
    return str(defn)


def _complexity(
    eval_result: Optional[EvaluationResult],
    candidate: Optional[HypothesisRepresentation],
) -> Optional[int]:
    if eval_result is not None and eval_result.complexity is not None:
        return eval_result.complexity
    if candidate is not None:
        return candidate.complexity
    return None


def _cohort_key(
    tier: EvaluationTier,
    dataset_id: Optional[str],
    metric_ver: str,
    metric_def: str,
) -> Tuple[int, Optional[str], str, str]:
    """Compute the cohort grouping key.

    Only VALID_HELD_OUT and VALID_TRAINING_ONLY candidates participate in
    metric-magnitude comparisons.  Other tiers use None dataset_id and empty
    version strings, forming a single non-metric presentation group per tier.
    """
    if tier in _RANKABLE_TIERS:
        return (_TIER_PRIORITY[tier], dataset_id, metric_ver, metric_def)
    return (_TIER_PRIORITY[tier], None, "", "")


def _sort_key(
    cohort_key: Tuple[int, Optional[str], str, str],
    held_out_score: Optional[float],
    training_score: Optional[float],
    secondary_held_out: Optional[float],
    complexity_val: Optional[int],
    hypothesis_id: str,
    policy: RankingPolicyConfig,
    best_score_in_cohort: Optional[float] = None,
) -> tuple:
    """Produce a sortable key (ascending = better rank)."""
    tier_priority, ds, mv, md = cohort_key
    ds_sentinel = ds if ds is not None else ""

    active_score: Optional[float] = None
    if tier_priority == _TIER_PRIORITY[EvaluationTier.VALID_HELD_OUT]:
        active_score = held_out_score
    elif tier_priority == _TIER_PRIORITY[EvaluationTier.VALID_TRAINING_ONLY]:
        active_score = training_score

    if active_score is None:
        return (tier_priority, ds_sentinel, mv, md, 0, 0, 0.0, 0.0, hypothesis_id)

    direction = 1.0 if policy.metric_direction == "minimize" else -1.0
    metric_sort = direction * active_score

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

    return (tier_priority, ds_sentinel, mv, md, band, complexity_sort, metric_sort, secondary_sort, hypothesis_id)


def _build_explanation(
    hypothesis_id: str,
    tier: EvaluationTier,
    held_out_score: Optional[float],
    training_score: Optional[float],
    secondary_held_out: Optional[float],
    complexity_val: Optional[int],
    display_position: int,
    within_cohort_rank: Optional[int],
    cohort_size: int,
    status: PlausibilityStatus,
    policy: RankingPolicyConfig,
    contradicting_count: int,
    supporting_count: int,
    indeterminate_count: int,
    unverified_membership_count: int,
    sample_count: int,
    held_out_sample_count: Optional[int],
    dataset_id: Optional[str],
    evaluation_id: Optional[str],
    cohort_id: str,
    metric_version_verified: bool,
    metric_def_verified: bool,
) -> str:
    """Build a deterministic, human-readable ranking explanation."""
    if within_cohort_rank is not None:
        rank_line = (
            f"Display position {display_position} | "
            f"Within-cohort scientific rank {within_cohort_rank} of {cohort_size}"
        )
    else:
        rank_line = (
            f"Display position {display_position} | "
            f"Within-cohort scientific rank: N/A (non-rankable tier)"
        )

    lines = [
        f"{rank_line} | {hypothesis_id} | Status: {status.value} | Tier: {tier.value}"
    ]

    if evaluation_id:
        lines.append(f"Evaluation: {evaluation_id}")
    if dataset_id:
        lines.append(f"Dataset: {dataset_id}")
    lines.append(f"Cohort: {cohort_id}")

    if not metric_version_verified:
        lines.append(
            "Metric version: absent or mixed in this cohort; "
            "direct metric comparison may be invalid."
        )
    if not metric_def_verified:
        lines.append(
            "Metric definition: absent or mixed in this cohort; "
            "candidates may not share the same metric computation."
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
            "Display position in INCOMPARABLE tier: evaluation completed but the "
            f"primary metric ({policy.primary_metric}) is absent, non-finite, or "
            "the dataset identity is missing. No metric-based ordering applied."
        )
    elif tier == EvaluationTier.EVALUATION_FAILED:
        lines.append("Display position in EVALUATION_FAILED tier: evaluation did not complete.")
    elif tier == EvaluationTier.UNEVALUATED:
        lines.append("Display position in UNEVALUATED tier: no evaluation result is available.")

    if complexity_val is not None:
        lines.append(f"Complexity: {complexity_val}.")

    lines.append(
        f"Evidence: {supporting_count} supporting, {contradicting_count} contradicting, "
        f"{indeterminate_count} indeterminate, {unverified_membership_count} unverified-membership."
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
                f"(minimum for PLAUSIBLE: {policy.min_samples_for_plausibility})."
            )
        elif unverified_membership_count > 0:
            lines.append(
                f"Status UNCERTAIN: {unverified_membership_count} observation(s) have "
                "unverified partition membership — evaluation population cannot be confirmed."
            )
        elif indeterminate_count > 0:
            lines.append(
                f"Status UNCERTAIN: {indeterminate_count} observation(s) yielded "
                "INDETERMINATE evidence."
            )
        elif policy.performance_acceptance_threshold is None:
            lines.append(
                "Status UNCERTAIN: no performance_acceptance_threshold configured; "
                "acceptable predictive performance cannot be asserted without an "
                "agreed task-specific criterion."
            )
        else:
            lines.append(
                "Status UNCERTAIN: primary metric does not meet the configured "
                f"performance_acceptance_threshold ({policy.performance_acceptance_threshold})."
            )

    return " | ".join(lines)


def _build_limitations(
    tier: EvaluationTier,
    status: PlausibilityStatus,
    dataset_id: Optional[str],
    has_cross_tier_peers: bool,
    has_cross_dataset_peers: bool,
    metric_version_verified: bool,
    metric_def_verified: bool,
) -> Tuple[tuple, tuple]:
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
        if dataset_id is None:
            limitations.append(
                "Dataset identifier absent: metric comparison with other candidates "
                "is not permitted without a confirmed shared evaluation identity."
            )
        else:
            limitations.append(
                "Primary metric is absent or non-finite; no metric-based ordering was applied."
            )

    if tier in (EvaluationTier.EVALUATION_FAILED, EvaluationTier.UNEVALUATED):
        limitations.append(
            "No valid evaluation result: metric-based ranking is not possible."
        )

    if status == PlausibilityStatus.CONTRADICTED:
        limitations.append(
            "Candidate has contradicting observations: it should not be treated as "
            "scientifically validated despite its display position."
        )

    if status == PlausibilityStatus.UNEVALUABLE:
        limitations.append(
            "Evaluation failed: the fitting or metric pipeline reported an error."
        )

    if status == PlausibilityStatus.UNCERTAIN and tier in _RANKABLE_TIERS:
        limitations.append(
            "Status UNCERTAIN: PLAUSIBLE requires an explicit performance_acceptance_threshold "
            "to be configured in RankingPolicyConfig, sufficient verified observations, "
            "no INDETERMINATE evidence, and the metric meeting that threshold."
        )

    if not metric_version_verified and tier in _RANKABLE_TIERS:
        limitations.append(
            "Metric version metadata is absent or mixed in this cohort; "
            "direct metric comparison may be invalid."
        )

    if not metric_def_verified and tier in _RANKABLE_TIERS:
        limitations.append(
            "Metric definition metadata is absent or mixed in this cohort; "
            "candidates may not share the same metric computation."
        )

    if has_cross_tier_peers:
        comparability_notes.append(
            "Other candidates are in different evaluation tiers. "
            "Cross-tier display position reflects tier priority, not metric magnitude."
        )

    if has_cross_dataset_peers:
        comparability_notes.append(
            "Other candidates were evaluated on different datasets. "
            "Each dataset forms its own comparability cohort; display positions across "
            "cohorts are NOT scientifically comparable metric orderings."
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

    See module docstring for details on cohorts, display position, and
    within-cohort rank semantics.
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
            "complexity_val", "dataset_id_val", "metric_ver", "metric_def_val",
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
                raw_tier,
                raw_held_out,
                raw_training,
                raw_secondary,
                self.has_explicit_non_finite,
            ) = _determine_tier(eval_result, policy)

            self.dataset_id_val = _dataset_id(eval_result)
            self.metric_ver = _metric_version(eval_result)
            self.metric_def_val = _primary_metric_def(eval_result, policy)

            # Finding #2: override tier to INCOMPARABLE when dataset_id is None
            # for a comparable tier.  Metric comparison requires a confirmed
            # shared evaluation identity.
            if raw_tier in _RANKABLE_TIERS and self.dataset_id_val is None:
                self.tier = EvaluationTier.INCOMPARABLE
                self.held_out_score = None
                self.training_score = None
                self.secondary_held_out = None
            else:
                self.tier = raw_tier
                self.held_out_score = raw_held_out
                self.training_score = raw_training
                self.secondary_held_out = raw_secondary

            self.complexity_val = _complexity(eval_result, candidate)
            self.cohort_key_val = _cohort_key(
                self.tier, self.dataset_id_val,
                self.metric_ver, self.metric_def_val,
            )

            # Evidence.
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

    comparable_dataset_ids: Set[Optional[str]] = {
        d.dataset_id_val
        for d in data_list
        if d.tier in _RANKABLE_TIERS
    }
    has_cross_dataset = len({ds for ds in comparable_dataset_ids if ds is not None}) > 1

    if has_cross_dataset:
        ds_list = sorted(ds for ds in comparable_dataset_ids if ds is not None)
        overall_limitations.append(
            f"Candidates were evaluated on different datasets ({', '.join(ds_list)}). "
            "Each dataset forms its own comparability cohort. Metric magnitudes across "
            "cohorts are NOT directly comparable; cross-cohort display position reflects "
            "cohort priority only."
        )

    if (
        EvaluationTier.VALID_HELD_OUT in tiers_present
        and EvaluationTier.VALID_TRAINING_ONLY in tiers_present
    ):
        overall_limitations.append(
            "Some candidates have held-out evaluation results and others have only "
            "training metrics. These groups are in separate tiers and their metric "
            "scores are NOT compared directly."
        )

    # Cohort-level verification flags.
    cohort_versions: Dict[Tuple, Set[str]] = {}
    cohort_defs: Dict[Tuple, Set[str]] = {}
    for d in data_list:
        cohort_versions.setdefault(d.cohort_key_val, set()).add(d.metric_ver)
        cohort_defs.setdefault(d.cohort_key_val, set()).add(d.metric_def_val)

    unverified_version_cohorts: Set[Tuple] = set()
    unverified_def_cohorts: Set[Tuple] = set()
    for ck, versions in cohort_versions.items():
        if _METRIC_VERSION_ABSENT in versions or len(versions) > 1:
            unverified_version_cohorts.add(ck)
    for ck, defs in cohort_defs.items():
        if _METRIC_DEF_ABSENT in defs or len(defs) > 1:
            unverified_def_cohorts.add(ck)

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

    # ---- Compute within-cohort ranks ------------------------------------- #
    # Count each cohort separately; only rankable tiers get a within-cohort rank.
    cohort_rank_counters: Dict[Tuple, int] = {}
    within_cohort_ranks: List[Optional[int]] = []
    cohort_sizes: Dict[Tuple, int] = {}

    # First pass: count cohort sizes for rankable tiers.
    for d in data_list:
        if d.tier in _RANKABLE_TIERS:
            cohort_sizes[d.cohort_key_val] = cohort_sizes.get(d.cohort_key_val, 0) + 1

    # Second pass: assign within-cohort ranks in display order.
    for d in data_list:
        if d.tier in _RANKABLE_TIERS:
            cohort_rank_counters[d.cohort_key_val] = cohort_rank_counters.get(d.cohort_key_val, 0) + 1
            within_cohort_ranks.append(cohort_rank_counters[d.cohort_key_val])
        else:
            within_cohort_ranks.append(None)

    # ---- Build RankedCandidate list --------------------------------------- #
    ranked: List[RankedCandidate] = []
    for display_pos, (d, wc_rank) in enumerate(zip(data_list, within_cohort_ranks), start=1):
        metric_version_verified = d.cohort_key_val not in unverified_version_cohorts
        metric_def_verified = d.cohort_key_val not in unverified_def_cohorts

        tier_label = d.tier.value
        ds_label = d.dataset_id_val or "unknown-dataset"
        mv_label = d.metric_ver if d.metric_ver != _METRIC_VERSION_ABSENT else "unversioned"
        md_label = d.metric_def_val if d.metric_def_val != _METRIC_DEF_ABSENT else "undefined"
        cohort_id = f"{tier_label}:{ds_label}:{mv_label}:{md_label}"

        cohort_sz = cohort_sizes.get(d.cohort_key_val, 1)

        limitations, comparability_notes = _build_limitations(
            d.tier,
            d.plausibility,
            d.dataset_id_val,
            has_cross_tier,
            has_cross_dataset,
            metric_version_verified,
            metric_def_verified,
        )
        explanation = _build_explanation(
            hypothesis_id=d.hypothesis_id,
            tier=d.tier,
            held_out_score=d.held_out_score,
            training_score=d.training_score,
            secondary_held_out=d.secondary_held_out,
            complexity_val=d.complexity_val,
            display_position=display_pos,
            within_cohort_rank=wc_rank,
            cohort_size=cohort_sz,
            status=d.plausibility,
            policy=policy,
            contradicting_count=d.evidence_summary.contradicting_count,
            supporting_count=d.evidence_summary.supporting_count,
            indeterminate_count=d.evidence_summary.indeterminate_count,
            unverified_membership_count=d.evidence_summary.unverified_membership_count,
            sample_count=d.training_sample_count,
            held_out_sample_count=d.held_out_sample_count,
            dataset_id=d.dataset_id_val,
            evaluation_id=d.eval_id,
            cohort_id=cohort_id,
            metric_version_verified=metric_version_verified,
            metric_def_verified=metric_def_verified,
        )

        rc = RankedCandidate(
            rank=display_pos,
            within_cohort_rank=wc_rank,
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
    """Validates whether two evaluation results can be fairly compared.

    Checks: tier match, dataset_id match, metric_version compatibility,
    and primary metric_definition compatibility (Finding #5).
    """

    def __init__(self, policy: Optional[RankingPolicyConfig] = None):
        self.policy = policy or RankingPolicyConfig()

    def classify_tier(self, eval_result: Optional[EvaluationResult]) -> EvaluationTier:
        tier, _, _, _, _ = _determine_tier(eval_result, self.policy)
        if tier in _RANKABLE_TIERS and _dataset_id(eval_result) is None:
            return EvaluationTier.INCOMPARABLE
        return tier

    def validate_pairwise_comparability(
        self,
        eval_a: Optional[EvaluationResult],
        eval_b: Optional[EvaluationResult],
    ) -> Tuple[bool, List[str]]:
        """Check if two evaluation results are directly comparable.

        Returns (is_comparable, list_of_reasons).  Absent metric_version or
        metric_definition is flagged as a warning (comparable=True with reason)
        rather than an outright rejection, unless the values conflict.
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

        if tier_a not in _RANKABLE_TIERS:
            reasons.append(f"Tier {tier_a.value} is not a directly comparable evaluation tier.")
            return False, reasons

        # Dataset comparability.
        ds_a = _dataset_id(eval_a)
        ds_b = _dataset_id(eval_b)
        if ds_a is None or ds_b is None or ds_a != ds_b:
            reasons.append(
                f"Dataset identifiers do not match or are absent ({ds_a!r} vs {ds_b!r})."
            )
            return False, reasons

        # Metric version.
        mv_a = eval_a.metric_version
        mv_b = eval_b.metric_version
        if mv_a is None or mv_b is None:
            reasons.append(
                "Metric version metadata is absent for one or both candidates; "
                "direct metric comparison is unverified."
            )
            # Return True (potentially comparable) with the warning.
        elif mv_a != mv_b:
            reasons.append(f"Metric versions differ ({mv_a!r} vs {mv_b!r}).")
            return False, reasons

        # Primary metric definition (Finding #5).
        def_a = _primary_metric_def(eval_a, self.policy)
        def_b = _primary_metric_def(eval_b, self.policy)
        if def_a != _METRIC_DEF_ABSENT and def_b != _METRIC_DEF_ABSENT:
            if def_a != def_b:
                reasons.append(
                    f"Primary metric definitions differ for '{self.policy.primary_metric}': "
                    f"{def_a!r} vs {def_b!r}."
                )
                return False, reasons
        elif (def_a == _METRIC_DEF_ABSENT) != (def_b == _METRIC_DEF_ABSENT):
            reasons.append(
                f"Primary metric definition ('{self.policy.primary_metric}') is absent "
                "for one candidate but present for the other; direct comparison is unverified."
            )
            # Return True with the warning if other checks pass.

        return True, reasons


class HypothesisRanker:
    """Service orchestrating candidate hypothesis ranking and reranking."""

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
        """Rerank after observation ingestion.

        ``current_ranking`` defines the audit boundary; the caller preserves
        the prior snapshot.  A full recompute is always performed.
        """
        _ = current_ranking  # noqa: F841
        return self.rank_candidates(
            candidates=candidates,
            evaluation_results=evaluation_results,
            observations_by_hypothesis=updated_observations_by_hypothesis,
            dataset_version=dataset_version,
        )
