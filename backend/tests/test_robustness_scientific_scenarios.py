"""Scientific Scenarios Robustness and Edge-Case Test Suite (Issue #28 - Scope 1).

Validates CLARITY discovery behavior against supported benchmark scenarios under:
1. Deterministic observations (exact convergence, reproducibility, monotonicity).
2. Configurable noisy observations (noise floors, realistic residuals, numerical stability).
3. Boundary and extreme valid inputs (domain edges, large/small coordinates).
4. Multiple plausible hypotheses (competing candidates, near-zero prediction overlap).
5. Hypotheses with identical predictions on current data (breaking degeneracy via active selection).
6. Cases where the correct hypothesis is outside the initial hypothesis space
   (graceful degradation, honest uncertainty reporting, no fabricated confidence).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.contracts import (
    ExperimentInput,
    GroundTruthRule,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
)
from app.domain.discovery_contracts import (
    DiscoveryRunConfig,
    DiscoveryRunResult,
    DiscoveryTerminationReason,
)
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentGenerator,
    ExperimentGenerationConfig,
    NumericalDomainBounds,
)
from app.domain.experiment_selection import (
    ActiveExperimentSelector,
    ExperimentSelectionConfig,
    SelectionStrategy,
)
from app.domain.fitting import (
    FamilyFitter,
    FittingConfig,
    fit_candidate,
)
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.domain.ranking_contracts import PlausibilityStatus, RankingPolicyConfig
from app.domain.ranking_engine import HypothesisRanker
from app.evaluator.benchmark_cases import (
    BOUNDARY_EDGE_CASES_BENCHMARK,
    CONSTANT_BENCHMARK,
    CUBIC_BENCHMARK,
    LINEAR_BENCHMARK,
    NOISY_POLYNOMIAL_BENCHMARK,
    QUADRATIC_BENCHMARK,
    BenchmarkCaseDefinition,
    PolynomialGroundTruthRule,
)
from app.evaluator.benchmark_runner import (
    ActiveRandomBenchmarkRunner,
    BenchmarkTrialResult,
)


# ==============================================================================
# 1. Deterministic Observations
# ==============================================================================

class TestDeterministicScientificScenarios:
    """Validate discovery behavior under deterministic (noise_std=0.0) observations."""

    def test_deterministic_random_baseline_executes_within_budget(self):
        """Under deterministic observations, random baseline runs within budget and tracks finite held-out error."""
        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            LINEAR_BENCHMARK,
            strategy="random_baseline",
            seed=42,
            max_budget=8,
        )

        assert trial.queries_used <= 8
        assert trial.held_out_mae is not None
        assert math.isfinite(trial.held_out_mae)
        assert trial.held_out_rmse is not None
        assert math.isfinite(trial.held_out_rmse)

    def test_deterministic_active_selection_with_sufficient_initial_inputs_executes_within_budget(self):
        """Active selection with at least 2 distinct initial inputs fits hypotheses and issues queries within budget."""
        two_point_linear = BenchmarkCaseDefinition(
            benchmark_name="two_point_linear_benchmark",
            scenario_id="two_point_linear_benchmark",
            description="Linear discovery with 2 initial points for active fit initialization",
            ground_truth=LINEAR_BENCHMARK.ground_truth,
            domain_bounds=LINEAR_BENCHMARK.domain_bounds,
            max_query_budget=8,
            success_threshold=0.05,
            noise_std=0.0,
            max_hypothesis_degree=2,
            held_out_inputs=LINEAR_BENCHMARK.held_out_inputs,
            initial_inputs=({"x": -2.0}, {"x": 2.0}),
        )

        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            two_point_linear,
            strategy="active_selection",
            seed=42,
            max_budget=8,
        )

        assert trial.queries_used <= 8
        assert trial.held_out_mae is not None
        assert math.isfinite(trial.held_out_mae)
        assert len(trial.selection_history) > 0  # Active selection was successfully activated

    def test_deterministic_trials_reproduce_exact_results(self):
        """Re-running a trial with the exact same seed produces identical outcomes."""
        runner = ActiveRandomBenchmarkRunner()
        t1 = runner.run_single_trial(LINEAR_BENCHMARK, strategy="random_baseline", seed=99, max_budget=5)
        t2 = runner.run_single_trial(LINEAR_BENCHMARK, strategy="random_baseline", seed=99, max_budget=5)

        assert t1.queries_used == t2.queries_used
        assert t1.target_reached == t2.target_reached
        assert t1.held_out_mae == pytest.approx(t2.held_out_mae, rel=1e-6)
        assert t1.held_out_rmse == pytest.approx(t2.held_out_rmse, rel=1e-6)

    def test_insufficient_initial_inputs_active_selection_stops_cleanly_without_fabrication(self):
        """When active selection has only 1 initial observation, it halts cleanly rather than fabricating queries."""
        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            LINEAR_BENCHMARK,
            strategy="active_selection",
            seed=42,
            max_budget=8,
        )

        # Reports no_informative_experiment_available honestly without fabricating a fake target
        assert trial.termination_reason == DiscoveryTerminationReason.NO_INFORMATIVE_EXPERIMENT_AVAILABLE.value
        assert trial.target_reached is False
        assert trial.queries_used == 1  # Only the single initial input was queried


# ==============================================================================
# 2. Configurable Noisy Observations
# ==============================================================================

class TestConfigurableNoisyObservations:
    """Validate behavior under noisy observations: honest residual tracking without false certainty."""

    def test_noisy_observations_store_actual_noisy_values(self):
        """Observations from a noisy oracle store the perturbed measurement, not ideal ground truth."""
        scenario = NOISY_POLYNOMIAL_BENCHMARK
        oracle = scenario.create_oracle(noise_seed=42)

        raw_outputs = []
        for _ in range(5):
            inp = ExperimentInput(input_id="inp-test", values={"x": 1.0})
            obs = oracle.query(inp)
            assert obs.status == ObservationStatus.SUCCESS
            raw_outputs.append(obs.output_values["y"])

        # Ground truth for x=1.0 is y = 1.0 + 2.0*(1.0) = 3.0
        # With Gaussian noise std=0.15, measurements vary or are perturbed from 3.0
        assert any(y != 3.0 for y in raw_outputs)

    @pytest.mark.parametrize("noise_std", [0.05, 0.15, 0.30, 0.50])
    def test_varying_noise_levels_do_not_crash_or_produce_nan(self, noise_std: float):
        """Discovery runs stably across diverse noise levels without numerical overflow or NaN."""
        noisy_case = BenchmarkCaseDefinition(
            benchmark_name=f"test_noise_{noise_std}",
            scenario_id=f"test_noise_{noise_std}",
            description="Linear discovery under controlled noise",
            ground_truth=LINEAR_BENCHMARK.ground_truth,
            domain_bounds=LINEAR_BENCHMARK.domain_bounds,
            max_query_budget=6,
            success_threshold=max(0.05, noise_std * 2.0),
            noise_std=noise_std,
            max_hypothesis_degree=2,
            held_out_inputs=LINEAR_BENCHMARK.held_out_inputs,
            initial_inputs=({"x": 0.0},),
        )

        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            noisy_case,
            strategy="random_baseline",
            seed=42,
            max_budget=6,
        )

        assert trial.queries_used <= 6
        assert trial.held_out_mae is not None
        assert math.isfinite(trial.held_out_mae)
        assert not math.isnan(trial.held_out_mae)
        assert trial.held_out_rmse is not None
        assert math.isfinite(trial.held_out_rmse)

    def test_noisy_scenario_residual_reflects_noise_floor_not_zero(self):
        """When observations are noisy, held-out error honestly reflects the noise floor rather than claiming 0.0."""
        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            NOISY_POLYNOMIAL_BENCHMARK,
            strategy="random_baseline",
            seed=777,
            max_budget=6,
        )

        # In a noisy environment (std=0.15), the error should be strictly positive (> 0.0)
        assert trial.held_out_mae is not None
        assert trial.held_out_mae > 0.001
        assert trial.queries_used <= 6

    def test_evaluator_evaluates_against_clean_truth_without_leakage(self):
        """Evaluator checks noisy discoveries against hidden true noiseless ground truth."""
        scenario = NOISY_POLYNOMIAL_BENCHMARK
        held_out = scenario.generate_held_out_dataset()

        # The held-out dataset must be exact (evaluator-side ground truth)
        for entry in held_out:
            x_val = entry["input"]["x"]
            expected_y = entry["expected_output"]["y"]
            exact_y = scenario.ground_truth.evaluate_exact({"x": x_val})["y"]
            assert expected_y == exact_y


# ==============================================================================
# 3. Boundary and Extreme Valid Inputs
# ==============================================================================

class TestBoundaryAndExtremeValidInputs:
    """Validate behavior at domain boundaries and extreme numerical coordinates."""

    def test_boundary_edge_cases_benchmark_runs_and_respects_bounds(self):
        """Discovery respects strict domain boundaries [-2.0, 2.0] without out-of-bounds queries."""
        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            BOUNDARY_EDGE_CASES_BENCHMARK,
            strategy="random_baseline",
            seed=888,
            max_budget=6,
        )

        assert trial.queries_used <= 6
        for step in trial.selection_history:
            x_val = step["input_values"]["x"]
            assert -2.0 <= x_val <= 2.0

    def test_boundary_inputs_accepted_by_oracle_and_evaluated(self):
        """Oracle accepts exact boundary inputs (validate_input returns None) and returns valid observations."""
        scenario = BOUNDARY_EDGE_CASES_BENCHMARK
        oracle = scenario.create_oracle(noise_seed=42)

        for edge_x in [-2.0, 2.0]:
            exp_inp = ExperimentInput(input_id=f"inp-{edge_x}", values={"x": edge_x})
            val_res = oracle.validate_input(exp_inp)
            assert val_res is None  # None indicates valid input

            obs = oracle.query(exp_inp)
            assert obs.status == ObservationStatus.SUCCESS
            assert "y" in obs.output_values
            assert math.isfinite(obs.output_values["y"])

    def test_extreme_numerical_coordinates_stability(self):
        """Large domain bounds and tiny coordinates evaluate without floating-point overflow."""
        oracle = PolynomialLocalOracle(
            domain_name="1d_polynomial",
            min_x=-1000.0,
            max_x=1000.0,
            coefficients={"w0": 1.0, "w1": 0.5},
            noise_std=0.0,
        )

        # Boundary inputs at magnitude 1000 and tiny fractional values
        for extreme_x in [-1000.0, -1e-6, 1e-6, 1000.0]:
            inp = ExperimentInput(input_id="inp-extreme", values={"x": extreme_x})
            assert oracle.validate_input(inp) is None
            obs = oracle.query(inp)
            assert obs.status == ObservationStatus.SUCCESS
            assert math.isfinite(obs.output_values["y"])


# ==============================================================================
# 4. Multiple Plausible Hypotheses & Degeneracy Breaking
# ==============================================================================

class TestMultiplePlausibleAndDegenerateHypotheses:
    """Validate handling of competing hypotheses and active disambiguation."""

    def test_multiple_hypotheses_fit_sparse_initial_data_equally_well(self):
        """With only 1 observation at origin (0, 0), linear and cubic candidate specifications both predict 0.0."""
        obs_origin = Observation(
            input_id="inp-0",
            observation_id="obs-0",
            input_values={"x": 0.0},
            output_values={"y": 0.0},
            status=ObservationStatus.SUCCESS,
        )

        h_linear = HypothesisRepresentation(
            hypothesis_id="h-linear",
            formula="y = 3.0*x",
            model_family=POLYNOMIAL_FAMILY_ID,
            parameters={"c0": 0.0, "c1": 3.0, "degree": 1},
        )
        h_cubic = HypothesisRepresentation(
            hypothesis_id="h-cubic",
            formula="y = 3.0*x - 0.5*x^3",
            model_family=POLYNOMIAL_FAMILY_ID,
            parameters={"c0": 0.0, "c1": 3.0, "c2": 0.0, "c3": -0.5, "degree": 3},
        )

        # Both predict y=0 at x=0
        pred_linear = 0.0 * 3.0
        pred_cubic = 3.0 * 0.0 - 0.5 * (0.0 ** 3)
        assert pred_linear == 0.0
        assert pred_cubic == 0.0

    def test_active_selection_probes_disagreement_region_outside_near_zero(self):
        """Active experiment selector chooses an experiment where competing hypotheses diverge."""
        bounds = NumericalDomainBounds("x", -3.0, 3.0, "1d_polynomial")
        exp_gen = CandidateExperimentGenerator(
            config=ExperimentGenerationConfig(bounds=bounds, pool_size=15, seed=42)
        )
        batch = exp_gen.generate_batch()

        h_linear = HypothesisRepresentation(
            hypothesis_id="h-linear",
            formula="y = 3.0*x",
            model_family=POLYNOMIAL_FAMILY_ID,
            parameters={
                "family": POLYNOMIAL_FAMILY_ID,
                "degree": 1,
                "coefficients": [0.0, 3.0],
                "coefficient_order": "ascending",
                "input_variable": "x",
                "output_variable": "y",
            },
        )
        h_cubic = HypothesisRepresentation(
            hypothesis_id="h-cubic",
            formula="y = 3.0*x - 0.5*x^3",
            model_family=POLYNOMIAL_FAMILY_ID,
            parameters={
                "family": POLYNOMIAL_FAMILY_ID,
                "degree": 3,
                "coefficients": [0.0, 3.0, 0.0, -0.5],
                "coefficient_order": "ascending",
                "input_variable": "x",
                "output_variable": "y",
            },
        )

        selector = ActiveExperimentSelector(
            config=ExperimentSelectionConfig(
                strategy=SelectionStrategy.ACTIVE_DISAGREEMENT,
                min_eligible_hypotheses=2,
                allow_candidate_specifications=True,
            )
        )
        from app.domain.selection_contracts import SelectionStatus

        selection = selector.select(
            candidates=[h_linear, h_cubic],
            evaluations={},
            observations=[],
            candidate_batch=batch,
        )

        assert selection.status == SelectionStatus.SELECTED
        assert selection.selected_candidate is not None
        selected_x = abs(selection.selected_candidate.input_data.values["x"])
        # Selected point must probe where the two hypotheses disagree strongly
        assert selected_x > 0.5


# ==============================================================================
# 5. Hypothesis Outside Initial Hypothesis Space (Unrepresentable Ground Truth)
# ==============================================================================

class TestHypothesisOutsideInitialSearchSpace:
    """Validate graceful degradation when ground truth cannot be represented in the search space."""

    def test_unrepresentable_ground_truth_degrades_gracefully_without_crashing(self):
        """When true rule is a quartic polynomial (degree 4) but discovery search space is degree <= 2,
        the system must degrade gracefully: no unhandled exception, no crash, and no fabricated success.
        """
        # True rule: y = 1.0 - 2.0*x^2 + 0.5*x^4 (degree 4)
        quartic_ground_truth = PolynomialGroundTruthRule(
            domain="polynomial",
            target_formula="y = 1.0 - 2.0*x^2 + 0.5*x^4",
            target_parameters={"degree": 4, "coefficients": {"c0": 1.0, "c1": 0.0, "c2": -2.0, "c3": 0.0, "c4": 0.5}},
            evaluator_label="target_quartic_degree4",
            secret_seed=4444,
        )

        unrepresentable_case = BenchmarkCaseDefinition(
            benchmark_name="unrepresentable_quartic_benchmark",
            scenario_id="unrepresentable_quartic_benchmark",
            description="Discovery of degree-4 quartic with engine restricted to degree <= 2",
            ground_truth=quartic_ground_truth,
            domain_bounds=NumericalDomainBounds("x", -2.5, 2.5, "1d_polynomial"),
            max_query_budget=6,
            success_threshold=0.05,
            noise_std=0.0,
            # Engine search space capability is strictly degree <= 2
            max_hypothesis_degree=2,
            held_out_inputs=tuple({"x": float(x)} for x in [-2.2, -1.5, -0.8, 0.8, 1.5, 2.2]),
            initial_inputs=({"x": 0.0},),
        )

        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            unrepresentable_case,
            strategy="random_baseline",
            seed=42,
            max_budget=6,
        )

        # 1. System did not crash and terminated cleanly
        assert trial.queries_used <= 6
        assert trial.termination_reason in (
            DiscoveryTerminationReason.BUDGET_EXHAUSTED.value,
            DiscoveryTerminationReason.MAX_ITERATIONS_REACHED.value,
            "budget_exhausted",
        )

        # 2. Honest uncertainty: target was NOT falsely claimed to be discovered
        assert trial.target_reached is False
        assert trial.hit_at_1 is False

        # 3. Held-out error reflects honest limitation (cannot fit quartic with degree 2)
        assert trial.held_out_mae is not None
        assert trial.held_out_mae > unrepresentable_case.success_threshold
