"""Tests for active experiment selection (Issue #10).

Covers the disagreement metrics, the eligibility policy, deterministic
tie-breaking, duplicate exclusion, structured no-selection outcomes, the seeded
random baseline, planner-protocol compatibility, discovery-engine integration,
and ground-truth isolation.
"""

from __future__ import annotations

import inspect
import json
import random
from typing import Any, Dict, List, Optional, Sequence

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    ExperimentInput,
    FittingStatus,
    HypothesisRepresentation,
    Observation,
    StructuredError,
)
from app.domain.discovery_contracts import (
    ERROR_PLANNER_FAILURE,
    WARNING_EXPERIMENT_SELECTION_UNAVAILABLE,
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryTerminationReason,
    ExperimentExecutionOutcome,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
    CandidateExperimentStatus,
    NumericalDomainBounds,
)
from app.domain.experiment_selection import (
    CANDIDATE_REASON_NOT_ELIGIBLE,
    CANDIDATE_REASON_PREVIOUSLY_QUERIED,
    HYPOTHESIS_REASON_CONTRADICTED,
    HYPOTHESIS_REASON_EVALUATION_FAILED,
    HYPOTHESIS_REASON_NO_EVALUATION,
    HYPOTHESIS_REASON_NOT_RANKED,
    PREDICTION_REASON_UNKNOWN_FAMILY,
    PREDICTION_SOURCE_CANDIDATE,
    PREDICTION_SOURCE_FITTED,
    ActiveExperimentSelector,
    ExperimentSelectionPlanner,
    RandomBaselinePlanner,
    compute_disagreement,
)
from app.domain.ranking_contracts import (
    EvidenceSummary,
    EvaluationTier,
    PlausibilityStatus,
    RankedCandidate,
    RankingPolicyConfig,
    RankingResult,
)
from app.domain.selection_contracts import (
    CandidateScoringStatus,
    DisagreementMetric,
    ExperimentSelectionConfig,
    ExperimentSelectionResult,
    SelectionFallback,
    SelectionReason,
    SelectionStatus,
    SelectionStrategy,
)

# ==============================================================================
# Helpers
# ==============================================================================


def make_spec(coefficients: Sequence[float]) -> Dict[str, Any]:
    """Canonical polynomial specification, ascending coefficient order."""
    degree = len(coefficients) - 1
    return {
        "family": "polynomial",
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": {f"c{index}": float(value) for index, value in enumerate(coefficients)},
        "input_variable": "x",
        "output_variable": "y",
    }


def make_hypothesis(hypothesis_id: str, coefficients: Sequence[float], **parameters: Any):
    specification = make_spec(coefficients)
    return HypothesisRepresentation(
        hypothesis_id=hypothesis_id,
        model_family="polynomial",
        formula=f"y = polynomial {hypothesis_id}",
        parameters=specification if not parameters else parameters,
        complexity=specification["degree"] + 1,
    )


def make_evaluation(
    hypothesis_id: str,
    coefficients: Sequence[float],
    *,
    status: EvaluationStatus = EvaluationStatus.COMPLETED,
    fitting_status: FittingStatus = FittingStatus.SUCCESS,
) -> EvaluationResult:
    failure = status != EvaluationStatus.COMPLETED or fitting_status == FittingStatus.FAILED
    return EvaluationResult(
        hypothesis_id=hypothesis_id,
        status=status,
        metrics={} if failure else {"mae": 0.0, "rmse": 0.0},
        sample_count=6,
        model_family="polynomial",
        fitting_status=fitting_status,
        fitting_method="least_squares",
        fitting_config={"method": "least_squares"},
        fitted_specification=None if fitting_status != FittingStatus.SUCCESS else make_spec(coefficients),
        error=(
            StructuredError(code="FIT_FAILED", message="simulated fit failure")
            if failure
            else None
        ),
    )


def make_candidate(
    x: float,
    *,
    candidate_id: Optional[str] = None,
    status: CandidateExperimentStatus = CandidateExperimentStatus.ELIGIBLE,
) -> CandidateExperiment:
    return CandidateExperiment(
        candidate_id=candidate_id or f"cand-exp-{x}",
        input_data=ExperimentInput(values={"x": float(x)}),
        status=status,
    )


def make_batch(candidates: Sequence[CandidateExperiment]) -> CandidateExperimentBatch:
    return CandidateExperimentBatch(
        candidates=list(candidates),
        requested_count=len(candidates),
        generated_count=len(candidates),
    )


def make_observation(x: float, *, y: float = 0.0) -> Observation:
    return Observation(
        observation_id=f"obs-{x}",
        input_id=f"inp-{x}",
        input_values={"x": float(x)},
        output_values={"y": float(y)},
    )


def make_ranked(
    hypothesis_id: str,
    status: PlausibilityStatus,
    *,
    rank: int = 1,
) -> RankedCandidate:
    return RankedCandidate(
        rank=rank,
        hypothesis_id=hypothesis_id,
        status=status,
        evaluation_tier=EvaluationTier.VALID_TRAINING_ONLY,
        primary_metric_name="mae",
        held_out_score=None,
        training_score=0.1,
        secondary_held_out_score=None,
        complexity=2,
        held_out_sample_count=None,
        training_sample_count=6,
        evidence_summary=EvidenceSummary(
            hypothesis_id=hypothesis_id,
            supporting_count=6,
            contradicting_count=0,
            indeterminate_count=0,
            total_evaluated=6,
            supporting_observation_ids=tuple(),
            contradicting_observation_ids=tuple(),
            indeterminate_observation_ids=tuple(),
        ),
        dataset_id="dataset-test",
        evaluation_id=f"eval-{hypothesis_id}",
        explanation="test ranking",
        limitations=(),
        comparability_notes=(),
    )


def make_ranking(*ranked: RankedCandidate) -> RankingResult:
    return RankingResult(
        ranked_candidates=tuple(ranked),
        policy_config=RankingPolicyConfig(),
        ranking_version="test",
        ranked_at="2026-01-01T00:00:00+00:00",
        limitations=(),
    )


def select(
    selector: ActiveExperimentSelector,
    hypotheses: Sequence[HypothesisRepresentation],
    evaluations: Dict[str, EvaluationResult],
    batch: CandidateExperimentBatch,
    observations: Sequence[Observation] = (),
    ranking: Optional[RankingResult] = None,
) -> ExperimentSelectionResult:
    return selector.select(hypotheses, evaluations, observations, batch, ranking)


def two_competing_hypotheses() -> List[HypothesisRepresentation]:
    """y = x versus y = x^2: agree at 0, disagree elsewhere."""
    return [make_hypothesis("hyp-linear", [0.0, 1.0]), make_hypothesis("hyp-quadratic", [0.0, 0.0, 1.0])]


def candidate_source_selector(**overrides: Any) -> ActiveExperimentSelector:
    """Selector explicitly opted into candidate-specification bootstrap mode."""
    overrides.setdefault("allow_candidate_specifications", True)
    return ActiveExperimentSelector(ExperimentSelectionConfig(**overrides))


def fitted_selector(**overrides: Any) -> ActiveExperimentSelector:
    """Default strict selector: fitted specifications only."""
    return ActiveExperimentSelector(ExperimentSelectionConfig(**overrides))


# ==============================================================================
# Metric tests
# ==============================================================================


def test_identical_predictions_produce_zero_disagreement_and_no_selection():
    hypotheses = [make_hypothesis("h1", [1.0]), make_hypothesis("h2", [1.0])]
    result = select(
        candidate_source_selector(),
        hypotheses,
        {},
        make_batch([make_candidate(0.0)]),
    )
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.ZERO_DISAGREEMENT_POOL
    assert result.selected_candidate is None
    diagnostic = result.candidates[0]
    assert diagnostic.scoring_status == CandidateScoringStatus.SCORED
    assert diagnostic.disagreement_score == 0.0
    # Zero disagreement is a valid score, never evidence of informativeness.
    assert diagnostic.usable_predictions == {"h1": 1.0, "h2": 1.0}


def test_discriminative_input_is_preferred_over_identical_predictions():
    hypotheses = two_competing_hypotheses()
    batch = make_batch([make_candidate(0.0), make_candidate(2.0)])
    result = select(candidate_source_selector(), hypotheses, {}, batch)

    assert result.status == SelectionStatus.SELECTED
    assert result.reason_code == SelectionReason.ACTIVE_DISAGREEMENT_MAX
    assert result.prediction_source == PREDICTION_SOURCE_CANDIDATE
    assert result.selected_input_values == {"x": 2.0}
    scores = {item.input_values["x"]: item.disagreement_score for item in result.candidates}
    assert scores[0.0] == 0.0
    assert scores[2.0] == pytest.approx(1.0)
    selected = next(item for item in result.candidates if item.candidate_id == result.selected_candidate_id)
    assert selected.usable_predictions == {"hyp-linear": 2.0, "hyp-quadratic": 4.0}
    assert selected.excluded_predictions == {}
    assert "population_variance" in result.rationale


def test_population_variance_matches_documented_formula_for_three_hypotheses():
    hypotheses = [make_hypothesis("h1", [1.0]), make_hypothesis("h2", [2.0]), make_hypothesis("h3", [3.0])]
    result = select(
        candidate_source_selector(disagreement_metric=DisagreementMetric.POPULATION_VARIANCE),
        hypotheses,
        {},
        make_batch([make_candidate(0.5)]),
    )
    assert result.candidates[0].disagreement_score == pytest.approx(2.0 / 3.0)
    assert compute_disagreement([1.0, 2.0, 3.0]) == pytest.approx(2.0 / 3.0)


def test_sample_variance_uses_n_minus_one_divisor():
    hypotheses = [make_hypothesis("h1", [1.0]), make_hypothesis("h2", [2.0]), make_hypothesis("h3", [3.0])]
    result = select(
        candidate_source_selector(disagreement_metric=DisagreementMetric.SAMPLE_VARIANCE),
        hypotheses,
        {},
        make_batch([make_candidate(0.5)]),
    )
    assert result.config.disagreement_metric == DisagreementMetric.SAMPLE_VARIANCE
    assert result.candidates[0].disagreement_score == pytest.approx(1.0)


def test_max_spread_metric_is_peak_to_peak_difference():
    hypotheses = [make_hypothesis("h1", [-1.5]), make_hypothesis("h2", [0.5])]
    result = select(
        candidate_source_selector(disagreement_metric=DisagreementMetric.MAX_SPREAD),
        hypotheses,
        {},
        make_batch([make_candidate(0.25)]),
    )
    # Negative and fractional predictions are valid: -1.5 .. 0.5 has spread 2.0.
    assert result.candidates[0].disagreement_score == pytest.approx(2.0)


def test_negative_and_fractional_predictions_use_population_variance():
    hypotheses = [make_hypothesis("h1", [-1.5]), make_hypothesis("h2", [0.5])]
    result = select(candidate_source_selector(), hypotheses, {}, make_batch([make_candidate(0.0)]))
    # mean -0.5 -> deviations -1.0 and 1.0 -> population variance 1.0
    assert result.candidates[0].disagreement_score == pytest.approx(1.0)


def test_extreme_finite_predictions_overflow_is_reported_not_scored():
    hypotheses = [make_hypothesis("h1", [1.0]), make_hypothesis("h2", [1e308])]
    assert compute_disagreement([1.0, 1e308]) is None
    result = select(candidate_source_selector(), hypotheses, {}, make_batch([make_candidate(0.0)]))
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.DISAGREEMENT_OVERFLOW
    diagnostic = result.candidates[0]
    assert diagnostic.scoring_status == CandidateScoringStatus.DISAGREEMENT_OVERFLOW
    assert diagnostic.disagreement_score is None


def test_non_finite_prediction_is_excluded_not_zero_filled():
    # h3 overflows at x=1e100; the score must use only h1/h2.
    hypotheses = [
        make_hypothesis("h1", [1.0]),
        make_hypothesis("h2", [3.0]),
        make_hypothesis("h3", [0.0, 0.0, 0.0, 1.0]),
    ]
    result = select(
        candidate_source_selector(),
        hypotheses,
        {},
        make_batch([make_candidate(1e103)]),
    )
    diagnostic = result.candidates[0]
    assert diagnostic.disagreement_score == pytest.approx(1.0)
    assert diagnostic.usable_predictions == {"h1": 1.0, "h2": 3.0}
    assert diagnostic.excluded_predictions == {"h3": "NON_FINITE_PREDICTION"}


def test_too_few_usable_predictions_is_not_an_active_disagreement_result():
    hypotheses = [
        make_hypothesis("h1", [1.0]),
        make_hypothesis("h2", [0.0, 0.0, 0.0, 1.0]),
    ]
    result = select(
        candidate_source_selector(),
        hypotheses,
        {},
        make_batch([make_candidate(1e103)]),
    )
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    diagnostic = result.candidates[0]
    assert diagnostic.scoring_status == CandidateScoringStatus.INSUFFICIENT_PREDICTIONS
    assert diagnostic.disagreement_score is None
    assert diagnostic.excluded_predictions["h2"] == "NON_FINITE_PREDICTION"


def test_malformed_specification_is_recorded_without_crashing():
    malformed = HypothesisRepresentation(
        hypothesis_id="hyp-broken",
        model_family="not-a-registered-family",
        formula="y = malformed",
        parameters={"family": "not-a-registered-family"},
    )
    hypotheses = [make_hypothesis("h1", [1.0]), malformed]
    result = select(candidate_source_selector(), hypotheses, {}, make_batch([make_candidate(0.0)]))
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.candidates[0].excluded_predictions["hyp-broken"] == PREDICTION_REASON_UNKNOWN_FAMILY


# ==============================================================================
# Selection and tie-breaking tests
# ==============================================================================


def test_highest_valid_disagreement_wins():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 2.0])]
    batch = make_batch([make_candidate(1.0), make_candidate(2.0), make_candidate(3.0)])
    result = select(candidate_source_selector(), hypotheses, {}, batch)
    assert result.selected_input_values == {"x": 3.0}
    assert result.candidates[0].disagreement_score < result.candidates[1].disagreement_score
    assert result.candidates[1].disagreement_score < result.candidates[2].disagreement_score


def test_equal_scores_use_deterministic_canonical_tie_break():
    # y = x^2 and y = 0.5*x^2 disagree identically at -2 and +2.
    hypotheses = [make_hypothesis("h1", [0.0, 0.0, 1.0]), make_hypothesis("h2", [0.0, 0.0, 0.5])]
    batch = make_batch([make_candidate(-2.0), make_candidate(2.0)])
    first = select(candidate_source_selector(), hypotheses, {}, batch)
    second = select(candidate_source_selector(), hypotheses, {}, batch)

    assert first.selected_input_values == {"x": -2.0}
    assert first.tie_break_applied is True
    assert len(first.tied_candidate_ids) == 2
    assert first.to_dict() == second.to_dict()
    assert "tie-break" in first.rationale


def test_candidate_batch_order_does_not_change_selection_or_diagnostics():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 2.0])]
    forward = make_batch([make_candidate(0.0), make_candidate(2.0), make_candidate(3.0)])
    backward = make_batch(list(reversed(forward.candidates)))
    first = select(candidate_source_selector(), hypotheses, {}, forward)
    second = select(candidate_source_selector(), hypotheses, {}, backward)
    assert first.to_dict() == second.to_dict()


def test_empty_candidate_pool_returns_structured_no_selection():
    result = select(
        candidate_source_selector(),
        two_competing_hypotheses(),
        {},
        make_batch([]),
    )
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.EMPTY_CANDIDATE_POOL
    assert result.candidate_pool_size == 0
    assert result.valid_candidate_count == 0
    assert result.candidates == ()


def test_non_eligible_candidates_are_excluded_with_diagnostics():
    batch = make_batch(
        [
            make_candidate(0.0, status=CandidateExperimentStatus.REJECTED),
            make_candidate(1.0, status=CandidateExperimentStatus.INVALID_BOUNDS),
            make_candidate(2.0, status=CandidateExperimentStatus.EXCLUDED_PREVIOUSLY_QUERIED),
        ]
    )
    result = select(candidate_source_selector(), two_competing_hypotheses(), {}, batch)
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.NO_ELIGIBLE_CANDIDATES
    assert set(result.excluded_candidates.values()) == {CANDIDATE_REASON_NOT_ELIGIBLE}
    assert result.valid_candidate_count == 0


def test_previously_queried_inputs_are_excluded_within_tolerance():
    hypotheses = two_competing_hypotheses()
    batch = make_batch([make_candidate(1.0), make_candidate(2.0)])
    observations = [make_observation(1.0 + 5e-6)]
    result = select(candidate_source_selector(), hypotheses, {}, batch, observations)

    assert result.selected_input_values == {"x": 2.0}
    assert result.excluded_candidates == {"cand-exp-1.0": CANDIDATE_REASON_PREVIOUSLY_QUERIED}


def test_duplicate_exclusion_is_disabled_only_by_explicit_configuration():
    hypotheses = two_competing_hypotheses()
    batch = make_batch([make_candidate(2.0)])
    observations = [make_observation(2.0)]
    result = select(
        candidate_source_selector(exclude_queried_inputs=False),
        hypotheses,
        {},
        batch,
        observations,
    )
    assert result.status == SelectionStatus.SELECTED
    assert result.selected_input_values == {"x": 2.0}
    assert result.excluded_candidates == {}


def test_all_candidates_previously_queried_has_a_distinct_reason():
    batch = make_batch([make_candidate(1.0, candidate_id="cand-a")])
    observations = [make_observation(1.0)]
    result = select(candidate_source_selector(), two_competing_hypotheses(), {}, batch, observations)
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.ALL_CANDIDATES_PREVIOUSLY_QUERIED
    assert result.excluded_candidates == {"cand-a": CANDIDATE_REASON_PREVIOUSLY_QUERIED}


def test_insufficient_eligible_hypotheses_never_selects_silently():
    hypotheses = [make_hypothesis("h1", [1.0])]
    result = select(
        candidate_source_selector(),
        hypotheses,
        {},
        make_batch([make_candidate(0.0)]),
    )
    assert result.status == SelectionStatus.NO_SELECTION
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.selected_candidate is None
    assert result.config.fallback_strategy == SelectionFallback.NONE


def test_explicit_sequential_fallback_is_labelled_as_fallback():
    hypotheses = [make_hypothesis("h1", [1.0])]
    selector = ActiveExperimentSelector(
        ExperimentSelectionConfig(fallback_strategy=SelectionFallback.SEQUENTIAL)
    )
    result = select(
        selector,
        hypotheses,
        {},
        make_batch([make_candidate(2.0), make_candidate(-1.0)]),
    )
    assert result.status == SelectionStatus.FALLBACK_SELECTED
    assert result.reason_code == SelectionReason.SEQUENTIAL_FALLBACK
    assert result.selected_input_values == {"x": -1.0}
    assert "not disagreement-driven" in result.rationale


def test_zero_disagreement_pool_can_use_explicit_fallback():
    hypotheses = [make_hypothesis("h1", [1.0]), make_hypothesis("h2", [1.0])]
    selector = ActiveExperimentSelector(
        ExperimentSelectionConfig(fallback_strategy=SelectionFallback.SEQUENTIAL)
    )
    result = select(selector, hypotheses, {}, make_batch([make_candidate(0.0)]))
    assert result.status == SelectionStatus.FALLBACK_SELECTED
    assert result.reason_code == SelectionReason.SEQUENTIAL_FALLBACK
    assert result.selected_input_values == {"x": 0.0}


# ==============================================================================
# Eligibility and fitted-source tests
# ==============================================================================


def test_strict_mode_requires_fitted_hypotheses_and_never_uses_candidate_specs():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 2.0])]
    result = select(fitted_selector(), hypotheses, {}, make_batch([make_candidate(1.0)]))
    assert result.prediction_source == PREDICTION_SOURCE_FITTED
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.excluded_hypotheses == {
        "h1": HYPOTHESIS_REASON_NO_EVALUATION,
        "h2": HYPOTHESIS_REASON_NO_EVALUATION,
    }
    assert result.candidates == ()


def test_fitted_specifications_are_preferred_and_never_mixed_with_unfitted_ones():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 2.0])]
    evaluations = {"h1": make_evaluation("h1", [0.0, 1.0])}
    result = select(fitted_selector(), hypotheses, evaluations, make_batch([make_candidate(1.0)]))
    assert result.prediction_source == PREDICTION_SOURCE_FITTED
    # One fitted specification only -> one eligible hypothesis -> no selection.
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.excluded_hypotheses == {"h2": HYPOTHESIS_REASON_NO_EVALUATION}


def test_fitted_specification_predictions_are_used():
    hypotheses = [make_hypothesis("h1", [100.0, 1.0]), make_hypothesis("h2", [200.0, 1.0])]
    evaluations = {
        "h1": make_evaluation("h1", [0.0, 1.0]),
        "h2": make_evaluation("h2", [0.0, 3.0]),
    }
    result = select(fitted_selector(), hypotheses, evaluations, make_batch([make_candidate(2.0)]))
    assert result.prediction_source == PREDICTION_SOURCE_FITTED
    assert result.candidates[0].usable_predictions == {"h1": 2.0, "h2": 6.0}
    assert result.candidates[0].disagreement_score == pytest.approx(4.0)


def test_explicit_candidate_mode_switches_the_whole_source_without_mixing():
    # h1 has a fit (0.0 + 1.0*x) but only one fit exists, below the minimum, so
    # opting in uses *every* hypothesis' candidate specification instead.
    hypotheses = [make_hypothesis("h1", [100.0, 1.0]), make_hypothesis("h2", [200.0, 2.0])]
    evaluations = {"h1": make_evaluation("h1", [0.0, 1.0])}
    result = select(
        candidate_source_selector(),
        hypotheses,
        evaluations,
        make_batch([make_candidate(2.0)]),
    )
    assert result.prediction_source == PREDICTION_SOURCE_CANDIDATE
    assert result.candidates[0].usable_predictions == {"h1": 102.0, "h2": 204.0}


def test_failed_evaluations_are_excluded_from_scoring():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 2.0])]
    evaluations = {
        "h1": make_evaluation("h1", [0.0, 1.0]),
        "h2": make_evaluation("h2", [0.0, 2.0], status=EvaluationStatus.FAILED, fitting_status=FittingStatus.FAILED),
    }
    result = select(fitted_selector(), hypotheses, evaluations, make_batch([make_candidate(1.0)]))
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.excluded_hypotheses == {"h2": HYPOTHESIS_REASON_EVALUATION_FAILED}


def test_contradicted_and_unranked_hypotheses_do_not_drive_selection():
    hypotheses = [make_hypothesis("h1", [0.0, 1.0]), make_hypothesis("h2", [0.0, 5.0])]
    evaluations = {
        "h1": make_evaluation("h1", [0.0, 1.0]),
        "h2": make_evaluation("h2", [0.0, 5.0]),
    }
    ranking = make_ranking(
        make_ranked("h1", PlausibilityStatus.PLAUSIBLE, rank=1),
        make_ranked("h2", PlausibilityStatus.CONTRADICTED, rank=2),
    )
    result = select(
        fitted_selector(),
        hypotheses,
        evaluations,
        make_batch([make_candidate(1.0)]),
        ranking=ranking,
    )
    assert result.reason_code == SelectionReason.INSUFFICIENT_HYPOTHESES
    assert result.excluded_hypotheses == {"h2": HYPOTHESIS_REASON_CONTRADICTED}

    partial_ranking = make_ranking(make_ranked("h1", PlausibilityStatus.PLAUSIBLE, rank=1))
    partial = select(
        fitted_selector(),
        hypotheses,
        evaluations,
        make_batch([make_candidate(1.0)]),
        ranking=partial_ranking,
    )
    assert partial.excluded_hypotheses == {"h2": HYPOTHESIS_REASON_NOT_RANKED}


# ==============================================================================
# Random baseline tests
# ==============================================================================


def random_selector(seed: int, **overrides: Any) -> ActiveExperimentSelector:
    return ActiveExperimentSelector(
        ExperimentSelectionConfig(
            strategy=SelectionStrategy.RANDOM_BASELINE,
            random_seed=seed,
            **overrides,
        )
    )


def test_random_baseline_reproduces_the_expected_seeded_draw():
    batch = make_batch([make_candidate(value) for value in (0.0, 1.0, 2.0, 3.0)])
    expected = random.Random(7).randrange(4)
    result = select(random_selector(7), [], {}, batch)
    assert result.status == SelectionStatus.SELECTED
    assert result.reason_code == SelectionReason.RANDOM_BASELINE_DRAW
    assert result.selected_input_values == {"x": float(expected)}
    assert result.random_seed == 7
    assert result.prediction_source is None
    assert all(
        item.scoring_status == CandidateScoringStatus.NOT_SCORED_RANDOM_BASELINE
        for item in result.candidates
    )


def test_random_baseline_is_repeatable_for_the_same_seed():
    batch = make_batch([make_candidate(value) for value in (0.0, 1.0, 2.0, 3.0)])
    first = select(random_selector(11), [], {}, batch)
    second = select(random_selector(11), [], {}, batch)
    assert first.to_dict() == second.to_dict()


def test_random_baseline_matches_independent_rng_for_different_seeds():
    batch = make_batch([make_candidate(value) for value in (-2.0, -1.0, 0.0, 1.0, 2.0)])
    for seed in (3, 99):
        expected = random.Random(seed).randrange(5)
        result = select(random_selector(seed), [], {}, batch)
        assert result.selected_input_values == {"x": float(batch.candidates[expected].input_data.values["x"])}


def test_active_and_random_baselines_share_the_same_valid_pool():
    batch = make_batch(
        [
            make_candidate(0.0, candidate_id="cand-rejected", status=CandidateExperimentStatus.REJECTED),
            make_candidate(1.0, candidate_id="cand-queried"),
            make_candidate(2.0, candidate_id="cand-open-a"),
            make_candidate(3.0, candidate_id="cand-open-b"),
        ]
    )
    observations = [make_observation(1.0)]
    active = select(candidate_source_selector(), two_competing_hypotheses(), {}, batch, observations)
    baseline = select(random_selector(5), [], {}, batch, observations)

    assert active.valid_candidate_count == 2
    assert baseline.valid_candidate_count == 2
    assert dict(active.excluded_candidates) == dict(baseline.excluded_candidates)
    assert {item.candidate_id for item in active.candidates} == {
        item.candidate_id for item in baseline.candidates
    }
    assert baseline.selected_candidate_id in {"cand-open-a", "cand-open-b"}


def test_random_baseline_requires_a_seed():
    with pytest.raises(ValueError):
        ExperimentSelectionConfig(strategy=SelectionStrategy.RANDOM_BASELINE)
    with pytest.raises(ValueError):
        ExperimentSelectionConfig(strategy=SelectionStrategy.RANDOM_BASELINE, random_seed=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        RandomBaselinePlanner(seed=None)  # type: ignore[arg-type]


def test_nonzero_uncertainty_weight_is_rejected_until_a_source_exists():
    with pytest.raises(ValueError):
        ExperimentSelectionConfig(uncertainty_weight=0.5)


# ==============================================================================
# Planner compatibility and serialization
# ==============================================================================


def test_planner_preserves_the_existing_select_experiment_contract():
    hypotheses = two_competing_hypotheses()
    batch = make_batch([make_candidate(0.0), make_candidate(2.0)])
    planner = ExperimentSelectionPlanner(
        ExperimentSelectionConfig(allow_candidate_specifications=True)
    )

    expected_arguments = [
        "candidates",
        "evaluations",
        "observations",
        "candidate_batch",
        "ranking",
    ]
    for name in ("select_experiment", "select_experiment_with_diagnostics"):
        signature = inspect.signature(getattr(planner, name))
        assert list(signature.parameters) == expected_arguments

    selected = planner.select_experiment(hypotheses, {}, [], batch, None)
    structured = planner.select_experiment_with_diagnostics(hypotheses, {}, [], batch, None)
    assert isinstance(structured, ExperimentSelectionResult)
    assert isinstance(selected, CandidateExperiment)
    assert selected.candidate_id == structured.selected_candidate_id


def test_selection_result_is_json_compatible_and_immutable():
    hypotheses = two_competing_hypotheses()
    result = select(candidate_source_selector(), hypotheses, {}, make_batch([make_candidate(2.0)]))
    payload = result.to_dict()
    serialized = json.dumps(payload, sort_keys=True)
    assert json.loads(serialized) == payload
    assert payload["uncertainty"]["available"] is False
    assert payload["uncertainty"]["source"] is None
    assert "uncertainty" not in payload["candidates"][0]

    with pytest.raises(TypeError):
        result.candidates[0].usable_predictions["hyp-linear"] = 999.0  # type: ignore[index]
    with pytest.raises(TypeError):
        result.excluded_hypotheses["hyp-new"] = "tampered"  # type: ignore[index]
    assert json.dumps(result.to_dict(), sort_keys=True) == serialized


def test_selection_diagnostics_are_deterministic_for_identical_inputs():
    hypotheses = two_competing_hypotheses()
    batch = make_batch([make_candidate(0.0), make_candidate(2.0)])
    first = select(candidate_source_selector(), hypotheses, {}, batch).to_dict()
    second = select(candidate_source_selector(), hypotheses, {}, batch).to_dict()
    assert first == second
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


# ==============================================================================
# Discovery-engine integration
# ==============================================================================


class RecordingOracle(PolynomialLocalOracle):
    """Deterministic oracle that records submitted inputs and guards ground truth."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: List[Dict[str, Any]] = []
        self.ground_truth_accessed = False

    def query(self, experiment_input: ExperimentInput) -> Observation:
        self.submitted.append(dict(experiment_input.values))
        return super().query(experiment_input)

    def _evaluator_get_ground_truth(self) -> Dict[str, Any]:
        self.ground_truth_accessed = True
        raise AssertionError("The selector or engine must never read evaluator-only ground truth.")


def initial_observations() -> List[Observation]:
    """Three observations of the local oracle rule, enough to fit degrees 0..2."""
    return [
        make_observation(-2.0, y=-1.0),
        make_observation(0.0, y=3.0),
        make_observation(2.0, y=-1.0),
    ]


def make_engine_config(**overrides: Any) -> DiscoveryRunConfig:
    defaults: Dict[str, Any] = dict(
        max_query_budget=2,
        max_iterations=4,
        domain_bounds=NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="test_polynomial"
        ),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=5, seed=11
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            performance_acceptance_threshold=1e9,
            max_tolerated_contradictions=99,
        ),
        experiment_pool_size=5,
    )
    defaults.update(overrides)
    return DiscoveryRunConfig(**defaults)


def test_discovery_engine_uses_configured_active_selector_and_records_diagnostics():
    oracle = RecordingOracle()
    engine = DiscoveryEngine(
        oracle,
        make_engine_config(),
        planner=ExperimentSelectionPlanner(),
    )
    result = engine.run(initial_observations())

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.query_accounting.attempted_queries == 2
    assert len(oracle.submitted) == 2
    assert oracle.ground_truth_accessed is False

    first = result.experiment_history[0]
    assert first.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS
    diagnostics = first.selection_diagnostics
    assert diagnostics is not None
    assert diagnostics["status"] == "selected"
    assert diagnostics["strategy"] == "active_disagreement"
    assert diagnostics["reason_code"] == "active_disagreement_max"
    assert diagnostics["prediction_source"] == "fitted_specification"
    selected_id = diagnostics["selected_candidate_id"]
    chosen = next(item for item in diagnostics["candidates"] if item["candidate_id"] == selected_id)
    assert chosen["input_values"] == oracle.submitted[0]

    # The decision is serialized in the run result and remains JSON-compatible.
    payload = result.to_dict()
    assert payload["experiment_history"][0]["selection_diagnostics"]["selected_candidate_id"] == selected_id
    json.dumps(payload, sort_keys=True)


def test_engine_terminates_safely_when_selection_has_no_candidate():
    oracle = RecordingOracle()
    engine = DiscoveryEngine(
        oracle,
        make_engine_config(max_query_budget=1, max_iterations=2),
        planner=ExperimentSelectionPlanner(
            ExperimentSelectionConfig(min_eligible_hypotheses=50)
        ),
    )
    result = engine.run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS
    assert result.termination_reason == DiscoveryTerminationReason.EXPERIMENT_POOL_EXHAUSTED
    assert oracle.submitted == []
    assert result.query_accounting.attempted_queries == 0
    warnings = [warning.code for warning in result.warnings]
    assert WARNING_EXPERIMENT_SELECTION_UNAVAILABLE in warnings


def test_planner_errors_become_structured_component_failures():
    class RaisingSelector(ActiveExperimentSelector):
        def select(self, *args: Any, **kwargs: Any) -> ExperimentSelectionResult:
            raise RuntimeError("selector exploded")

    oracle = RecordingOracle()
    engine = DiscoveryEngine(
        oracle,
        make_engine_config(max_query_budget=1, max_iterations=1),
        planner=ExperimentSelectionPlanner(selector=RaisingSelector()),
    )
    result = engine.run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    assert [error.code for error in result.errors] == [ERROR_PLANNER_FAILURE]
    assert oracle.submitted == []


def test_recorded_selection_diagnostics_cannot_mutate_run_state():
    engine = DiscoveryEngine(
        RecordingOracle(),
        make_engine_config(max_query_budget=1, max_iterations=1),
        planner=ExperimentSelectionPlanner(),
    )
    result = engine.run(initial_observations())
    record = result.experiment_history[0]
    assert record.selection_diagnostics is not None

    with pytest.raises(TypeError):
        record.selection_diagnostics["status"] = "tampered"  # type: ignore[index]
    with pytest.raises(TypeError):
        record.selection_diagnostics["candidates"][0]["input_values"]["x"] = 99.0  # type: ignore[index]

    assert record.selection_diagnostics["status"] == "selected"
    assert record.selection_diagnostics["selected_input_values"]["x"] in {
        observation.input_values["x"] for observation in result.observations
    }


def test_random_baseline_planner_is_a_valid_engine_planner():
    oracle = RecordingOracle()
    engine = DiscoveryEngine(
        oracle,
        make_engine_config(max_query_budget=1, max_iterations=1),
        planner=RandomBaselinePlanner(seed=13),
    )
    result = engine.run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.query_accounting.attempted_queries == 1
    diagnostics = result.experiment_history[0].selection_diagnostics
    assert diagnostics is not None
    assert diagnostics["strategy"] == "random_baseline"
    assert diagnostics["random_seed"] == 13
    assert diagnostics["reason_code"] == "random_baseline_draw"


# ==============================================================================
# Ground-truth isolation
# ==============================================================================


def test_selector_module_has_no_oracle_or_evaluator_dependencies():
    import app.domain.experiment_selection as module
    import app.domain.selection_contracts as contracts

    for source in (inspect.getsource(module), inspect.getsource(contracts)):
        assert "GroundTruthRule" not in source
        assert "app.adapters" not in source
        assert "app.evaluator" not in source
        assert "_evaluator_get_ground_truth" not in source


def test_selection_diagnostics_never_contain_oracle_ground_truth():
    oracle = RecordingOracle()
    engine = DiscoveryEngine(
        oracle,
        make_engine_config(max_query_budget=2, max_iterations=2),
        planner=ExperimentSelectionPlanner(),
    )
    result = engine.run(initial_observations())
    payload = json.dumps(result.to_dict(), sort_keys=True)
    assert "secret_coefficients" not in payload
    assert "evaluator-canary" not in payload
    assert oracle.ground_truth_accessed is False
