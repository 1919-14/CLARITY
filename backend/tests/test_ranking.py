"""Comprehensive test suite for hypothesis ranking and evidence tracking (Issue #7).

Covers:
1. Evidence classification, tolerance calculation, and residual handling (finite, non-finite, overflow).
2. Evidence idempotency and EvidenceStore deduplication across evaluation revisions.
3. Plausibility status transitions (UNEVALUATED, UNCERTAIN, PLAUSIBLE, CONTRADICTED, UNEVALUABLE).
4. Ranking ordering priorities: Held-out generalization > Training-only > Incomparable > Failed > Unevaluated.
5. Complexity penalty within relative window vs materially different predictive metrics.
6. Mathematical transitivity and permutation invariance across N >= 3 candidates.
7. Non-finite (NaN, Inf), missing metrics, and incompatible dataset isolation.
8. Multi-candidate failure isolation (one failed fit does not corrupt valid rankings).
9. Dynamic reranking upon arrival of new contradictory observations.
10. Scientific transparency: zero fabricated confidence percentages or ground truth claims.
"""

from __future__ import annotations

import itertools
import math
from typing import Dict, List, Optional

import pytest

from app.domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    FittingStatus,
    HypothesisRepresentation,
    HypothesisStatus,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.domain.ranking_contracts import (
    EvaluationTier,
    EvidenceClassification,
    EvidenceRecord,
    EvidenceSummary,
    PlausibilityStatus,
    RankedCandidate,
    RankingCohort,
    RankingPolicyConfig,
    RankingResult,
)
from app.domain.evidence_tracker import (
    EvidenceStore,
    classify_observation_evidence,
    collect_evidence,
    derive_plausibility_status,
    summarize_evidence,
)
from app.domain.ranking_engine import (
    HypothesisRanker,
    MetricComparabilityValidator,
    RANKING_VERSION,
    rank_candidates,
)


# ==============================================================================
# Helper Factories
# ==============================================================================

def make_candidate(
    hypothesis_id: str,
    formula: str = "y = 2.0*x",
    degree: int = 1,
    coefficients: Optional[Dict[str, float]] = None,
    complexity: Optional[int] = None,
) -> HypothesisRepresentation:
    if coefficients is None:
        coefficients = {"c0": 0.0, "c1": 2.0}
    params = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": coefficients,
        "input_variable": "x",
        "output_variable": "y",
    }
    return HypothesisRepresentation(
        hypothesis_id=hypothesis_id,
        model_family=POLYNOMIAL_FAMILY_ID,
        formula=formula,
        parameters=params,
        complexity=complexity if complexity is not None else (degree + 1),
        status=HypothesisStatus.CANDIDATE,
    )


def make_observation(
    obs_id: str,
    x: float,
    y: Optional[float] = None,
    is_held_out: bool = False,
    status: ObservationStatus = ObservationStatus.SUCCESS,
) -> Observation:
    output_values = {"y": y} if (status == ObservationStatus.SUCCESS and y is not None) else None
    error = None if status == ObservationStatus.SUCCESS else StructuredError(code="ERROR", message="Failure")
    return Observation(
        observation_id=obs_id,
        input_id=f"in-{obs_id}",
        input_values={"x": x},
        output_values=output_values,
        status=status,
        is_held_out=is_held_out,
        error=error,
    )


def make_evaluation_result(
    hyp_id: str,
    eval_id: str = "eval-1",
    mae: Optional[float] = 0.05,
    rmse: Optional[float] = 0.08,
    held_out_mae: Optional[float] = None,
    held_out_rmse: Optional[float] = None,
    sample_count: int = 5,
    held_out_sample_count: Optional[int] = None,
    dataset_id: str = "dataset-default",
    status: EvaluationStatus = EvaluationStatus.COMPLETED,
    fitting_status: Optional[FittingStatus] = FittingStatus.SUCCESS,
    complexity: int = 2,
    coefficients: Optional[Dict[str, float]] = None,
) -> EvaluationResult:
    if coefficients is None:
        coefficients = {"c0": 0.0, "c1": 2.0}
    fitted_spec = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": complexity - 1,
        "coefficient_order": "ascending",
        "coefficients": coefficients,
        "input_variable": "x",
        "output_variable": "y",
    } if (status == EvaluationStatus.COMPLETED and fitting_status == FittingStatus.SUCCESS) else None

    metrics = {"mae": mae, "rmse": rmse} if mae is not None else {}
    held_out_metrics = {"mae": held_out_mae, "rmse": held_out_rmse} if held_out_mae is not None else None

    err = None if status == EvaluationStatus.COMPLETED else StructuredError(code="EVAL_FAIL", message="Failed")

    return EvaluationResult(
        hypothesis_id=hyp_id,
        evaluation_id=eval_id,
        status=status,
        metrics=metrics,
        sample_count=sample_count,
        held_out_metrics=held_out_metrics,
        held_out_sample_count=held_out_sample_count,
        model_family=POLYNOMIAL_FAMILY_ID,
        fitting_status=fitting_status,
        fitted_specification=fitted_spec,
        complexity=complexity,
        dataset_ids={"dataset_id": dataset_id},
        error=err,
    )


# ==============================================================================
# 1. Evidence Classification & Tolerance Calculations
# ==============================================================================

class TestEvidenceClassification:
    def test_supporting_evidence_within_tolerance(self):
        policy = RankingPolicyConfig(tolerance_absolute=0.1, tolerance_relative=0.01)
        hyp = make_candidate("hyp-1", formula="y = 2*x")
        eval_res = make_evaluation_result("hyp-1")
        # For x=2.0, model predicts y=4.0. Observation is y=4.05.
        # Absolute residual = 0.05.
        # Tolerance T = 0.1 + 0.01 * 4.05 = 0.1405.
        # 0.05 <= 0.1405 -> SUPPORTING.
        obs = make_observation("obs-1", x=2.0, y=4.05)
        rec = classify_observation_evidence(
            hypothesis_id="hyp-1",
            observation=obs,
            evaluation_id=eval_res.evaluation_id,
            policy=policy,
            model_family_id=hyp.model_family,
            fitted_specification=eval_res.fitted_specification,
        )
        assert rec.classification == EvidenceClassification.SUPPORTING
        assert rec.residual == pytest.approx(0.05)
        assert rec.tolerance == pytest.approx(0.1405)
        assert rec.is_held_out is False
        assert rec.partition == "training"
        assert rec.experiment_id == "in-obs-1"

    def test_contradicting_evidence_exceeding_tolerance(self):
        policy = RankingPolicyConfig(tolerance_absolute=0.05, tolerance_relative=0.01)
        hyp = make_candidate("hyp-1")
        eval_res = make_evaluation_result("hyp-1")
        # For x=3.0, model predicts y=6.0. Observation is y=7.0.
        # Residual = 1.0 > tolerance -> CONTRADICTING.
        obs = make_observation("obs-2", x=3.0, y=7.0, is_held_out=True)
        rec = classify_observation_evidence(
            hypothesis_id="hyp-1",
            observation=obs,
            evaluation_id=eval_res.evaluation_id,
            policy=policy,
            model_family_id=hyp.model_family,
            fitted_specification=eval_res.fitted_specification,
        )
        assert rec.classification == EvidenceClassification.CONTRADICTING
        assert rec.residual == pytest.approx(1.0)
        assert rec.is_held_out is True
        assert rec.partition == "held_out"

    def test_indeterminate_on_failed_observation(self):
        policy = RankingPolicyConfig()
        hyp = make_candidate("hyp-1")
        eval_res = make_evaluation_result("hyp-1")
        obs = make_observation("obs-fail", x=1.0, y=None, status=ObservationStatus.ORACLE_FAILURE)
        rec = classify_observation_evidence(
            hypothesis_id="hyp-1",
            observation=obs,
            evaluation_id=eval_res.evaluation_id,
            policy=policy,
            model_family_id=hyp.model_family,
            fitted_specification=eval_res.fitted_specification,
        )
        assert rec.classification == EvidenceClassification.INDETERMINATE
        assert rec.observed_value is None
        assert rec.residual is None

    def test_indeterminate_on_non_finite_or_overflow_values(self):
        policy = RankingPolicyConfig()
        hyp = make_candidate("hyp-1")
        eval_res = make_evaluation_result("hyp-1")
        # Extreme float value causing overflow or nan
        obs = make_observation("obs-inf", x=1e308, y=1e308)
        # Prediction for 2 * 1e308 overflows float
        rec = classify_observation_evidence(
            hypothesis_id="hyp-1",
            observation=obs,
            evaluation_id=eval_res.evaluation_id,
            policy=policy,
            model_family_id=hyp.model_family,
            fitted_specification=eval_res.fitted_specification,
        )
        assert rec.classification == EvidenceClassification.INDETERMINATE
        assert rec.residual is None


# ==============================================================================
# 2. Evidence Idempotency & EvidenceStore
# ==============================================================================

class TestEvidenceStoreAndIdempotency:
    def test_idempotent_key_structure(self):
        rec_id = EvidenceRecord.make_evidence_id("hyp-10", "obs-20", "training")
        assert rec_id == "evi-hyp-10-obs-20-training"

    def test_repeated_observation_does_not_duplicate_evidence(self):
        policy = RankingPolicyConfig()
        hyp = make_candidate("hyp-1")
        eval_res1 = make_evaluation_result("hyp-1", eval_id="eval-v1")
        obs = make_observation("obs-1", x=2.0, y=4.0)

        # First evaluation
        records1 = collect_evidence(eval_res1, [obs], policy, dataset_version=1)
        assert len(records1) == 1
        assert records1[0].evaluation_id == "eval-v1"

        store = EvidenceStore()
        store.add_many(records1)
        assert store.count() == 1

        # Second evaluation of the same observation with a new evaluation_id (e.g. revision)
        eval_res2 = make_evaluation_result("hyp-1", eval_id="eval-v2")
        records2 = collect_evidence(eval_res2, [obs], policy, dataset_version=2)
        assert len(records2) == 1
        assert records2[0].evidence_id == records1[0].evidence_id
        assert records2[0].evaluation_id == "eval-v2"

        # Ingest into store; should update provenance without increasing count
        store.add_many(records2)
        assert store.count() == 1
        summary = store.get_summary("hyp-1")
        assert summary.total_evaluated == 1
        assert summary.supporting_count == 1
        assert summary.supporting_observation_ids == ("obs-1",)

    def test_summary_preserves_both_supporting_and_contradicting_lists(self):
        policy = RankingPolicyConfig()
        hyp = make_candidate("hyp-1")
        eval_res = make_evaluation_result("hyp-1")
        obs_supp = make_observation("obs-s", x=2.0, y=4.0)
        obs_contra = make_observation("obs-c", x=3.0, y=10.0)

        records = collect_evidence(eval_res, [obs_supp, obs_contra], policy)
        summary = summarize_evidence("hyp-1", records)
        assert summary.supporting_count == 1
        assert summary.contradicting_count == 1
        assert summary.supporting_observation_ids == ("obs-s",)
        assert summary.contradicting_observation_ids == ("obs-c",)
        assert summary.total_observations == 2


# ==============================================================================
# 3. Scientific Status Transitions & Plausibility
# ==============================================================================

class TestPlausibilityStatusTransitions:
    def test_unevaluated_status_when_no_evaluation(self):
        policy = RankingPolicyConfig()
        summary = EvidenceSummary("hyp-1", 0, 0, 0, 0, (), (), ())
        status = derive_plausibility_status(None, summary, policy)
        assert status == PlausibilityStatus.UNEVALUATED

    def test_unevaluable_status_on_failed_evaluation(self):
        policy = RankingPolicyConfig()
        eval_res = make_evaluation_result("hyp-1", status=EvaluationStatus.FAILED, fitting_status=FittingStatus.FAILED)
        summary = EvidenceSummary("hyp-1", 0, 0, 0, 0, (), (), ())
        status = derive_plausibility_status(eval_res, summary, policy)
        assert status == PlausibilityStatus.UNEVALUABLE

    def test_uncertain_status_when_sample_count_below_minimum(self):
        policy = RankingPolicyConfig(min_samples_for_plausibility=5)
        # Only 3 samples accepted (< 5)
        eval_res = make_evaluation_result("hyp-1", sample_count=3)
        summary = EvidenceSummary("hyp-1", 3, 0, 0, 3, ("o1", "o2", "o3"), (), ())
        status = derive_plausibility_status(eval_res, summary, policy)
        assert status == PlausibilityStatus.UNCERTAIN

    def test_plausible_status_when_sufficient_samples_and_no_contradictions(self):
        policy = RankingPolicyConfig(min_samples_for_plausibility=5, max_tolerated_contradictions=0)
        eval_res = make_evaluation_result("hyp-1", sample_count=6)
        summary = EvidenceSummary("hyp-1", 6, 0, 0, 6, ("o1", "o2", "o3", "o4", "o5", "o6"), (), ())
        status = derive_plausibility_status(eval_res, summary, policy)
        assert status == PlausibilityStatus.PLAUSIBLE

    def test_contradicted_status_when_contradictions_exceed_tolerance(self):
        policy = RankingPolicyConfig(min_samples_for_plausibility=5, max_tolerated_contradictions=0)
        eval_res = make_evaluation_result("hyp-1", sample_count=6)
        summary = EvidenceSummary("hyp-1", 5, 1, 0, 6, ("o1", "o2", "o3", "o4", "o5"), ("o6",), ())
        status = derive_plausibility_status(eval_res, summary, policy)
        assert status == PlausibilityStatus.CONTRADICTED


# ==============================================================================
# 4. Ranking Order Priorities & Presentation Tiers
# ==============================================================================

class TestRankingOrderPriorities:
    def test_held_out_metrics_rank_ahead_of_training_only(self):
        """Held-out candidate must appear in VALID_HELD_OUT tier ahead of VALID_TRAINING_ONLY tier."""
        c_held = make_candidate("hyp-held")
        c_train = make_candidate("hyp-train")

        # hyp-held has held-out MAE = 0.05
        e_held = make_evaluation_result("hyp-held", mae=0.04, held_out_mae=0.05, held_out_sample_count=5)
        # hyp-train has better training MAE (0.01) but NO held-out evaluation
        e_train = make_evaluation_result("hyp-train", mae=0.01, held_out_mae=None)

        result = rank_candidates(
            candidates=[c_train, c_held],
            evaluation_results={"hyp-held": e_held, "hyp-train": e_train},
            observations_by_hypothesis={},
        )

        ranked = result.ranked_candidates
        assert len(ranked) == 2
        # hyp-held is rank 1 because VALID_HELD_OUT outranks VALID_TRAINING_ONLY
        assert ranked[0].hypothesis_id == "hyp-held"
        assert ranked[0].evaluation_tier == EvaluationTier.VALID_HELD_OUT
        assert ranked[1].hypothesis_id == "hyp-train"
        assert ranked[1].evaluation_tier == EvaluationTier.VALID_TRAINING_ONLY

    def test_within_tier_ranking_by_held_out_metric(self):
        c1 = make_candidate("hyp-1")
        c2 = make_candidate("hyp-2")

        e1 = make_evaluation_result("hyp-1", held_out_mae=0.10)
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.04)

        result = rank_candidates(
            candidates=[c1, c2],
            evaluation_results={"hyp-1": e1, "hyp-2": e2},
            observations_by_hypothesis={},
        )
        assert result.ranked_candidates[0].hypothesis_id == "hyp-2"
        assert result.ranked_candidates[1].hypothesis_id == "hyp-1"

    def test_tier_ordering_failed_and_unevaluated(self):
        c_val = make_candidate("hyp-val")
        c_fail = make_candidate("hyp-fail")
        c_un = make_candidate("hyp-uneval")

        e_val = make_evaluation_result("hyp-val", held_out_mae=0.05)
        e_fail = make_evaluation_result("hyp-fail", status=EvaluationStatus.FAILED, fitting_status=FittingStatus.FAILED)

        result = rank_candidates(
            candidates=[c_un, c_fail, c_val],
            evaluation_results={"hyp-val": e_val, "hyp-fail": e_fail},
            observations_by_hypothesis={},
        )
        tiers = [r.evaluation_tier for r in result.ranked_candidates]
        assert tiers == [
            EvaluationTier.VALID_HELD_OUT,
            EvaluationTier.EVALUATION_FAILED,
            EvaluationTier.UNEVALUATED,
        ]


# ==============================================================================
# 5. Complexity Trade-Off & Relative Window
# ==============================================================================

class TestComplexityTradeOff:
    def test_simpler_model_preferred_when_metrics_comparable(self):
        """When MAEs are within 1% relative window (e.g. 0.1000 vs 0.1005), simpler model wins."""
        c_simple = make_candidate("hyp-simple", degree=1, complexity=2)
        c_complex = make_candidate("hyp-complex", degree=3, complexity=4)

        # Relative difference = |0.1005 - 0.1000| / 0.1005 = ~0.00497 (0.5% < 1.0% window)
        e_simple = make_evaluation_result("hyp-simple", held_out_mae=0.1005, complexity=2)
        e_complex = make_evaluation_result("hyp-complex", held_out_mae=0.1000, complexity=4)

        policy = RankingPolicyConfig(complexity_window=0.01)
        result = rank_candidates(
            candidates=[c_complex, c_simple],
            evaluation_results={"hyp-simple": e_simple, "hyp-complex": e_complex},
            observations_by_hypothesis={},
            policy=policy,
        )
        # Simple model should be ranked 1 despite tiny metric difference
        assert result.ranked_candidates[0].hypothesis_id == "hyp-simple"
        assert result.ranked_candidates[1].hypothesis_id == "hyp-complex"

    def test_better_metric_wins_when_difference_material(self):
        """When MAE difference exceeds window (e.g. 0.05 vs 0.10), better metric wins regardless of complexity."""
        c_simple = make_candidate("hyp-simple", degree=1, complexity=2)
        c_complex = make_candidate("hyp-complex", degree=3, complexity=4)

        e_simple = make_evaluation_result("hyp-simple", held_out_mae=0.10, complexity=2)
        e_complex = make_evaluation_result("hyp-complex", held_out_mae=0.05, complexity=4)

        policy = RankingPolicyConfig(complexity_window=0.01)
        result = rank_candidates(
            candidates=[c_simple, c_complex],
            evaluation_results={"hyp-simple": e_simple, "hyp-complex": e_complex},
            observations_by_hypothesis={},
            policy=policy,
        )
        assert result.ranked_candidates[0].hypothesis_id == "hyp-complex"
        assert result.ranked_candidates[1].hypothesis_id == "hyp-simple"


# ==============================================================================
# 6. Mathematical Transitivity & Permutation Invariance
# ==============================================================================

class TestMathematicalTransitivity:
    def test_transitive_total_ordering_all_permutations(self):
        """For N=3 candidates, sorting across all 3! = 6 permutations must yield identical order."""
        c_a = make_candidate("hyp-A", degree=1, complexity=2)
        c_b = make_candidate("hyp-B", degree=1, complexity=2)
        c_c = make_candidate("hyp-C", degree=2, complexity=3)

        # Metric order: A (0.02) < B (0.05) < C (0.08)
        e_a = make_evaluation_result("hyp-A", held_out_mae=0.02)
        e_b = make_evaluation_result("hyp-B", held_out_mae=0.05)
        e_c = make_evaluation_result("hyp-C", held_out_mae=0.08)

        eval_map = {"hyp-A": e_a, "hyp-B": e_b, "hyp-C": e_c}
        candidates = [c_a, c_b, c_c]

        expected_order = ["hyp-A", "hyp-B", "hyp-C"]

        for perm in itertools.permutations(candidates):
            res = rank_candidates(
                candidates=list(perm),
                evaluation_results=eval_map,
                observations_by_hypothesis={},
            )
            actual_order = [rc.hypothesis_id for rc in res.ranked_candidates]
            assert actual_order == expected_order, f"Failed for permutation {[c.hypothesis_id for c in perm]}"

    def test_stable_lexicographical_tie_breaking(self):
        """When metrics, complexity, and secondary scores are identical, hypothesis_id breaks ties deterministically."""
        c_x = make_candidate("hyp-X", degree=1, complexity=2)
        c_y = make_candidate("hyp-Y", degree=1, complexity=2)

        e_x = make_evaluation_result("hyp-X", held_out_mae=0.05, held_out_rmse=0.06, complexity=2)
        e_y = make_evaluation_result("hyp-Y", held_out_mae=0.05, held_out_rmse=0.06, complexity=2)

        res1 = rank_candidates([c_x, c_y], {"hyp-X": e_x, "hyp-Y": e_y}, {})
        res2 = rank_candidates([c_y, c_x], {"hyp-X": e_x, "hyp-Y": e_y}, {})

        assert [r.hypothesis_id for r in res1.ranked_candidates] == ["hyp-X", "hyp-Y"]
        assert [r.hypothesis_id for r in res2.ranked_candidates] == ["hyp-X", "hyp-Y"]


# ==============================================================================
# 7. Non-Finite & Incomparable Metric Handling
# ==============================================================================

class TestIncomparableMetricHandling:
    def test_non_finite_metric_placed_in_incomparable_tier(self):
        c1 = make_candidate("hyp-nan")
        c2 = make_candidate("hyp-valid")

        # nan metric
        e1 = make_evaluation_result("hyp-nan", held_out_mae=float("nan"))
        e2 = make_evaluation_result("hyp-valid", held_out_mae=0.05)

        res = rank_candidates([c1, c2], {"hyp-nan": e1, "hyp-valid": e2}, {})
        assert res.ranked_candidates[0].hypothesis_id == "hyp-valid"
        assert res.ranked_candidates[0].evaluation_tier == EvaluationTier.VALID_HELD_OUT
        assert res.ranked_candidates[1].hypothesis_id == "hyp-nan"
        assert res.ranked_candidates[1].evaluation_tier == EvaluationTier.INCOMPARABLE

    def test_differing_datasets_disclosed_in_limitations(self):
        c1 = make_candidate("hyp-1")
        c2 = make_candidate("hyp-2")

        e1 = make_evaluation_result("hyp-1", held_out_mae=0.04, dataset_id="ds-A")
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.05, dataset_id="ds-B")

        res = rank_candidates([c1, c2], {"hyp-1": e1, "hyp-2": e2}, {})
        # Overall limitations should disclose different datasets
        assert any("different datasets" in lim for lim in res.limitations)


# ==============================================================================
# 8. Multi-Candidate Failure Isolation
# ==============================================================================

class TestMultiCandidateIsolation:
    def test_failing_candidate_does_not_affect_valid_candidates(self):
        c_good1 = make_candidate("hyp-1")
        c_bad = make_candidate("hyp-bad")
        c_good2 = make_candidate("hyp-2")

        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02)
        e_bad = make_evaluation_result("hyp-bad", status=EvaluationStatus.FAILED, fitting_status=FittingStatus.FAILED)
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.05)

        res = rank_candidates(
            [c_good2, c_bad, c_good1],
            {"hyp-1": e1, "hyp-bad": e_bad, "hyp-2": e2},
            {},
        )
        ranked = [r.hypothesis_id for r in res.ranked_candidates]
        assert ranked == ["hyp-1", "hyp-2", "hyp-bad"]
        assert res.ranked_candidates[2].status == PlausibilityStatus.UNEVALUABLE


# ==============================================================================
# 9. Dynamic Reranking & HypothesisRanker Service
# ==============================================================================

class TestHypothesisRankerService:
    def test_reranking_on_new_contradictory_observation(self):
        ranker = HypothesisRanker()
        c1 = make_candidate("hyp-1", formula="y = 2*x")
        c2 = make_candidate("hyp-2", formula="y = 3*x", coefficients={"c0": 0.0, "c1": 3.0})

        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02, coefficients={"c0": 0.0, "c1": 2.0})
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.03, coefficients={"c0": 0.0, "c1": 3.0})

        # Initial observations: both have clean supporting data
        obs_initial = [make_observation(f"obs-{i}", x=float(i), y=2.0 * i) for i in range(1, 6)]
        obs_initial_c2 = [make_observation(f"obs-{i}", x=float(i), y=3.0 * i) for i in range(1, 6)]

        initial_res = ranker.rank_candidates(
            candidates=[c1, c2],
            evaluation_results={"hyp-1": e1, "hyp-2": e2},
            observations_by_hypothesis={"hyp-1": obs_initial, "hyp-2": obs_initial_c2},
        )
        assert initial_res.ranked_candidates[0].hypothesis_id == "hyp-1"
        assert initial_res.ranked_candidates[0].status == PlausibilityStatus.PLAUSIBLE

        # Ingest new observation contradicting hyp-1 (x=10, y=50 instead of 20)
        obs_new = make_observation("obs-contra", x=10.0, y=50.0)
        updated_obs_c1 = list(obs_initial) + [obs_new]

        reranked_res = ranker.rerank_on_new_observations(
            current_ranking=initial_res,
            candidates=[c1, c2],
            evaluation_results={"hyp-1": e1, "hyp-2": e2},
            updated_observations_by_hypothesis={"hyp-1": updated_obs_c1, "hyp-2": obs_initial_c2},
        )
        rc1 = [r for r in reranked_res.ranked_candidates if r.hypothesis_id == "hyp-1"][0]
        assert rc1.status == PlausibilityStatus.CONTRADICTED
        assert rc1.evidence_summary.contradicting_count == 1
        assert "obs-contra" in rc1.evidence_summary.contradicting_observation_ids


# ==============================================================================
# 10. Scientific Transparency & No Fabricated Claims
# ==============================================================================

class TestScientificTransparency:
    def test_no_fabricated_confidence_percentages(self):
        c1 = make_candidate("hyp-1")
        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02)
        obs = [make_observation(f"o{i}", x=float(i), y=2.0 * i) for i in range(1, 7)]

        res = rank_candidates([c1], {"hyp-1": e1}, {"hyp-1": obs})
        rc = res.ranked_candidates[0]

        d = rc.to_dict()
        serialized = str(d).lower()
        # Ensure forbidden terms do not appear in serialized explanations or summaries
        assert "% confident" not in serialized
        assert "probability of correctness" not in serialized
        assert "ground truth" not in serialized
        assert "true rule" not in serialized
        assert rc.status == PlausibilityStatus.PLAUSIBLE
        assert rc.rank == 1


# ==============================================================================
# 11. Metric Comparability Validator & Serialization
# ==============================================================================

class TestMetricComparabilityValidator:
    def test_pairwise_comparable_same_dataset_and_tier(self):
        validator = MetricComparabilityValidator()
        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02, dataset_id="ds-1")
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.03, dataset_id="ds-1")

        comparable, reasons = validator.validate_pairwise_comparability(e1, e2)
        assert comparable is True
        assert len(reasons) == 0

    def test_pairwise_incomparable_different_tiers(self):
        validator = MetricComparabilityValidator()
        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02, dataset_id="ds-1")
        e2 = make_evaluation_result("hyp-2", mae=0.03, held_out_mae=None, dataset_id="ds-1")

        comparable, reasons = validator.validate_pairwise_comparability(e1, e2)
        assert comparable is False
        assert any("different presentation tiers" in r for r in reasons)

    def test_pairwise_incomparable_different_datasets(self):
        validator = MetricComparabilityValidator()
        e1 = make_evaluation_result("hyp-1", held_out_mae=0.02, dataset_id="ds-1")
        e2 = make_evaluation_result("hyp-2", held_out_mae=0.03, dataset_id="ds-2")

        comparable, reasons = validator.validate_pairwise_comparability(e1, e2)
        assert comparable is False
        assert any("Dataset identifiers do not match" in r for r in reasons)


class TestRankingPolicyConfigValidation:
    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="metric_direction"):
            RankingPolicyConfig(metric_direction="invalid")

    def test_negative_thresholds_raise(self):
        with pytest.raises(ValueError):
            RankingPolicyConfig(tolerance_absolute=-0.1)
        with pytest.raises(ValueError):
            RankingPolicyConfig(tolerance_relative=-0.01)
        with pytest.raises(ValueError):
            RankingPolicyConfig(min_samples_for_plausibility=0)

    def test_is_better_and_is_comparable(self):
        policy_min = RankingPolicyConfig(metric_direction="minimize", complexity_window=0.05)
        assert policy_min.is_better(0.01, 0.05) is True
        assert policy_min.is_better(0.05, 0.01) is False
        assert policy_min.is_comparable_within_window(1.0, 1.04) is True
        assert policy_min.is_comparable_within_window(1.0, 1.10) is False

        policy_max = RankingPolicyConfig(metric_direction="maximize")
        assert policy_max.is_better(0.9, 0.8) is True
        assert policy_max.is_better(0.8, 0.9) is False

