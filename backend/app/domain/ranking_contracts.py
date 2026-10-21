"""Ranking contracts and data structures for CLARITY Issue #7.

Defines the pure-domain data types used by the evidence tracker and ranking
engine.  No database access, oracle calls, or model fitting occurs here.

Key design decisions:
- Evidence identity is (hypothesis_id, observation_id, partition).
  This key is stable across evaluation revisions, so re-processing the same
  observation is always idempotent.
- Evaluation provenance (evaluation_id, dataset_version, evaluated_at) is
  recorded alongside the evidence record so audit trails are preserved without
  creating duplicate evidence entries.
- Ranking uses a tiered transitive total ordering.  Candidates that cannot be
  compared (different datasets, missing metrics, non-finite values) are assigned
  to separate presentation tiers rather than given fabricated default scores.
- Evidence counts do NOT participate in metric sorting.  Evidence establishes
  PlausibilityStatus and traces why a candidate holds its rank; it does not
  double-count observations already synthesised into MAE/RMSE.
- No confidence percentages, probability-of-correctness claims, or hidden-rule
  knowledge are produced here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

from .contracts import generate_id, utc_now_iso


# ==============================================================================
# 1. Enumeration Types
# ==============================================================================

class PlausibilityStatus(str, Enum):
    """Scientific plausibility state of a candidate hypothesis.

    Conditions (configured by RankingPolicyConfig):

    UNEVALUATED
        No EvaluationResult exists for this candidate yet (status is PENDING or
        the candidate has never been submitted to the evaluator).

    UNEVALUABLE
        An EvaluationResult exists but its status is FAILED due to a fitter /
        evaluator failure (e.g. unregistered model family, malformed parameters,
        numerical divergence).  This is an evaluator-side failure, NOT a
        scientific contradiction.

    UNCERTAIN
        A valid (COMPLETED) EvaluationResult exists but either:
          - accepted_sample_count < RankingPolicyConfig.min_samples_for_plausibility, or
          - both supporting and contradicting evidence exist without a decisive
            contradiction count, or
          - one or more evidence records are INDETERMINATE (non-finite values).

    PLAUSIBLE
        A valid COMPLETED EvaluationResult exists, accepted_sample_count >=
        min_samples_for_plausibility, and contradicting_count <=
        max_tolerated_contradictions.

    CONTRADICTED
        A valid COMPLETED EvaluationResult exists and contradicting_count >
        max_tolerated_contradictions.
    """

    UNEVALUATED = "unevaluated"
    UNEVALUABLE = "unevaluable"
    UNCERTAIN = "uncertain"
    PLAUSIBLE = "plausible"
    CONTRADICTED = "contradicted"


class EvidenceClassification(str, Enum):
    """Classification of one observation-prediction comparison.

    Tolerance applied: T = tau_abs + tau_rel * |y_obs|

    SUPPORTING     |y_obs - y_hat| <= T  (and both values are finite)
    CONTRADICTING  |y_obs - y_hat| >  T  (and both values are finite)
    INDETERMINATE  y_obs or y_hat is None, non-finite (NaN/Inf), or the
                   residual computation itself overflowed/raised.
    """

    SUPPORTING = "supporting"
    CONTRADICTING = "contradicting"
    INDETERMINATE = "indeterminate"


class EvaluationTier(str, Enum):
    """Presentation tier that groups candidates by comparability.

    Only candidates in the same tier with the same dataset_id are directly
    comparable.  Cross-tier comparisons are disclosed in limitation notes.

    VALID_HELD_OUT
        EvaluationResult is COMPLETED with finite held_out_metrics on the
        primary metric.

    VALID_TRAINING_ONLY
        EvaluationResult is COMPLETED but held_out_metrics is absent or
        the primary metric is missing; training metrics are available.

    INCOMPARABLE
        EvaluationResult is COMPLETED but the primary metric is non-finite
        (NaN/Inf), the dataset_id is absent, or the metric definition is
        incompatible.

    EVALUATION_FAILED
        EvaluationResult exists but status is FAILED.

    UNEVALUATED
        No EvaluationResult is available.
    """

    VALID_HELD_OUT = "valid_held_out"
    VALID_TRAINING_ONLY = "valid_training_only"
    INCOMPARABLE = "incomparable"
    EVALUATION_FAILED = "evaluation_failed"
    UNEVALUATED = "unevaluated"


# ==============================================================================
# 2. Evidence Record and Summary
# ==============================================================================

@dataclass(frozen=True)
class EvidenceRecord:
    """Immutable record of one observation compared against one hypothesis prediction.

    Identity key
    ------------
    evidence_id is deterministically derived from (hypothesis_id, observation_id,
    partition) and is stable across evaluation revisions.  Callers must use
    EvidenceRecord.make_evidence_id() to construct it.

    Provenance
    ----------
    evaluation_id and dataset_version record which evaluation run produced or
    verified this evidence, without affecting idempotency.  When the same
    observation is re-processed under a new evaluation revision the evidence_id
    remains identical, but the provenance fields are updated in the EvidenceStore.
    This preserves audit trails without duplicating observation-level facts.

    Fields
    ------
    evidence_id         Deterministic key: "evi-{hypothesis_id}-{observation_id}-{partition}".
    hypothesis_id       Candidate being compared.
    observation_id      Source observation ID from Observation.observation_id.
    partition           "training" or "held_out" — the evaluation data partition.
    classification      SUPPORTING, CONTRADICTING, or INDETERMINATE.
    observed_value      Float from Observation.output_values[output_variable].
                        None when the output is absent or non-numeric.
    predicted_value     Float from family.predict() for this observation's input.
                        None when prediction could not be produced.
    residual            |observed_value - predicted_value| when both are finite.
                        None otherwise.
    tolerance           Applied threshold T = tau_abs + tau_rel * |observed_value|
                        when computed; None when classification is INDETERMINATE.
    is_held_out         True when the observation was in the held-out partition.
    dataset_id          Dataset identifier from EvaluationResult.dataset_ids.
    evaluation_id       EvaluationResult.evaluation_id that produced this record.
    dataset_version     Numeric dataset version at evaluation time, when available.
    timestamp           UTC ISO-8601 timestamp when this record was created.
    membership_verified Whether this observation's partition membership was confirmed
                        against the evaluation's accepted observation-ID lists.
                        False when the evaluation carries no membership lists (absent
                        dataset_ids.observation_ids / held_out_observation_ids) or when
                        the observation ID is absent from the applicable list.
                        Classification is still computed from residuals for display, but
                        status derivation treats unverified records as uncertain.
    """

    evidence_id: str
    hypothesis_id: str
    observation_id: str
    partition: str           # "training" | "held_out"
    classification: EvidenceClassification
    observed_value: Optional[float]
    predicted_value: Optional[float]
    residual: Optional[float]
    tolerance: Optional[float]
    is_held_out: bool
    dataset_id: Optional[str]
    evaluation_id: str
    dataset_version: Optional[int]
    timestamp: str

    experiment_id: Optional[str] = None
    membership_verified: bool = True

    @staticmethod
    def make_evidence_id(
        hypothesis_id: str,
        observation_id: str,
        partition: str,
    ) -> str:
        """Deterministic, stable identity key for an observation-hypothesis pair."""
        return f"evi-{hypothesis_id}-{observation_id}-{partition}"

    def __post_init__(self) -> None:
        expected_id = EvidenceRecord.make_evidence_id(
            self.hypothesis_id, self.observation_id, self.partition
        )
        if self.evidence_id != expected_id:
            raise ValueError(
                f"evidence_id {self.evidence_id!r} does not match expected "
                f"{expected_id!r} for (hypothesis_id={self.hypothesis_id!r}, "
                f"observation_id={self.observation_id!r}, partition={self.partition!r})."
            )
        if self.partition not in ("training", "held_out"):
            raise ValueError(
                f"partition must be 'training' or 'held_out', got {self.partition!r}."
            )

    def to_dict(self) -> Dict:
        return {
            "evidence_id": self.evidence_id,
            "hypothesis_id": self.hypothesis_id,
            "observation_id": self.observation_id,
            "experiment_id": self.experiment_id,
            "partition": self.partition,
            "classification": self.classification.value,
            "observed_value": self.observed_value,
            "predicted_value": self.predicted_value,
            "residual": self.residual,
            "tolerance": self.tolerance,
            "is_held_out": self.is_held_out,
            "dataset_id": self.dataset_id,
            "evaluation_id": self.evaluation_id,
            "dataset_version": self.dataset_version,
            "timestamp": self.timestamp,
            "membership_verified": self.membership_verified,
        }


@dataclass(frozen=True)
class EvidenceSummary:
    """Aggregate summary of all evidence records for one hypothesis.

    Supporting and contradicting observation ID lists are preserved separately
    so conflicts are never silently merged into a net count.  This allows
    downstream consumers to see that a candidate has both supporting and
    contradicting evidence simultaneously.

    Fields
    ------
    hypothesis_id               Candidate this summary belongs to.
    supporting_count            Number of unique supporting observations.
    contradicting_count         Number of unique contradicting observations.
    indeterminate_count         Number of indeterminate observations.
    total_evaluated             Total observations for which evidence exists.
    supporting_observation_ids  Stable tuple of supporting observation IDs.
    contradicting_observation_ids
                                Stable tuple of contradicting observation IDs.
    indeterminate_observation_ids
                                Stable tuple of indeterminate observation IDs.
    unverified_membership_count Number of observations whose partition membership
                                could not be confirmed against the evaluation's accepted
                                ID lists.  Status derivation treats these as uncertain
                                evidence: a candidate cannot be PLAUSIBLE when this
                                count is > 0, because the evaluation population is
                                unknown.  Defaults to 0 for backwards compatibility
                                when constructing summaries directly (e.g. in tests).
    """

    hypothesis_id: str
    supporting_count: int
    contradicting_count: int
    indeterminate_count: int
    total_evaluated: int
    supporting_observation_ids: Tuple[str, ...]
    contradicting_observation_ids: Tuple[str, ...]
    indeterminate_observation_ids: Tuple[str, ...]
    unverified_membership_count: int = 0

    @property
    def total_observations(self) -> int:
        """Alias for total_evaluated."""
        return self.total_evaluated

    def to_dict(self) -> Dict:
        return {
            "hypothesis_id": self.hypothesis_id,
            "supporting_count": self.supporting_count,
            "contradicting_count": self.contradicting_count,
            "indeterminate_count": self.indeterminate_count,
            "total_evaluated": self.total_evaluated,
            "total_observations": self.total_evaluated,
            "supporting_observation_ids": list(self.supporting_observation_ids),
            "contradicting_observation_ids": list(self.contradicting_observation_ids),
            "indeterminate_observation_ids": list(self.indeterminate_observation_ids),
            "unverified_membership_count": self.unverified_membership_count,
        }


# ==============================================================================
# 3. Ranking Policy Configuration
# ==============================================================================

@dataclass(frozen=True)
class RankingPolicyConfig:
    """Configurable scientific policy for ranking and evidence classification.

    All thresholds carry justified defaults based on the evaluation contracts.

    Fields
    ------
    primary_metric          Name of the metric used for primary ranking comparisons.
                            Must match a key in EvaluationResult.held_out_metrics or
                            EvaluationResult.metrics.  Default: "mae".

    secondary_metric        Tie-breaking metric when primary scores are within the
                            complexity window.  Default: "rmse".

    metric_direction        "minimize" (lower is better, e.g. MAE/RMSE) or "maximize"
                            (higher is better).  Default: "minimize".

    complexity_window       Relative threshold within which primary metric values are
                            considered "comparable" and complexity tie-breaking applies.
                            |m1 - m2| / max(max(|m1|, |m2|), 1e-9) <= complexity_window.
                            Default: 0.01 (1% relative difference).

    tolerance_absolute      Absolute residual tolerance tau_abs for evidence
                            classification.  Default: 0.05.

    tolerance_relative      Relative residual tolerance tau_rel.  Threshold becomes
                            T = tau_abs + tau_rel * |y_obs|.  Default: 0.01.

    min_samples_for_plausibility
                            Minimum number of accepted training observations required
                            before a candidate can be PLAUSIBLE rather than UNCERTAIN.
                            Default: 5.  Justification: a polynomial of degree up to
                            DEFAULT_MAX_POLYNOMIAL_DEGREE requires at least degree+1
                            points; 5 provides one degree of freedom beyond the
                            maximum default degree (4).

    max_tolerated_contradictions
                            Number of contradicting observations tolerated before
                            status becomes CONTRADICTED.  Default: 0 (strict:
                            any validated contradiction triggers CONTRADICTED).
                            Set > 0 for noisy or bounded-error environments.

    performance_acceptance_threshold
                            Task-specific upper (minimize) or lower (maximize) bound
                            on the primary metric that a candidate must meet for its
                            status to be PLAUSIBLE.  When None (the default), a
                            finite metric is necessary but not sufficient: no
                            threshold has been agreed, so the status remains UNCERTAIN
                            even when all other criteria pass.  This is intentional:
                            PLAUSIBLE must mean "meets an agreed performance criterion",
                            not merely "has a finite error and enough samples".

                            Set an explicit value that is appropriate for the task
                            (e.g. 0.10 for MAE meaning "error below 10 units is
                            acceptable") before relying on PLAUSIBLE status.
    """

    primary_metric: str = "mae"
    secondary_metric: str = "rmse"
    metric_direction: str = "minimize"
    complexity_window: float = 0.01
    tolerance_absolute: float = 0.05
    tolerance_relative: float = 0.01
    min_samples_for_plausibility: int = 5
    max_tolerated_contradictions: int = 0
    complexity_penalty_threshold: Optional[float] = None
    performance_acceptance_threshold: Optional[float] = None
    allow_unverified_comparability: bool = False

    def __post_init__(self) -> None:
        if self.complexity_penalty_threshold is not None:
            object.__setattr__(self, "complexity_window", self.complexity_penalty_threshold)
        if self.metric_direction not in ("minimize", "maximize"):
            raise ValueError(
                f"metric_direction must be 'minimize' or 'maximize', "
                f"got {self.metric_direction!r}."
            )
        if self.complexity_window < 0.0:
            raise ValueError("complexity_window must be >= 0.")
        if self.tolerance_absolute < 0.0:
            raise ValueError("tolerance_absolute must be >= 0.")
        if self.tolerance_relative < 0.0:
            raise ValueError("tolerance_relative must be >= 0.")
        if self.min_samples_for_plausibility < 1:
            raise ValueError("min_samples_for_plausibility must be >= 1.")
        if self.max_tolerated_contradictions < 0:
            raise ValueError("max_tolerated_contradictions must be >= 0.")

    def metric_meets_acceptance(self, metric_value: float) -> bool:
        """Return True when metric_value satisfies the performance acceptance policy.

        When performance_acceptance_threshold is None, returns False: no task-specific
        acceptance criterion has been configured, so performance cannot be asserted
        as acceptable, and plausibility status must remain UNCERTAIN.
        """
        if metric_value is None or not math.isfinite(metric_value):
            return False
        if self.performance_acceptance_threshold is None:
            return False
        if self.metric_direction == "minimize":
            return metric_value <= self.performance_acceptance_threshold
        return metric_value >= self.performance_acceptance_threshold

    def is_better(self, score_a: float, score_b: float) -> bool:
        """Return True if score_a is strictly better than score_b under metric_direction."""
        if self.metric_direction == "minimize":
            return score_a < score_b
        return score_a > score_b

    def is_comparable_within_window(self, score_a: float, score_b: float) -> bool:
        """Return True when scores are within the relative complexity window."""
        denom = max(abs(score_a), abs(score_b), 1e-9)
        return abs(score_a - score_b) / denom <= self.complexity_window


# ==============================================================================
# 4. Ranked Candidate and Result Output
# ==============================================================================

@dataclass(frozen=True)
class RankedCandidate:
    """Comprehensive ranking result for one candidate hypothesis.

    All displayed scores and evidence summaries are derived from actual
    EvaluationResult and EvidenceRecord fields.  No synthetic metrics or
    fabricated confidence percentages appear here.

    Fields
    ------
    rank                    1-based display position across ALL candidates (global).
                            This is a presentation order number, not a scientific
                            rank claim across cohorts.  Use within_cohort_rank for
                            scientifically meaningful ordering.
    within_cohort_rank      1-based rank within the candidate's comparability cohort
                            (same tier + dataset_id + metric_version + metric_def).
                            None when the candidate is in a non-rankable tier
                            (INCOMPARABLE, EVALUATION_FAILED, UNEVALUATED).
    hypothesis_id           Candidate identifier.
    status                  Scientific plausibility status.
    evaluation_tier         Comparability tier (determines which candidates are
                            directly comparable to this one).
    primary_metric_name     Name of the metric used to rank within this tier.
    held_out_score          Primary metric value from held_out_metrics, if available.
    training_score          Primary metric value from training metrics, if available.
    secondary_held_out_score
                            Secondary metric value from held_out_metrics, if available.
    complexity              Complexity from HypothesisRepresentation or EvaluationResult.
    held_out_sample_count   Number of held-out observations accepted, if available.
    training_sample_count   Number of training observations accepted.
    evidence_summary        Structured summary of supporting/contradicting evidence.
    dataset_id              Dataset identifier used for this evaluation.
    evaluation_id           Evaluation execution ID from EvaluationResult.
    explanation             Deterministic human-readable explanation of why this
                            candidate has this display position and status.
    limitations             Tuple of known limitations for this candidate's ranking.
    comparability_notes     Notes explaining why comparisons against other tiers
                            are qualified.
    """

    rank: int
    hypothesis_id: str
    status: PlausibilityStatus
    evaluation_tier: EvaluationTier
    primary_metric_name: str
    held_out_score: Optional[float]
    training_score: Optional[float]
    secondary_held_out_score: Optional[float]
    complexity: Optional[int]
    held_out_sample_count: Optional[int]
    training_sample_count: int
    evidence_summary: EvidenceSummary
    dataset_id: Optional[str]
    evaluation_id: Optional[str]
    explanation: str
    limitations: Tuple[str, ...]
    comparability_notes: Tuple[str, ...]
    within_cohort_rank: Optional[int] = None

    def to_dict(self) -> Dict:
        return {
            "rank": self.rank,
            "within_cohort_rank": self.within_cohort_rank,
            "hypothesis_id": self.hypothesis_id,
            "status": self.status.value,
            "evaluation_tier": self.evaluation_tier.value,
            "primary_metric_name": self.primary_metric_name,
            "held_out_score": self.held_out_score,
            "training_score": self.training_score,
            "secondary_held_out_score": self.secondary_held_out_score,
            "complexity": self.complexity,
            "held_out_sample_count": self.held_out_sample_count,
            "training_sample_count": self.training_sample_count,
            "evidence_summary": self.evidence_summary.to_dict(),
            "dataset_id": self.dataset_id,
            "evaluation_id": self.evaluation_id,
            "explanation": self.explanation,
            "limitations": list(self.limitations),
            "comparability_notes": list(self.comparability_notes),
        }


@dataclass(frozen=True)
class RankingResult:
    """Complete ordered ranking result for a set of candidate hypotheses.

    Fields
    ------
    ranked_candidates   Ordered tuple of RankedCandidate from rank 1 downward.
    policy_config       The RankingPolicyConfig that produced this result.
    ranking_version     Version string for the ranking strategy.
    ranked_at           UTC ISO-8601 timestamp of when ranking was computed.
    limitations         Overall limitations of this ranking run.
    """

    ranked_candidates: Tuple[RankedCandidate, ...]
    policy_config: RankingPolicyConfig
    ranking_version: str
    ranked_at: str
    limitations: Tuple[str, ...]

    def to_dict(self) -> Dict:
        return {
            "ranked_candidates": [rc.to_dict() for rc in self.ranked_candidates],
            "ranking_version": self.ranking_version,
            "ranked_at": self.ranked_at,
            "limitations": list(self.limitations),
        }


# Alias for callers referring to cohorts
RankingCohort = RankingResult
