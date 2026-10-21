"""Active experiment selection for CLARITY (Issue #10).

Chooses the next black-box query by comparing the predictions of the currently
eligible hypotheses at every valid candidate experiment. The selector is a pure
domain component: it consumes the candidate batch from Issue #9, the candidate
and fitted model specifications from Issues #5/#6, evaluations and rankings from
Issues #6/#7, and committed observations from Issue #8. It never generates
candidates, refits models, calls an oracle, or reads evaluator-only ground truth.

Selection strategies
--------------------
``ACTIVE_DISAGREEMENT``
    Score each valid candidate by the spread of eligible hypotheses' scalar
    predictions and select the highest-scoring candidate. Ties are broken by the
    canonical input key and then by candidate ID, so the decision does not
    depend on batch order.

``RANDOM_BASELINE``
    Uniformly select one candidate from the same valid candidate pool using a
    local ``random.Random(seed)`` instance. Prediction scores are never used to
    bias the draw. This exists only for reproducible benchmark comparison.

Prediction sources (never mixed)
--------------------------------
Fitted specifications are always used when at least the configured minimum
number of hypotheses have a valid fit; hypotheses without one are excluded with
a diagnostic. Strict mode (the default) never uses unfitted specifications, so
a run must have enough initial observations to fit at least two hypotheses.
Explicitly setting ``allow_candidate_specifications=True`` switches the *whole*
decision to the hypotheses' own validated candidate specifications when too few
fits exist, reporting ``prediction_source="candidate_specification"``. A single
selection never mixes fitted and unfitted predictions.

Uncertainty
-----------
Not available. The fit/evaluation contracts expose no validated
prediction-covariance, residual-variance, or interval estimate, so the result
reports ``uncertainty.available=false`` with a reason. Prediction disagreement
is not statistical uncertainty and is never presented as one.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .contracts import (
    EvaluationResult,
    EvaluationStatus,
    FittingStatus,
    HypothesisRepresentation,
    Observation,
)
from .experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
    CandidateExperimentStatus,
)
from .hypothesis_models import ModelFamilyRegistry, default_registry
from .ranking_contracts import PlausibilityStatus, RankingResult
from .selection_contracts import (
    CandidateScoringStatus,
    CandidateSelectionDiagnostics,
    DisagreementMetric,
    ExperimentSelectionConfig,
    ExperimentSelectionResult,
    SelectionFallback,
    SelectionReason,
    SelectionStatus,
    SelectionStrategy,
    experiment_order_key,
)

PREDICTION_SOURCE_FITTED = "fitted_specification"
PREDICTION_SOURCE_CANDIDATE = "candidate_specification"

# Hypothesis exclusion reason codes (safe, stable, serializable strings).
HYPOTHESIS_REASON_NOT_RANKED = "not_in_ranking"
HYPOTHESIS_REASON_CONTRADICTED = "contradicted_by_evidence"
HYPOTHESIS_REASON_UNEVALUABLE = "unevaluable"
HYPOTHESIS_REASON_NO_EVALUATION = "no_completed_evaluation"
HYPOTHESIS_REASON_EVALUATION_FAILED = "evaluation_failed"
HYPOTHESIS_REASON_NO_FITTED_SPECIFICATION = "no_fitted_specification"

# Prediction exclusion reason codes.
PREDICTION_REASON_UNKNOWN_FAMILY = "unknown_model_family"
PREDICTION_REASON_UNSUPPORTED_OUTPUT = "unsupported_output_shape"
PREDICTION_REASON_NON_FINITE = "non_finite_prediction"
PREDICTION_REASON_FAILED = "prediction_failed"

# Candidate exclusion reason codes.
CANDIDATE_REASON_NOT_ELIGIBLE = "candidate_not_eligible"
CANDIDATE_REASON_PREVIOUSLY_QUERIED = "previously_queried_input"


_EligibleHypothesis = Tuple[HypothesisRepresentation, str, Mapping[str, Any]]


def compute_disagreement(
    predictions: Sequence[float],
    metric: DisagreementMetric = DisagreementMetric.POPULATION_VARIANCE,
) -> Optional[float]:
    """Return the configured disagreement score, or ``None`` on overflow.

    ``None`` means the metric could not be represented as a finite float. It is
    never silently replaced with zero or infinity: an unrepresentable score is
    not evidence of an informative experiment.

    Predictions are scaled by their largest magnitude before squaring so that
    finite inputs near the float limit do not overflow intermediate sums. The
    result is still checked for finiteness.
    """
    if not isinstance(metric, DisagreementMetric):
        raise ValueError("metric must be a DisagreementMetric.")
    values = [float(value) for value in predictions]
    if len(values) < 2:
        return None
    if not all(math.isfinite(value) for value in values):
        return None

    if metric == DisagreementMetric.MAX_SPREAD:
        spread = max(values) - min(values)
        return spread if math.isfinite(spread) else None

    scale = max(abs(value) for value in values)
    if scale == 0.0:
        return 0.0

    scaled = [value / scale for value in values]
    count = len(scaled)
    mean = math.fsum(scaled) / count
    squared_sum = math.fsum((value - mean) ** 2 for value in scaled)
    divisor = count if metric == DisagreementMetric.POPULATION_VARIANCE else count - 1
    scaled_variance = squared_sum / divisor
    variance = scaled_variance * scale * scale
    return variance if math.isfinite(variance) else None


def _predict_scalar(
    registry: ModelFamilyRegistry,
    family_id: str,
    specification: Mapping[str, Any],
    input_values: Mapping[str, Any],
) -> Tuple[Optional[float], Optional[str]]:
    """Predict one finite scalar, converting expected failures to reason codes.

    The model family contract reports malformed specifications and non-finite
    predictions as structured errors. Arithmetic/type errors raised by a third
    party family are converted here as well; unexpected exceptions are not
    swallowed, so a family contract violation is not hidden as a prediction
    failure.
    """
    family = registry.get(family_id)
    if family is None:
        return None, PREDICTION_REASON_UNKNOWN_FAMILY
    try:
        outputs, error = family.predict(specification, input_values)
    except (ArithmeticError, TypeError, ValueError):
        return None, PREDICTION_REASON_FAILED
    if error is not None:
        return None, error.code or PREDICTION_REASON_FAILED
    if not isinstance(outputs, Mapping) or len(outputs) != 1:
        return None, PREDICTION_REASON_UNSUPPORTED_OUTPUT
    value = next(iter(outputs.values()))
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, PREDICTION_REASON_UNSUPPORTED_OUTPUT
    try:
        numeric = float(value)
    except OverflowError:
        return None, PREDICTION_REASON_NON_FINITE
    if not math.isfinite(numeric):
        return None, PREDICTION_REASON_NON_FINITE
    return numeric, None


def _is_previously_queried(
    input_values: Mapping[str, Any],
    observations: Sequence[Observation],
    tolerance: float,
) -> bool:
    """Match the engine's pure absolute-tolerance duplicate semantics.

    The engine compares the configured domain variable. The selector is
    domain-agnostic, so it compares every common numeric input variable with the
    same ``rel_tol=0.0`` absolute tolerance. For the supported 1-D domain the two
    checks are identical. Candidate input IDs are never used: duplicate identity
    is scientific (input values), not bookkeeping.
    """
    for observation in observations:
        existing = observation.input_values
        if not isinstance(existing, Mapping):
            continue
        common = set(input_values).intersection(existing)
        if not common:
            continue
        matches = True
        for name in common:
            candidate_value = input_values.get(name)
            existing_value = existing.get(name)
            if isinstance(candidate_value, bool) or not isinstance(
                candidate_value, (int, float)
            ):
                matches = False
                break
            if isinstance(existing_value, bool) or not isinstance(
                existing_value, (int, float)
            ):
                matches = False
                break
            try:
                if not math.isclose(
                    float(candidate_value),
                    float(existing_value),
                    rel_tol=0.0,
                    abs_tol=tolerance,
                ):
                    matches = False
                    break
            except OverflowError:
                matches = False
                break
        if matches:
            return True
    return False


class ActiveExperimentSelector:
    """Deterministic, diagnostics-first experiment selector (Issue #10).

    The selector is stateless between calls: configuration and the model-family
    registry are the only stored state, and every decision is returned in the
    result. Repeated calls with identical effective inputs and configuration
    return equivalent decisions and diagnostics.
    """

    selection_version = "1.0.0"

    def __init__(
        self,
        config: Optional[ExperimentSelectionConfig] = None,
        *,
        registry: Optional[ModelFamilyRegistry] = None,
    ):
        self._config = config if config is not None else ExperimentSelectionConfig()
        if not isinstance(self._config, ExperimentSelectionConfig):
            raise ValueError("config must be an ExperimentSelectionConfig.")
        self._registry = registry if registry is not None else default_registry()

    @property
    def config(self) -> ExperimentSelectionConfig:
        return self._config

    def select(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult] = None,
    ) -> ExperimentSelectionResult:
        """Select one valid candidate and return the full decision diagnostics."""
        if not isinstance(candidate_batch, CandidateExperimentBatch):
            raise ValueError("candidate_batch must be a CandidateExperimentBatch.")
        if not isinstance(evaluations, Mapping):
            raise ValueError("evaluations must be a mapping of hypothesis ID to result.")
        if ranking is not None and not isinstance(ranking, RankingResult):
            raise ValueError("ranking must be a RankingResult or None.")
        observations = tuple(observations)
        pool = list(candidate_batch.candidates)

        # 1. Validate the pool: respect candidate status, never resurrect a
        #    candidate the shared contract marked non-eligible.
        excluded_candidates: Dict[str, str] = {}
        valid: List[CandidateExperiment] = []
        for candidate in pool:
            if not isinstance(candidate, CandidateExperiment):
                excluded_candidates[str(getattr(candidate, "candidate_id", "unknown"))] = (
                    CANDIDATE_REASON_NOT_ELIGIBLE
                )
                continue
            if candidate.status != CandidateExperimentStatus.ELIGIBLE:
                excluded_candidates[candidate.candidate_id] = CANDIDATE_REASON_NOT_ELIGIBLE
                continue
            valid.append(candidate)

        if not pool:
            return self._no_selection(
                SelectionReason.EMPTY_CANDIDATE_POOL,
                pool_size=0,
                valid_count=0,
                excluded_candidates=excluded_candidates,
                rationale="The candidate batch contained no experiments, so there "
                "was nothing to score or select.",
            )
        if not valid:
            return self._no_selection(
                SelectionReason.NO_ELIGIBLE_CANDIDATES,
                pool_size=len(pool),
                valid_count=0,
                excluded_candidates=excluded_candidates,
                rationale=(
                    f"All {len(pool)} candidate experiment(s) were excluded by the "
                    "shared candidate contract (status is not 'eligible')."
                ),
            )

        # 2. Exclude previously queried inputs, using the documented tolerance.
        if self._config.exclude_queried_inputs:
            remaining: List[CandidateExperiment] = []
            for candidate in valid:
                if _is_previously_queried(
                    candidate.input_data.values,
                    observations,
                    float(self._config.exclusion_tolerance),
                ):
                    excluded_candidates[candidate.candidate_id] = (
                        CANDIDATE_REASON_PREVIOUSLY_QUERIED
                    )
                else:
                    remaining.append(candidate)
            valid = remaining
            if not valid:
                return self._no_selection(
                    SelectionReason.ALL_CANDIDATES_PREVIOUSLY_QUERIED,
                    pool_size=len(pool),
                    valid_count=0,
                    excluded_candidates=excluded_candidates,
                    rationale=(
                        "Every eligible candidate input was already queried under the "
                        f"configured absolute tolerance of {self._config.exclusion_tolerance}."
                    ),
                )

        # 3. Canonical ordering makes both strategies order-independent.
        valid.sort(
            key=lambda candidate: (
                experiment_order_key(candidate.input_data.values),
                candidate.candidate_id,
            )
        )

        if self._config.strategy == SelectionStrategy.RANDOM_BASELINE:
            return self._random_selection(pool, valid, excluded_candidates)

        return self._active_selection(
            candidates, evaluations, pool, valid, ranking, excluded_candidates
        )

    # ------------------------------------------------------------------
    # Active disagreement
    # ------------------------------------------------------------------

    def _active_selection(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        pool: Sequence[CandidateExperiment],
        valid: Sequence[CandidateExperiment],
        ranking: Optional[RankingResult],
        excluded_candidates: Mapping[str, str],
    ) -> ExperimentSelectionResult:
        prediction_source = self._prediction_source(candidates, evaluations)
        eligible, excluded_hypotheses = self._eligible_hypotheses(
            candidates, evaluations, ranking, prediction_source
        )

        if len(eligible) < self._config.min_eligible_hypotheses:
            return self._no_selection(
                SelectionReason.INSUFFICIENT_HYPOTHESES,
                pool_size=len(pool),
                valid_count=len(valid),
                excluded_candidates=excluded_candidates,
                excluded_hypotheses=excluded_hypotheses,
                eligible_hypothesis_ids=tuple(item[0].hypothesis_id for item in eligible),
                prediction_source=prediction_source,
                valid_candidates=valid,
                rationale=(
                    f"Only {len(eligible)} eligible hypothesis(es) with usable "
                    f"{prediction_source} predictions were available, below the "
                    f"configured minimum of {self._config.min_eligible_hypotheses}; "
                    "disagreement cannot be measured from a single competing "
                    "prediction."
                ),
            )

        diagnostics: List[CandidateSelectionDiagnostics] = []
        for candidate in valid:
            usable: Dict[str, float] = {}
            failed: Dict[str, str] = {}
            for hypothesis, family_id, specification in eligible:
                prediction, reason = _predict_scalar(
                    self._registry,
                    family_id,
                    specification,
                    candidate.input_data.values,
                )
                if reason is not None:
                    failed[hypothesis.hypothesis_id] = reason
                else:
                    usable[hypothesis.hypothesis_id] = prediction

            scoring_status = CandidateScoringStatus.SCORED
            score: Optional[float] = None
            if not usable:
                scoring_status = CandidateScoringStatus.NO_USABLE_PREDICTIONS
            elif len(usable) < self._config.min_eligible_hypotheses:
                scoring_status = CandidateScoringStatus.INSUFFICIENT_PREDICTIONS
            else:
                score = compute_disagreement(
                    [usable[key] for key in sorted(usable)], self._config.disagreement_metric
                )
                if score is None:
                    scoring_status = CandidateScoringStatus.DISAGREEMENT_OVERFLOW

            diagnostics.append(
                CandidateSelectionDiagnostics(
                    candidate_id=candidate.candidate_id,
                    input_id=candidate.input_data.input_id,
                    input_values=dict(candidate.input_data.values),
                    scoring_status=scoring_status,
                    disagreement_score=score,
                    usable_predictions=usable,
                    excluded_predictions=failed,
                    order_key=experiment_order_key(candidate.input_data.values),
                )
            )

        scored = [
            diagnostic
            for diagnostic in diagnostics
            if diagnostic.scoring_status == CandidateScoringStatus.SCORED
        ]
        if not scored:
            statuses = {diagnostic.scoring_status for diagnostic in diagnostics}
            if statuses == {CandidateScoringStatus.DISAGREEMENT_OVERFLOW}:
                reason = SelectionReason.DISAGREEMENT_OVERFLOW
                rationale = (
                    "Every candidate had at least the minimum number of usable "
                    "predictions, but the disagreement metric overflowed the finite "
                    "float range; an unrepresentable score is not evidence of an "
                    "informative experiment."
                )
            elif CandidateScoringStatus.INSUFFICIENT_PREDICTIONS in statuses:
                reason = SelectionReason.INSUFFICIENT_HYPOTHESES
                rationale = (
                    "No candidate produced usable predictions from at least "
                    f"{self._config.min_eligible_hypotheses} eligible hypotheses."
                )
            else:
                reason = SelectionReason.NO_USABLE_PREDICTIONS
                rationale = (
                    "No candidate produced any usable finite prediction from the "
                    "eligible hypotheses."
                )
            return self._no_selection(
                reason,
                pool_size=len(pool),
                valid_count=len(valid),
                excluded_candidates=excluded_candidates,
                excluded_hypotheses=excluded_hypotheses,
                eligible_hypothesis_ids=tuple(item[0].hypothesis_id for item in eligible),
                prediction_source=prediction_source,
                candidates=tuple(diagnostics),
                valid_candidates=valid,
                rationale=rationale,
            )

        maximum = max(diagnostic.disagreement_score for diagnostic in scored)
        if maximum <= 0.0:
            return self._no_selection(
                SelectionReason.ZERO_DISAGREEMENT_POOL,
                pool_size=len(pool),
                valid_count=len(valid),
                excluded_candidates=excluded_candidates,
                excluded_hypotheses=excluded_hypotheses,
                eligible_hypothesis_ids=tuple(item[0].hypothesis_id for item in eligible),
                prediction_source=prediction_source,
                candidates=tuple(diagnostics),
                valid_candidates=valid,
                rationale=(
                    "Every scorable candidate had zero disagreement across the "
                    "eligible hypotheses, so no proposed experiment separates them. "
                    "A zero score is never treated as informativeness."
                ),
            )

        tied = sorted(
            (diagnostic for diagnostic in scored if diagnostic.disagreement_score == maximum),
            key=lambda diagnostic: (diagnostic.order_key, diagnostic.candidate_id),
        )
        selected_diagnostic = tied[0]
        by_id = {candidate.candidate_id: candidate for candidate in valid}
        selected = by_id[selected_diagnostic.candidate_id]

        metric_name = self._config.disagreement_metric.value
        rationale = (
            f"Selected candidate {selected.candidate_id} at "
            f"{dict(selected.input_data.values)} with the maximum {metric_name} "
            f"disagreement score ({maximum!r}) across {len(eligible)} eligible "
            f"hypothesis(es); {len(tied)} candidate(s) shared that score and the "
            "canonical input key/candidate ID tie-break selected this one."
            if len(tied) > 1
            else f"Selected candidate {selected.candidate_id} at "
            f"{dict(selected.input_data.values)} with the maximum {metric_name} "
            f"disagreement score ({maximum!r}) across {len(eligible)} eligible "
            "hypothesis(es)."
        )
        return ExperimentSelectionResult(
            status=SelectionStatus.SELECTED,
            strategy=SelectionStrategy.ACTIVE_DISAGREEMENT,
            reason_code=SelectionReason.ACTIVE_DISAGREEMENT_MAX,
            rationale=rationale,
            config=self._config,
            candidate_pool_size=len(pool),
            valid_candidate_count=len(valid),
            eligible_hypothesis_ids=tuple(item[0].hypothesis_id for item in eligible),
            excluded_hypotheses=excluded_hypotheses,
            excluded_candidates=excluded_candidates,
            candidates=tuple(diagnostics),
            selected_candidate=selected,
            tied_candidate_ids=tuple(diagnostic.candidate_id for diagnostic in tied),
            prediction_source=prediction_source,
        )

    def _prediction_source(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
    ) -> str:
        """Choose one uniform prediction source for the whole selection.

        Fitted specifications are used whenever at least the configured minimum
        number of hypotheses has a successful fit. Otherwise explicit
        ``allow_candidate_specifications`` may switch the whole decision to
        candidate specifications; the default stays in strict fitted mode so
        the selector reports insufficient hypotheses instead of comparing
        fitted and unfitted predictions.
        """
        fitted_count = 0
        for candidate in candidates:
            evaluation = evaluations.get(candidate.hypothesis_id)
            if evaluation is None:
                continue
            if (
                evaluation.status == EvaluationStatus.COMPLETED
                and evaluation.fitting_status == FittingStatus.SUCCESS
                and evaluation.fitted_specification is not None
            ):
                fitted_count += 1
        if (
            fitted_count < self._config.min_eligible_hypotheses
            and self._config.allow_candidate_specifications
        ):
            return PREDICTION_SOURCE_CANDIDATE
        return PREDICTION_SOURCE_FITTED

    def _eligible_hypotheses(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        ranking: Optional[RankingResult],
        prediction_source: str,
    ) -> Tuple[List[_EligibleHypothesis], Dict[str, str]]:
        """Apply the documented eligibility policy.

        Eligibility combines the ranking's scientific status with evaluation
        availability and prediction-source availability:

        - ``CONTRADICTED`` hypotheses are no longer competing explanations.
        - ``UNEVALUABLE`` hypotheses (evaluator-side failure) contribute no
          trustworthy prediction.
        - When a ranking is supplied, every hypothesis must appear in it.
        - In fitted mode, a COMPLETED evaluation with a successful fit and a
          fitted specification is required.
        - In candidate mode, the hypothesis' own validated specification is used.

        A ranking position is never treated as truth; it only excludes
        contradicted/unevaluable candidates.
        """
        ranking_by_id = {}
        if ranking is not None:
            ranking_by_id = {
                ranked.hypothesis_id: ranked for ranked in ranking.ranked_candidates
            }

        eligible: List[_EligibleHypothesis] = []
        excluded: Dict[str, str] = {}
        for candidate in candidates:
            hypothesis_id = candidate.hypothesis_id
            ranked = None
            if ranking is not None:
                ranked = ranking_by_id.get(hypothesis_id)
                if ranked is None:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_NOT_RANKED
                    continue
                if ranked.status == PlausibilityStatus.CONTRADICTED:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_CONTRADICTED
                    continue
                if ranked.status == PlausibilityStatus.UNEVALUABLE:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_UNEVALUABLE
                    continue

            if prediction_source == PREDICTION_SOURCE_FITTED:
                evaluation = evaluations.get(hypothesis_id)
                if evaluation is None:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_NO_EVALUATION
                    continue
                if evaluation.status == EvaluationStatus.FAILED:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_EVALUATION_FAILED
                    continue
                if evaluation.status != EvaluationStatus.COMPLETED:
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_NO_EVALUATION
                    continue
                if (
                    evaluation.fitting_status != FittingStatus.SUCCESS
                    or evaluation.fitted_specification is None
                ):
                    excluded[hypothesis_id] = HYPOTHESIS_REASON_NO_FITTED_SPECIFICATION
                    continue
                eligible.append(
                    (
                        candidate,
                        evaluation.model_family or candidate.model_family,
                        evaluation.fitted_specification,
                    )
                )
            else:
                eligible.append(
                    (candidate, candidate.model_family, candidate.parameters)
                )
        return eligible, excluded

    # ------------------------------------------------------------------
    # Random baseline
    # ------------------------------------------------------------------

    def _random_selection(
        self,
        pool: Sequence[CandidateExperiment],
        valid: Sequence[CandidateExperiment],
        excluded_candidates: Mapping[str, str],
    ) -> ExperimentSelectionResult:
        """Uniform draw from the already-validated, canonically ordered pool."""
        seed = self._config.random_seed
        assert seed is not None  # enforced by config validation
        diagnostics = tuple(
            CandidateSelectionDiagnostics(
                candidate_id=candidate.candidate_id,
                input_id=candidate.input_data.input_id,
                input_values=dict(candidate.input_data.values),
                scoring_status=CandidateScoringStatus.NOT_SCORED_RANDOM_BASELINE,
                disagreement_score=None,
                order_key=experiment_order_key(candidate.input_data.values),
            )
            for candidate in valid
        )
        rng = random.Random(seed)
        index = rng.randrange(len(valid))
        selected = valid[index]
        return ExperimentSelectionResult(
            status=SelectionStatus.SELECTED,
            strategy=SelectionStrategy.RANDOM_BASELINE,
            reason_code=SelectionReason.RANDOM_BASELINE_DRAW,
            rationale=(
                f"Random baseline drew candidate {selected.candidate_id} at "
                f"{dict(selected.input_data.values)} (index {index} of {len(valid)}) "
                f"from the same valid candidate pool using seed {seed}. No "
                "disagreement score influenced this choice."
            ),
            config=self._config,
            candidate_pool_size=len(pool),
            valid_candidate_count=len(valid),
            excluded_candidates=excluded_candidates,
            candidates=diagnostics,
            selected_candidate=selected,
            random_seed=seed,
        )

    # ------------------------------------------------------------------
    # Result construction
    # ------------------------------------------------------------------

    def _no_selection(
        self,
        reason: SelectionReason,
        *,
        pool_size: int,
        valid_count: int,
        rationale: str,
        excluded_candidates: Mapping[str, str],
        excluded_hypotheses: Optional[Mapping[str, str]] = None,
        eligible_hypothesis_ids: Tuple[str, ...] = (),
        prediction_source: Optional[str] = None,
        candidates: Tuple[CandidateSelectionDiagnostics, ...] = (),
        valid_candidates: Sequence[CandidateExperiment] = (),
    ) -> ExperimentSelectionResult:
        if (
            self._config.fallback_strategy == SelectionFallback.SEQUENTIAL
            and valid_candidates
        ):
            # Explicit opt-in only. The fallback is deterministic bookkeeping and
            # is reported as such; it is never reported as disagreement-driven.
            ordered = sorted(
                valid_candidates,
                key=lambda candidate: (
                    experiment_order_key(candidate.input_data.values),
                    candidate.candidate_id,
                ),
            )
            if ordered:
                selected = ordered[0]
                return ExperimentSelectionResult(
                    status=SelectionStatus.FALLBACK_SELECTED,
                    strategy=self._config.strategy,
                    reason_code=SelectionReason.SEQUENTIAL_FALLBACK,
                    rationale=(
                        f"Active selection produced no informative candidate "
                        f"({reason.value}); the explicitly configured sequential "
                        f"fallback selected the first valid candidate "
                        f"{selected.candidate_id} at {dict(selected.input_data.values)}. "
                        "This is not disagreement-driven selection."
                    ),
                    config=self._config,
                    candidate_pool_size=pool_size,
                    valid_candidate_count=valid_count,
                    eligible_hypothesis_ids=eligible_hypothesis_ids,
                    excluded_hypotheses=excluded_hypotheses or {},
                    excluded_candidates=excluded_candidates,
                    candidates=candidates,
                    selected_candidate=selected,
                    prediction_source=prediction_source,
                )

        return ExperimentSelectionResult(
            status=SelectionStatus.NO_SELECTION,
            strategy=self._config.strategy,
            reason_code=reason,
            rationale=rationale,
            config=self._config,
            candidate_pool_size=pool_size,
            valid_candidate_count=valid_count,
            eligible_hypothesis_ids=eligible_hypothesis_ids,
            excluded_hypotheses=excluded_hypotheses or {},
            excluded_candidates=excluded_candidates,
            candidates=candidates,
            prediction_source=prediction_source,
            random_seed=(
                self._config.random_seed
                if self._config.strategy == SelectionStrategy.RANDOM_BASELINE
                else None
            ),
        )


class ExperimentSelectionPlanner:
    """Adapter exposing active selection through the Issue #8 planner protocol.

    ``select_experiment`` keeps the existing ``CandidateExperiment | None``
    contract, so the discovery engine accepts this planner unchanged.
    ``select_experiment_with_diagnostics`` returns the full structured result;
    the engine uses it when available and records the serialized diagnostics in
    experiment history.
    """

    planner_id = "experiment_selection_planner"

    def __init__(
        self,
        config: Optional[ExperimentSelectionConfig] = None,
        *,
        selector: Optional[ActiveExperimentSelector] = None,
        registry: Optional[ModelFamilyRegistry] = None,
    ):
        if selector is not None and config is not None:
            raise ValueError("Pass either config or selector, not both.")
        self._selector = selector or ActiveExperimentSelector(config, registry=registry)

    @property
    def config(self) -> ExperimentSelectionConfig:
        return self._selector.config

    def select_experiment(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> Optional[CandidateExperiment]:
        """Issue #8-compatible planner call: return only the selected candidate."""
        return self.select_experiment_with_diagnostics(
            candidates, evaluations, observations, candidate_batch, ranking
        ).selected_candidate

    def select_experiment_with_diagnostics(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> ExperimentSelectionResult:
        return self._selector.select(
            candidates, evaluations, observations, candidate_batch, ranking
        )


class RandomBaselinePlanner(ExperimentSelectionPlanner):
    """Reproducible uniform-random planner for active-versus-random benchmarks."""

    planner_id = "random_baseline_planner"

    def __init__(
        self,
        seed: int,
        *,
        registry: Optional[ModelFamilyRegistry] = None,
    ):
        super().__init__(
            ExperimentSelectionConfig(
                strategy=SelectionStrategy.RANDOM_BASELINE,
                random_seed=seed,
            ),
            registry=registry,
        )


__all__ = [
    "PREDICTION_SOURCE_FITTED",
    "PREDICTION_SOURCE_CANDIDATE",
    "HYPOTHESIS_REASON_NOT_RANKED",
    "HYPOTHESIS_REASON_CONTRADICTED",
    "HYPOTHESIS_REASON_UNEVALUABLE",
    "HYPOTHESIS_REASON_NO_EVALUATION",
    "HYPOTHESIS_REASON_EVALUATION_FAILED",
    "HYPOTHESIS_REASON_NO_FITTED_SPECIFICATION",
    "PREDICTION_REASON_UNKNOWN_FAMILY",
    "PREDICTION_REASON_UNSUPPORTED_OUTPUT",
    "PREDICTION_REASON_NON_FINITE",
    "PREDICTION_REASON_FAILED",
    "CANDIDATE_REASON_NOT_ELIGIBLE",
    "CANDIDATE_REASON_PREVIOUSLY_QUERIED",
    "compute_disagreement",
    "ActiveExperimentSelector",
    "ExperimentSelectionPlanner",
    "RandomBaselinePlanner",
]
