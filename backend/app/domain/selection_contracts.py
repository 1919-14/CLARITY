"""Contracts for active experiment selection (Issue #10).

Defines the pure-domain data types used by the experiment selector: selection
strategy and disagreement-metric identifiers, validated selection configuration,
per-candidate diagnostics, and the structured selection result.

Nothing here fits models, generates candidates, calls an oracle, or reads
evaluator-only ground truth. These are plain, JSON-serializable contracts so a
selection decision can be stored in run history and reviewed later.

Scientific scope
----------------
Only scalar numerical predictions are supported. Disagreement is *not*
statistical uncertainty, a posterior, or a probability of success: it is the
spread of the competing hypotheses' predictions at a candidate input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Tuple

from .contracts import freeze_payload, thaw_payload, verify_no_ground_truth_leakage
from .experiment_generator import CandidateExperiment

SELECTION_VERSION = "1.0.0"

TIE_BREAK_POLICY = (
    "highest disagreement score, then lexicographically smallest canonical input "
    "key (numeric values ascending), then smallest candidate_id"
)

UNCERTAINTY_UNAVAILABLE_REASON = (
    "The fit and evaluation contracts expose no validated prediction-covariance, "
    "residual-variance, or interval estimate, so uncertainty is reported as "
    "unavailable. Prediction spread between hypotheses is not calibrated "
    "statistical uncertainty."
)


class SelectionStrategy(str, Enum):
    """Supported selection strategies.

    ACTIVE_DISAGREEMENT
        Score every valid candidate by the spread of eligible hypotheses'
        predictions and select the maximum-scoring candidate.

    RANDOM_BASELINE
        Uniformly select one candidate from the same valid candidate pool using
        a local seeded RNG. This is a benchmarking baseline, never an
        intelligent strategy, and it does not use disagreement scores.
    """

    ACTIVE_DISAGREEMENT = "active_disagreement"
    RANDOM_BASELINE = "random_baseline"


class DisagreementMetric(str, Enum):
    """Supported disagreement metrics for scalar numerical predictions.

    For usable predictions ``p_1..p_n`` with mean ``p_bar``:

    POPULATION_VARIANCE  D = (1/n) * sum((p_i - p_bar)^2)
    SAMPLE_VARIANCE      D = (1/(n-1)) * sum((p_i - p_bar)^2)   (n >= 2)
    MAX_SPREAD           D = max(p_i) - min(p_i)

    The sample/population distinction is explicit: population variance divides
    by ``n``, sample variance divides by ``n - 1``. Scores are only comparable
    within one metric; no cross-metric normalization is performed.
    """

    POPULATION_VARIANCE = "population_variance"
    SAMPLE_VARIANCE = "sample_variance"
    MAX_SPREAD = "max_spread"


class SelectionFallback(str, Enum):
    """Explicit fallback for a valid pool with no informative candidate.

    NONE
        Return a structured no-selection result. The default: never invent an
        informative score and never silently switch strategies.

    SEQUENTIAL
        Select the first valid candidate in canonical input order and report
        ``FALLBACK_SELECTED`` with the fallback reason. This is deterministic
        bookkeeping, not disagreement-driven selection.
    """

    NONE = "none"
    SEQUENTIAL = "sequential"


class SelectionStatus(str, Enum):
    """Outcome of one selection decision."""

    SELECTED = "selected"
    FALLBACK_SELECTED = "fallback_selected"
    NO_SELECTION = "no_selection"


class SelectionReason(str, Enum):
    """Machine-readable reason for the selection status.

    ``SELECTED`` uses ``ACTIVE_DISAGREEMENT_MAX`` or ``RANDOM_BASELINE_DRAW``.
    ``FALLBACK_SELECTED`` uses ``SEQUENTIAL_FALLBACK``. ``NO_SELECTION`` uses one
    of the remaining values, so an empty pool is never conflated with a valid
    but uninformative pool.
    """

    ACTIVE_DISAGREEMENT_MAX = "active_disagreement_max"
    RANDOM_BASELINE_DRAW = "random_baseline_draw"
    SEQUENTIAL_FALLBACK = "sequential_fallback"
    EMPTY_CANDIDATE_POOL = "empty_candidate_pool"
    NO_ELIGIBLE_CANDIDATES = "no_eligible_candidates"
    ALL_CANDIDATES_PREVIOUSLY_QUERIED = "all_candidates_previously_queried"
    INSUFFICIENT_HYPOTHESES = "insufficient_eligible_hypotheses"
    NO_USABLE_PREDICTIONS = "no_usable_predictions"
    DISAGREEMENT_OVERFLOW = "disagreement_overflow"
    ZERO_DISAGREEMENT_POOL = "zero_disagreement_pool"


class CandidateScoringStatus(str, Enum):
    """Why a valid candidate does or does not carry a disagreement score."""

    SCORED = "scored"
    NOT_SCORED_RANDOM_BASELINE = "not_scored_random_baseline"
    INSUFFICIENT_PREDICTIONS = "insufficient_predictions"
    NO_USABLE_PREDICTIONS = "no_usable_predictions"
    DISAGREEMENT_OVERFLOW = "disagreement_overflow"


def experiment_order_key(
    input_values: Mapping[str, Any],
) -> Tuple[Tuple[str, int, Any], ...]:
    """Deterministic, type-safe ascending key for canonical input ordering.

    Numbers sort before non-numeric values for the same variable. The tag keeps
    tuple comparison total even for mixed-type values, so ordering never raises
    a raw ``TypeError`` and never depends on insertion or generation order.
    """
    key: list = []
    for name in sorted(input_values):
        value = input_values[name]
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                numeric = float(value)
            except OverflowError:
                key.append((name, 1, repr(value)))
            else:
                if math.isfinite(numeric):
                    key.append((name, 0, numeric))
                else:
                    key.append((name, 1, repr(value)))
        else:
            key.append((name, 1, repr(value)))
    return tuple(key)


@dataclass(frozen=True)
class ExperimentSelectionConfig:
    """Validated configuration for one selection decision.

    Fields
    ------
    strategy
        ``ACTIVE_DISAGREEMENT`` (default) or ``RANDOM_BASELINE``.
    disagreement_metric
        Metric used by active selection. Population variance by default.
    min_eligible_hypotheses
        Minimum number of hypotheses with usable predictions before a candidate
        can be scored. Must be >= 2: disagreement requires at least two
        competing predictions.
    exclude_queried_inputs
        When True (default), candidates matching a committed observation input
        under the pure absolute tolerance are excluded.
    exclusion_tolerance
        Absolute tolerance (``rel_tol=0.0``) used for that comparison, matching
        the discovery engine and Issue #9 generator semantics.
    fallback_strategy
        ``NONE`` (default) or ``SEQUENTIAL``. A random fallback is intentionally
        unsupported. Random baseline never falls back.
    random_seed
        Required integer seed for ``RANDOM_BASELINE`` so the baseline is
        reproducible. Ignored by active selection.
    uncertainty_weight
        Reserved for a future validated uncertainty source. It must currently
        be ``0.0``: no fit contract exposes calibrated prediction uncertainty,
        so a non-zero weight is rejected rather than silently ignored or
        reinterpreted as disagreement.
    allow_candidate_specifications
        Explicit opt-in for bootstrap selection. When at least
        ``min_eligible_hypotheses`` hypotheses have valid fitted
        specifications, fitted specifications are always used. When fewer do,
        this flag switches the *whole* decision to the hypotheses' own
        validated candidate specifications; it never mixes fitted and unfitted
        predictions in one score. Default ``False`` (strict: fitted evidence
        only), so behaviour only becomes permissive when configured.
    """

    strategy: SelectionStrategy = SelectionStrategy.ACTIVE_DISAGREEMENT
    disagreement_metric: DisagreementMetric = DisagreementMetric.POPULATION_VARIANCE
    min_eligible_hypotheses: int = 2
    exclude_queried_inputs: bool = True
    exclusion_tolerance: float = 1e-5
    fallback_strategy: SelectionFallback = SelectionFallback.NONE
    random_seed: Optional[int] = None
    uncertainty_weight: float = 0.0
    allow_candidate_specifications: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.strategy, SelectionStrategy):
            raise ValueError(
                "strategy must be a SelectionStrategy, got "
                f"{type(self.strategy).__name__}."
            )
        if not isinstance(self.disagreement_metric, DisagreementMetric):
            raise ValueError(
                "disagreement_metric must be a DisagreementMetric, got "
                f"{type(self.disagreement_metric).__name__}."
            )
        if (
            isinstance(self.min_eligible_hypotheses, bool)
            or not isinstance(self.min_eligible_hypotheses, int)
        ):
            raise ValueError("min_eligible_hypotheses must be an integer >= 2.")
        if self.min_eligible_hypotheses < 2:
            raise ValueError(
                "min_eligible_hypotheses must be >= 2; disagreement requires at "
                "least two competing hypotheses."
            )
        if not isinstance(self.exclude_queried_inputs, bool):
            raise ValueError("exclude_queried_inputs must be a bool.")
        if (
            isinstance(self.exclusion_tolerance, bool)
            or not isinstance(self.exclusion_tolerance, (int, float))
            or not math.isfinite(float(self.exclusion_tolerance))
            or float(self.exclusion_tolerance) < 0.0
        ):
            raise ValueError("exclusion_tolerance must be a finite number >= 0.")
        if not isinstance(self.fallback_strategy, SelectionFallback):
            raise ValueError(
                "fallback_strategy must be a SelectionFallback, got "
                f"{type(self.fallback_strategy).__name__}."
            )
        if self.random_seed is not None and (
            isinstance(self.random_seed, bool) or not isinstance(self.random_seed, int)
        ):
            raise ValueError("random_seed must be an integer or None.")
        if not isinstance(self.allow_candidate_specifications, bool):
            raise ValueError("allow_candidate_specifications must be a bool.")
        if (
            isinstance(self.uncertainty_weight, bool)
            or not isinstance(self.uncertainty_weight, (int, float))
            or not math.isfinite(float(self.uncertainty_weight))
            or float(self.uncertainty_weight) != 0.0
        ):
            raise ValueError(
                "uncertainty_weight must be 0.0: no validated uncertainty source "
                "is exposed by the fit/evaluation contracts, so uncertainty "
                "weighting is disabled until one is documented and validated."
            )
        if self.strategy == SelectionStrategy.RANDOM_BASELINE:
            if self.random_seed is None:
                raise ValueError(
                    "RANDOM_BASELINE requires an explicit integer random_seed so "
                    "the benchmark baseline is reproducible."
                )
            if self.fallback_strategy != SelectionFallback.NONE:
                raise ValueError(
                    "RANDOM_BASELINE does not take a fallback_strategy; it either "
                    "selects uniformly from the valid pool or reports no selection."
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "strategy": self.strategy.value,
            "disagreement_metric": self.disagreement_metric.value,
            "min_eligible_hypotheses": self.min_eligible_hypotheses,
            "exclude_queried_inputs": self.exclude_queried_inputs,
            "exclusion_tolerance": self.exclusion_tolerance,
            "fallback_strategy": self.fallback_strategy.value,
            "random_seed": self.random_seed,
            "uncertainty_weight": self.uncertainty_weight,
            "allow_candidate_specifications": self.allow_candidate_specifications,
        }


@dataclass(frozen=True)
class CandidateSelectionDiagnostics:
    """Per-candidate scoring evidence for one selection decision.

    Fields
    ------
    candidate_id, input_id, input_values
        Stable identifiers and the proposed input values.
    scoring_status
        Why this candidate has or does not have a score.
    disagreement_score
        Selected metric value, or ``None`` when the candidate was not scored.
    usable_predictions
        ``hypothesis_id -> finite scalar prediction`` actually used in the
        score. Invalid predictions are never zero-filled.
    excluded_predictions
        ``hypothesis_id -> safe reason code`` for predictions that were
        unusable (for example a family prediction error code).
    order_key
        Canonical ascending input key used for deterministic tie-breaking.
    """

    candidate_id: str
    input_id: str
    input_values: Mapping[str, Any]
    scoring_status: CandidateScoringStatus
    disagreement_score: Optional[float]
    usable_predictions: Mapping[str, float] = field(default_factory=dict)
    excluded_predictions: Mapping[str, str] = field(default_factory=dict)
    order_key: Tuple[Tuple[str, int, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.input_values, Mapping):
            raise ValueError("CandidateSelectionDiagnostics input_values must be a mapping.")
        if not isinstance(self.usable_predictions, Mapping):
            raise ValueError("usable_predictions must be a mapping.")
        if not isinstance(self.excluded_predictions, Mapping):
            raise ValueError("excluded_predictions must be a mapping.")
        if self.disagreement_score is not None:
            if not math.isfinite(float(self.disagreement_score)):
                raise ValueError("disagreement_score must be finite or None.")
        if self.scoring_status == CandidateScoringStatus.SCORED and self.disagreement_score is None:
            raise ValueError("SCORED candidate diagnostics require a disagreement_score.")
        object.__setattr__(self, "input_values", freeze_payload(dict(self.input_values)))
        object.__setattr__(
            self, "usable_predictions", freeze_payload(dict(self.usable_predictions))
        )
        object.__setattr__(
            self, "excluded_predictions", freeze_payload(dict(self.excluded_predictions))
        )
        object.__setattr__(self, "order_key", freeze_payload(self.order_key))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "input_id": self.input_id,
            "input_values": thaw_payload(self.input_values),
            "scoring_status": self.scoring_status.value,
            "disagreement_score": self.disagreement_score,
            "usable_predictions": thaw_payload(self.usable_predictions),
            "excluded_predictions": thaw_payload(self.excluded_predictions),
            "usable_prediction_count": len(self.usable_predictions),
            "order_key": [list(item) for item in self.order_key],
        }


@dataclass(frozen=True)
class ExperimentSelectionResult:
    """Structured, auditable outcome of one selection decision.

    The result carries the selected candidate (if any), the strategy and
    configuration used, per-candidate scoring evidence, eligible/excluded
    hypotheses, excluded candidates, the tie-breaking outcome, and the reason.
    It is deterministic for identical effective inputs and configuration: no
    timestamps or generated identifiers are added during selection.

    ``selected_candidate`` is returned for planner interoperability; serialized
    diagnostics expose ``selected_candidate_id`` and ``selected_input_values``
    instead of duplicating the whole candidate payload.
    """

    status: SelectionStatus
    strategy: SelectionStrategy
    reason_code: SelectionReason
    rationale: str
    config: ExperimentSelectionConfig
    candidate_pool_size: int
    valid_candidate_count: int
    eligible_hypothesis_ids: Tuple[str, ...] = ()
    excluded_hypotheses: Mapping[str, str] = field(default_factory=dict)
    excluded_candidates: Mapping[str, str] = field(default_factory=dict)
    candidates: Tuple[CandidateSelectionDiagnostics, ...] = ()
    selected_candidate: Optional[CandidateExperiment] = None
    selected_input_values: Optional[Mapping[str, Any]] = None
    tie_break_policy: str = TIE_BREAK_POLICY
    tied_candidate_ids: Tuple[str, ...] = ()
    prediction_source: Optional[str] = None
    random_seed: Optional[int] = None
    uncertainty_available: bool = False
    uncertainty_source: Optional[str] = None
    uncertainty_unavailable_reason: str = UNCERTAINTY_UNAVAILABLE_REASON
    selection_version: str = SELECTION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.status, SelectionStatus):
            raise ValueError("status must be a SelectionStatus.")
        if not isinstance(self.strategy, SelectionStrategy):
            raise ValueError("strategy must be a SelectionStrategy.")
        if not isinstance(self.reason_code, SelectionReason):
            raise ValueError("reason_code must be a SelectionReason.")
        if not isinstance(self.config, ExperimentSelectionConfig):
            raise ValueError("config must be an ExperimentSelectionConfig.")
        if not isinstance(self.rationale, str) or not self.rationale:
            raise ValueError("rationale must be a non-empty string.")
        if self.candidate_pool_size < 0 or self.valid_candidate_count < 0:
            raise ValueError("candidate counts must be non-negative.")
        if self.valid_candidate_count > self.candidate_pool_size:
            raise ValueError("valid_candidate_count cannot exceed candidate_pool_size.")
        if self.status == SelectionStatus.NO_SELECTION:
            if self.selected_candidate is not None or self.selected_input_values is not None:
                raise ValueError("NO_SELECTION must not carry a selected candidate.")
        else:
            if self.selected_candidate is None:
                raise ValueError(
                    f"{self.status.value} requires a selected candidate."
                )
            if self.selected_input_values is None:
                object.__setattr__(
                    self,
                    "selected_input_values",
                    freeze_payload(dict(self.selected_candidate.input_data.values)),
                )
        if self.selected_input_values is not None:
            object.__setattr__(
                self, "selected_input_values", freeze_payload(dict(self.selected_input_values))
            )
        object.__setattr__(self, "eligible_hypothesis_ids", tuple(self.eligible_hypothesis_ids))
        object.__setattr__(self, "tied_candidate_ids", tuple(self.tied_candidate_ids))
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(
            self, "excluded_hypotheses", freeze_payload(dict(self.excluded_hypotheses))
        )
        object.__setattr__(
            self, "excluded_candidates", freeze_payload(dict(self.excluded_candidates))
        )

    @property
    def selected_candidate_id(self) -> Optional[str]:
        return (
            self.selected_candidate.candidate_id
            if self.selected_candidate is not None
            else None
        )

    @property
    def tie_break_applied(self) -> bool:
        return len(self.tied_candidate_ids) > 1

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a discovery-safe, JSON-compatible dictionary."""
        data: Dict[str, Any] = {
            "selection_version": self.selection_version,
            "status": self.status.value,
            "strategy": self.strategy.value,
            "reason_code": self.reason_code.value,
            "rationale": self.rationale,
            "config": self.config.to_dict(),
            "candidate_pool_size": self.candidate_pool_size,
            "valid_candidate_count": self.valid_candidate_count,
            "eligible_hypothesis_count": len(self.eligible_hypothesis_ids),
            "eligible_hypothesis_ids": list(self.eligible_hypothesis_ids),
            "excluded_hypotheses": thaw_payload(self.excluded_hypotheses),
            "excluded_candidates": thaw_payload(self.excluded_candidates),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selected_candidate_id": self.selected_candidate_id,
            "selected_input_values": thaw_payload(self.selected_input_values),
            "tie_break_policy": self.tie_break_policy,
            "tie_break_applied": self.tie_break_applied,
            "tied_candidate_ids": list(self.tied_candidate_ids),
            "prediction_source": self.prediction_source,
            "random_seed": self.random_seed,
            "uncertainty": {
                "available": self.uncertainty_available,
                "source": self.uncertainty_source,
                "reason": self.uncertainty_unavailable_reason,
            },
        }
        verify_no_ground_truth_leakage(data)
        return data


__all__ = [
    "SELECTION_VERSION",
    "TIE_BREAK_POLICY",
    "UNCERTAINTY_UNAVAILABLE_REASON",
    "SelectionStrategy",
    "DisagreementMetric",
    "SelectionFallback",
    "SelectionStatus",
    "SelectionReason",
    "CandidateScoringStatus",
    "experiment_order_key",
    "ExperimentSelectionConfig",
    "CandidateSelectionDiagnostics",
    "ExperimentSelectionResult",
]
