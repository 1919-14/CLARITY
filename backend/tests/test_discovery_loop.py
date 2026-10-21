"""Integration tests for the end-to-end discovery loop (Issue #8).

The tests exercise the real component boundaries: candidate generation,
fitting/evaluation, ranking/evidence, experiment generation, the local oracle
adapter, and the planner protocol. Only deliberately failing/malformed
components are faked, so a failure proves the orchestrator's handling rather
than a mock's.

Coverage:
A. Complete multi-iteration run against the local test oracle.
B. Zero query budget with permitted initial observations.
C. Query budget exhaustion at the exact ceiling.
D. Configured stopping criteria and their deterministic precedence.
E. No valid experiment remains (empty pool, duplicate-only pool).
F. Oracle failures and invalid responses.
G. Evaluation/ranking/planner component failures with partial results.
H. Incremental external observation ingestion and identity preservation.
I. Ground-truth isolation (dependency wiring + canary values).
J. History, serialization, and reproducibility.
K. Repeatability of a deterministic scenario.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List, Optional, Sequence

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import (
    EvaluationResult,
    ExperimentInput,
    Observation,
    ObservationStatus,
    OracleInterface,
    StructuredError,
)
from app.domain.discovery_contracts import (
    ERROR_EVALUATION_FAILURE,
    ERROR_INVALID_DISCOVERY_CONFIG,
    ERROR_INVALID_INITIAL_OBSERVATION,
    ERROR_INVALID_OBSERVATION,
    ERROR_INVALID_ORACLE_RESPONSE,
    ERROR_ORACLE_QUERY_FAILURE,
    ERROR_PLANNER_FAILURE,
    ERROR_RANKING_FAILURE,
    ERROR_RUN_NOT_ACTIVE,
    ERROR_RUN_TERMINAL_FAILURE,
    ERROR_DUPLICATE_OBSERVATION_ID,
    ERROR_DUPLICATE_OBSERVATION_INPUT,
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryRunError,
    DiscoveryTerminationReason,
    ExperimentExecutionOutcome,
    StoppingCriteriaConfig,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.evaluation import EvaluationPipeline
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
    ExperimentGenerationStrategy,
    NumericalDomainBounds,
)
from app.domain.ranking_contracts import PlausibilityStatus, RankingPolicyConfig

CANARY = "evaluator-canary-6f3c1b9d-should-never-surface"

# ==============================================================================
# Fixtures and test doubles
# ==============================================================================


def make_config(**overrides: Any) -> DiscoveryRunConfig:
    """A small, deterministic configuration for local discovery runs."""
    defaults: Dict[str, Any] = dict(
        max_query_budget=4,
        max_iterations=6,
        domain_bounds=NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="test_polynomial"
        ),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=5, seed=11
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            performance_acceptance_threshold=1e9,
        ),
        experiment_pool_size=6,
    )
    defaults.update(overrides)
    return DiscoveryRunConfig(**defaults)


def make_observation(
    x: float,
    y: Optional[float] = None,
    *,
    observation_id: Optional[str] = None,
    status: ObservationStatus = ObservationStatus.SUCCESS,
    error: Optional[StructuredError] = None,
) -> Observation:
    return Observation(
        observation_id=observation_id or f"obs-{x}",
        input_id=f"inp-{x}",
        input_values={"x": float(x)},
        output_values=None if y is None else {"y": float(y)},
        status=status,
        error=error,
    )


class CountingOracle(PolynomialLocalOracle):
    """Local deterministic oracle that records every submitted experiment."""

    def __init__(self, **kwargs: Any):
        super().__init__(**kwargs)
        self.submitted: List[Dict[str, Any]] = []

    def query(self, experiment_input: ExperimentInput) -> Observation:
        self.submitted.append(dict(experiment_input.values))
        return super().query(experiment_input)


class FakeOracle(OracleInterface):
    """In-memory oracle with private, evaluator-only ground truth.

    ``__hidden_rule`` is deliberately never exposed through the public oracle
    interface. Tests use it as a canary to prove discovery never receives it.
    """

    def __init__(
        self,
        function=None,
        *,
        min_x: float = -100.0,
        max_x: float = 100.0,
        fail_on_calls: Sequence[int] = (),
        invalid_input_id_on_calls: Sequence[int] = (),
        invalid_output_on_calls: Sequence[int] = (),
        non_success_on_calls: Sequence[int] = (),
        reject_all_inputs: bool = False,
    ):
        self.__function = function or (lambda x: 2.0 * x + 1.0)
        self.__hidden_rule = {
            "secret_coefficients": {"w1": 2.0, "w0": 1.0},
            "canary": CANARY,
        }
        self._min_x = min_x
        self._max_x = max_x
        self._fail_on_calls = set(fail_on_calls)
        self._invalid_input_id_on_calls = set(invalid_input_id_on_calls)
        self._invalid_output_on_calls = set(invalid_output_on_calls)
        self._non_success_on_calls = set(non_success_on_calls)
        self._reject_all_inputs = reject_all_inputs
        self.call_count = 0
        self.validation_count = 0
        self.submitted: List[Dict[str, Any]] = []

    @property
    def domain(self) -> str:
        return "test_oracle"

    def validate_input(self, experiment_input: ExperimentInput) -> Optional[StructuredError]:
        self.validation_count += 1
        if self._reject_all_inputs:
            return StructuredError(
                code="OUT_OF_BOUNDS",
                message="This oracle rejects every proposed input for the test.",
                details={},
            )
        value = experiment_input.values.get("x")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return StructuredError(code="INVALID_PARAMETER_TYPE", message="x must be numeric.")
        if not (self._min_x <= float(value) <= self._max_x):
            return StructuredError(code="OUT_OF_BOUNDS", message="x is out of bounds.")
        return None

    def query(self, experiment_input: ExperimentInput) -> Observation:
        self.call_count += 1
        self.submitted.append(dict(experiment_input.values))
        if self.call_count in self._fail_on_calls:
            raise RuntimeError("simulated oracle transport failure")
        if self.call_count in self._non_success_on_calls:
            return Observation(
                input_id=experiment_input.input_id,
                input_values=dict(experiment_input.values),
                status=ObservationStatus.ORACLE_FAILURE,
                error=StructuredError(code="ORACLE_FAILURE", message="simulated failure"),
                sequence_number=self.call_count,
            )
        if self.call_count in self._invalid_input_id_on_calls:
            return Observation(
                input_id="mismatched-input-id",
                input_values=dict(experiment_input.values),
                output_values={"y": 0.0},
                sequence_number=self.call_count,
            )
        if self.call_count in self._invalid_output_on_calls:
            return Observation(
                input_id=experiment_input.input_id,
                input_values=dict(experiment_input.values),
                output_values={"y": "not-a-number"},
                sequence_number=self.call_count,
            )
        x = float(experiment_input.values["x"])
        return Observation(
            input_id=experiment_input.input_id,
            input_values={"x": x},
            output_values={"y": self.__function(x)},
            sequence_number=self.call_count,
        )

    def evaluator_only_ground_truth(self) -> Dict[str, Any]:
        """Evaluator-side accessor that discovery must never call."""
        return dict(self.__hidden_rule)


class RecordingPlanner:
    """Delegates selection but records exactly what the orchestrator passed."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._delegate = None

    def select_experiment(self, candidates, evaluations, observations, candidate_batch, ranking):
        self.calls.append(
            {
                "candidates": list(candidates),
                "evaluations": dict(evaluations),
                "observations": list(observations),
                "batch": candidate_batch,
                "ranking": ranking,
            }
        )
        for candidate in candidate_batch.candidates:
            return candidate
        return None


class FailingPlanner:
    def select_experiment(self, *args, **kwargs):
        raise RuntimeError("planner exploded")


class FailingRanker:
    def rank_candidates(self, *args, **kwargs):
        raise RuntimeError("ranking exploded")


class FailingPipeline:
    """Wraps the real pipeline and raises on the configured call numbers."""

    def __init__(self, fail_on_calls: Sequence[int] = (), fail_from_call: Optional[int] = None):
        self._real = EvaluationPipeline()
        self._fail_on_calls = set(fail_on_calls)
        self._fail_from_call = fail_from_call
        self.calls = 0

    def evaluate_many(self, requests, observations, dataset_id=None):
        self.calls += 1
        if self.calls in self._fail_on_calls or (
            self._fail_from_call is not None and self.calls >= self._fail_from_call
        ):
            raise RuntimeError("evaluation exploded")
        return self._real.evaluate_many(requests, observations, dataset_id=dataset_id)


class FixedBatchGenerator:
    """Returns one fixed candidate batch, ignoring exclusions."""

    def __init__(self, experiment_input: ExperimentInput):
        self._input = experiment_input

    def generate_batch(self, config_override=None):
        candidate = CandidateExperiment(input_data=self._input)
        return CandidateExperimentBatch(
            candidates=[candidate], requested_count=1, generated_count=1
        )


def successful_inputs(result) -> List[Dict[str, Any]]:
    return [
        dict(record.input_values)
        for record in result.experiment_history
        if record.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS
    ]


# ==============================================================================
# A. Complete multi-iteration run
# ==============================================================================


def test_complete_multi_iteration_run_against_local_oracle():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=4, max_iterations=5))

    result = engine.run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
    assert result.iterations_completed == 4
    assert len(result.observations) == 4
    assert len(oracle.submitted) == 4
    assert result.query_accounting.attempted_queries == 4
    assert result.query_accounting.successful_queries == 4
    assert result.query_accounting.failed_queries == 0
    assert result.remaining_queries == 0

    # One successful experiment record per iteration, in order.
    assert len(result.experiment_history) == 4
    assert [record.iteration for record in result.experiment_history] == [1, 2, 3, 4]
    assert all(
        record.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS
        for record in result.experiment_history
    )
    assert {record.observation_id for record in result.experiment_history} == {
        observation.observation_id for observation in result.observations
    }

    # Every generated candidate is evaluated; the ranking reflects four samples.
    assert result.ranking is not None
    assert result.ranking_is_fresh is True
    assert len(result.hypotheses) == 5
    assert len(result.evaluations) == 5
    best = result.ranked_hypotheses[0]
    assert best.training_sample_count == 4
    assert best.evidence_summary.total_evaluated == 4
    assert best.dataset_id == result.run_id

    # History is ordered and explains each iteration.
    assert [record.iteration for record in result.iteration_history] == [1, 2, 3, 4]
    for record in result.iteration_history:
        assert record.attempted_queries_after == record.attempted_queries_before + 1
        assert record.observation_id is not None
        assert record.top_hypothesis_id is not None
    assert result.errors == ()


def test_ranking_evolves_after_new_observations():
    """Same initial data, two runs: extra evidence changes the leading model."""
    initial = [
        make_observation(0.0, 0.0, observation_id="obs-a"),
        make_observation(1.0, 1.0, observation_id="obs-b"),
        make_observation(4.0, 64.0, observation_id="obs-c"),
    ]
    base_kwargs = dict(
        domain_bounds=NumericalDomainBounds(min_val=0.0, max_val=4.0),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=3, candidate_count=5, seed=3
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3, performance_acceptance_threshold=1e9
        ),
        experiment_pool_size=2,
    )

    initial_engine = DiscoveryEngine(
        FakeOracle(lambda x: x ** 3), make_config(max_query_budget=0, max_iterations=2, **base_kwargs)
    )
    initial_result = initial_engine.run(initial)

    expanded_engine = DiscoveryEngine(
        FakeOracle(lambda x: x ** 3), make_config(max_query_budget=2, max_iterations=3, **base_kwargs)
    )
    expanded_result = expanded_engine.run(initial)

    assert initial_result.ranked_hypotheses[0].complexity == 3  # exact quadratic
    assert len(expanded_result.observations) == 5
    assert expanded_result.ranked_hypotheses[0].complexity == 4  # cubic now identified
    assert expanded_result.ranked_hypotheses[0].training_score == pytest.approx(0.0, abs=1e-9)
    # The same candidate set was evaluated in both runs; only evidence changed.
    initial_specs = {h.formula for h in initial_result.hypotheses}
    expanded_specs = {h.formula for h in expanded_result.hypotheses}
    assert initial_specs == expanded_specs


def test_empty_initial_observations_runs_and_ingests_first_query():
    oracle = FakeOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=1, max_iterations=2))

    result = engine.run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.iterations_completed == 1
    assert len(result.observations) == 1
    assert oracle.call_count == 1
    assert result.query_accounting.attempted_queries == 1
    assert len(result.iteration_history) == 1
    assert result.ranking is not None
    assert result.ranking_is_fresh is True
    # With a single observation, only the constant model can be fitted.
    evaluated = [r for r in result.evaluations if r.status.value == "completed"]
    assert [r.complexity for r in evaluated] == [1]
    # No metric is fabricated for the models that could not be fitted.
    for evaluation in result.evaluations:
        if evaluation.status.value == "failed":
            assert evaluation.metrics == {}
            assert evaluation.fitted_specification is None


# ==============================================================================
# B. Zero budget
# ==============================================================================


def test_zero_budget_evaluates_initial_data_and_never_queries():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=0, max_iterations=3))
    initial = [make_observation(x, 0.5 * x ** 3 - x * x - 2.0 * x + 3.0) for x in (-2, -1, 0, 1)]

    result = engine.run(initial)

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
    assert oracle.submitted == []
    assert result.query_accounting.attempted_queries == 0
    assert result.query_accounting.remaining_queries == 0
    assert result.iterations_completed == 0
    assert len(result.observations) == 4
    assert result.ranking is not None
    assert result.ranked_hypotheses[0].training_sample_count == 4
    assert len(result.evaluations) == len(result.hypotheses)


# ==============================================================================
# C. Budget exhaustion
# ==============================================================================


def test_budget_exhaustion_never_exceeds_configured_ceiling():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=3, max_iterations=10))

    result = engine.run()

    assert len(oracle.submitted) == 3
    assert result.query_accounting.attempted_queries == 3
    assert result.query_accounting.remaining_queries == 0
    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
    assert len(result.observations) == 3


def test_initial_observations_do_not_consume_query_budget():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=2, max_iterations=5))
    initial = [make_observation(-1.0, 2.0), make_observation(0.0, 1.0)]

    result = engine.run(initial)

    assert len(result.observations) == 4  # 2 initial + 2 queried
    assert result.query_accounting.attempted_queries == 2
    assert len(oracle.submitted) == 2


# ==============================================================================
# D. Stopping criteria
# ==============================================================================


def _linear_initial_observations():
    return [
        make_observation(-1.0, -1.0, observation_id="obs-l1"),
        make_observation(0.0, 1.0, observation_id="obs-l2"),
        make_observation(1.0, 3.0, observation_id="obs-l3"),
        make_observation(2.0, 5.0, observation_id="obs-l4"),
    ]


def test_configured_plausibility_criterion_stops_before_spending_budget():
    oracle = FakeOracle(lambda x: 2.0 * x + 1.0)
    config = make_config(
        max_query_budget=3,
        max_iterations=3,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=1, candidate_count=2, seed=5
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            tolerance_absolute=1e-9,
            tolerance_relative=0.0,
            performance_acceptance_threshold=1e-6,
        ),
        stopping_criteria=StoppingCriteriaConfig(target_plausible_candidate_count=1),
    )
    result = DiscoveryEngine(oracle, config).run(_linear_initial_observations())

    assert (
        result.termination_reason
        == DiscoveryTerminationReason.STOPPING_CRITERION_PLAUSIBILITY_REACHED
    )
    assert result.status == DiscoveryLifecycleStatus.TERMINATED_STOPPING_CRITERION
    assert oracle.call_count == 0
    assert result.query_accounting.attempted_queries == 0
    assert any(
        candidate.status == PlausibilityStatus.PLAUSIBLE
        for candidate in result.ranked_hypotheses
    )


def test_configured_metric_threshold_stops_before_spending_budget():
    oracle = FakeOracle(lambda x: 2.0 * x + 1.0)
    config = make_config(
        max_query_budget=2,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=1, candidate_count=2, seed=5
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3, performance_acceptance_threshold=1e9
        ),
        stopping_criteria=StoppingCriteriaConfig(target_primary_metric_threshold=1e-6),
    )
    result = DiscoveryEngine(oracle, config).run(_linear_initial_observations())

    assert (
        result.termination_reason
        == DiscoveryTerminationReason.STOPPING_CRITERION_METRIC_THRESHOLD_REACHED
    )
    assert oracle.call_count == 0


def test_metric_threshold_without_policy_acceptance_is_not_success():
    """A low score must not be treated as discovery acceptance on its own."""
    oracle = FakeOracle(lambda x: 2.0 * x + 1.0)
    config = make_config(
        max_query_budget=1,
        max_iterations=2,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=1, candidate_count=2, seed=5
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            tolerance_absolute=1e-9,
            tolerance_relative=0.0,
            performance_acceptance_threshold=None,
        ),
        # The training score is exactly 0, far below this stop threshold, but
        # the ranking policy deliberately leaves the candidate UNCERTAIN.
        stopping_criteria=StoppingCriteriaConfig(target_primary_metric_threshold=1e-6),
    )
    result = DiscoveryEngine(oracle, config).run(_linear_initial_observations())

    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert oracle.call_count == 1
    assert all(
        candidate.status != PlausibilityStatus.PLAUSIBLE
        for candidate in result.ranked_hypotheses
    )


def test_unconfigured_acceptance_threshold_never_stops_as_success():
    oracle = FakeOracle(lambda x: 2.0 * x + 1.0)
    config = make_config(
        max_query_budget=1,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=1, candidate_count=2, seed=5
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            performance_acceptance_threshold=None,
        ),
        stopping_criteria=StoppingCriteriaConfig(target_plausible_candidate_count=1),
    )
    result = DiscoveryEngine(oracle, config).run(_linear_initial_observations())

    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
    assert oracle.call_count == 1
    assert all(
        candidate.status != PlausibilityStatus.PLAUSIBLE
        for candidate in result.ranked_hypotheses
    )


def test_max_iterations_terminates_the_loop():
    oracle = CountingOracle()
    config = make_config(max_query_budget=10, max_iterations=2)
    result = DiscoveryEngine(oracle, config).run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_MAX_ITERATIONS
    assert result.termination_reason == DiscoveryTerminationReason.MAX_ITERATIONS_REACHED
    assert result.iterations_completed == 2
    assert oracle.submitted and len(oracle.submitted) == 2


def test_termination_precedence_prefers_stopping_criterion_over_budget():
    oracle = FakeOracle(lambda x: 2.0 * x + 1.0)
    config = make_config(
        max_query_budget=0,
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=1, candidate_count=2, seed=5
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3,
            tolerance_absolute=1e-9,
            tolerance_relative=0.0,
            performance_acceptance_threshold=1e-6,
        ),
        stopping_criteria=StoppingCriteriaConfig(target_plausible_candidate_count=1),
    )
    result = DiscoveryEngine(oracle, config).run(_linear_initial_observations())

    assert (
        result.termination_reason
        == DiscoveryTerminationReason.STOPPING_CRITERION_PLAUSIBILITY_REACHED
    )
    assert oracle.call_count == 0


# ==============================================================================
# E. No valid experiment remains
# ==============================================================================


def test_degenerate_domain_exhausts_the_experiment_pool():
    oracle = FakeOracle()
    config = make_config(
        max_query_budget=5,
        max_iterations=5,
        domain_bounds=NumericalDomainBounds(min_val=0.5, max_val=0.5),
        experiment_pool_size=3,
    )
    result = DiscoveryEngine(oracle, config).run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS
    assert result.termination_reason == DiscoveryTerminationReason.EXPERIMENT_POOL_EXHAUSTED
    assert oracle.call_count == 1
    assert result.query_accounting.attempted_queries == 1
    assert len(result.observations) == 1  # the valid observation survives
    assert result.ranking is not None


def test_duplicate_only_experiment_pool_terminates_without_querying():
    oracle = FakeOracle()
    committed_input = ExperimentInput(values={"x": 0.0}, domain="test_polynomial")
    result = DiscoveryEngine(
        oracle,
        make_config(max_query_budget=3, max_iterations=3, experiment_pool_size=1),
        experiment_generator=FixedBatchGenerator(committed_input),
    ).run([make_observation(0.0, 1.0)])

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS
    assert oracle.call_count == 0
    assert result.query_accounting.attempted_queries == 0
    assert len(result.observations) == 1
    assert any(
        record.outcome == ExperimentExecutionOutcome.SKIPPED_DUPLICATE
        for record in result.experiment_history
    )


def test_pre_invocation_rejection_does_not_consume_budget():
    oracle = FakeOracle(reject_all_inputs=True)
    result = DiscoveryEngine(
        oracle, make_config(max_query_budget=3, max_iterations=3, experiment_pool_size=2)
    ).run()

    assert result.status == DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS
    assert oracle.call_count == 0
    assert result.query_accounting.attempted_queries == 0
    assert result.query_accounting.remaining_queries == 3
    assert result.experiment_history
    assert all(
        record.outcome == ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION
        for record in result.experiment_history
    )


# ==============================================================================
# F. Oracle failures and invalid responses
# ==============================================================================


def test_raising_oracle_consumes_budget_and_stops_at_failure_limit():
    oracle = FakeOracle(fail_on_calls={1, 2, 3})
    config = make_config(
        max_query_budget=5,
        max_iterations=5,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=2),
    )
    result = DiscoveryEngine(oracle, config).run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.CONSECUTIVE_ORACLE_FAILURES
    assert oracle.call_count == 2
    assert result.query_accounting.attempted_queries == 2
    assert result.query_accounting.failed_queries == 2
    assert result.query_accounting.successful_queries == 0
    assert result.observations == ()
    assert any(error.code == ERROR_ORACLE_QUERY_FAILURE for error in result.warnings)


def test_invalid_oracle_response_is_not_committed_and_preserves_prior_data():
    oracle = FakeOracle(invalid_input_id_on_calls={2})
    config = make_config(
        max_query_budget=2,
        max_iterations=2,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
    )
    initial = [make_observation(-1.5, 2.0, observation_id="obs-initial")]
    result = DiscoveryEngine(oracle, config).run(initial)

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.CONSECUTIVE_ORACLE_FAILURES
    # The initial observation and the first good query remain committed; the
    # malformed second response is rejected and never becomes evidence.
    assert len(result.observations) == 2
    assert result.observations[0].observation_id == "obs-initial"
    assert all(o.status == ObservationStatus.SUCCESS for o in result.observations)
    assert result.experiment_history[-1].outcome == ExperimentExecutionOutcome.INVOKED_FAILED
    assert result.experiment_history[-1].observation_id is None
    assert result.query_accounting.attempted_queries == 2
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 1
    assert any(error.code == ERROR_INVALID_ORACLE_RESPONSE for error in result.warnings)


def test_non_success_oracle_observation_is_a_failed_query():
    oracle = FakeOracle(non_success_on_calls={1})
    config = make_config(
        max_query_budget=2,
        max_iterations=2,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
    )
    result = DiscoveryEngine(oracle, config).run()

    assert result.termination_reason == DiscoveryTerminationReason.CONSECUTIVE_ORACLE_FAILURES
    assert result.observations == ()
    assert result.query_accounting.attempted_queries == 1
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 0


def test_invalid_output_value_is_rejected():
    oracle = FakeOracle(invalid_output_on_calls={1})
    config = make_config(
        max_query_budget=1,
        max_iterations=1,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
    )
    result = DiscoveryEngine(oracle, config).run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.observations == ()
    assert result.query_accounting.failed_queries == 1
    assert any(error.code == ERROR_INVALID_ORACLE_RESPONSE for error in result.warnings)


# ==============================================================================
# G. Component failures and partial results
# ==============================================================================


def test_evaluation_failure_preserves_committed_observation_and_prior_ranking():
    oracle = FakeOracle()
    pipeline = FailingPipeline(fail_from_call=2)
    config = make_config(max_query_budget=2, max_iterations=2)
    initial = [make_observation(-1.0, 3.0, observation_id="obs-initial")]

    result = DiscoveryEngine(oracle, config, evaluation_pipeline=pipeline).run(initial)

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    # The new observation is kept, the previous ranking snapshot survives.
    assert len(result.observations) == 2
    assert result.ranking is not None
    assert result.ranking_is_fresh is False
    assert any(error.code == ERROR_EVALUATION_FAILURE for error in result.errors)
    assert result.iteration_history[-1].ranking_is_fresh is False
    assert result.limitations


def test_ranking_failure_reports_failure_without_losing_observations():
    oracle = FakeOracle()
    result = DiscoveryEngine(
        oracle, make_config(max_query_budget=1, max_iterations=1), ranker=FailingRanker()
    ).run([make_observation(-1.0, 3.0, observation_id="obs-initial")])

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    assert result.ranking is None
    assert result.ranking_is_fresh is False
    assert [o.observation_id for o in result.observations] == ["obs-initial"]
    assert any(error.code == ERROR_RANKING_FAILURE for error in result.errors)


def test_planner_failure_reports_structured_failure():
    oracle = FakeOracle()
    result = DiscoveryEngine(
        oracle, make_config(max_query_budget=1, max_iterations=1), planner=FailingPlanner()
    ).run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    assert oracle.call_count == 0
    assert any(error.code == ERROR_PLANNER_FAILURE for error in result.errors)
    assert result.query_accounting.attempted_queries == 0


def test_candidate_generation_failure_returns_structured_failed_result():
    class ExplodingGenerator:
        def generate(self):
            raise RuntimeError("generation exploded")

    oracle = FakeOracle()
    result = DiscoveryEngine(
        oracle, make_config(), candidate_generator=ExplodingGenerator()
    ).run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.UNRECOVERABLE_ERROR
    assert oracle.call_count == 0
    assert result.hypotheses == ()
    assert result.errors and result.errors[0].code == "CANDIDATE_GENERATION_FAILURE"


def test_invalid_initial_observation_fails_before_querying():
    oracle = FakeOracle()
    broken = make_observation(
        -1.0,
        None,
        observation_id="obs-broken",
        status=ObservationStatus.ORACLE_FAILURE,
        error=StructuredError(code="ORACLE_FAILURE", message="no output"),
    )
    result = DiscoveryEngine(oracle, make_config(max_query_budget=2)).run([broken])

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert oracle.call_count == 0
    assert result.query_accounting.attempted_queries == 0
    assert result.errors[0].code == ERROR_INVALID_INITIAL_OBSERVATION


# ==============================================================================
# H. Incremental updates and identity preservation
# ==============================================================================


def test_resume_ingests_external_observation_and_preserves_identities_and_budget():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=1, max_iterations=1))
    result = engine.run()

    observation_ids = [o.observation_id for o in result.observations]
    hypothesis_ids = [h.hypothesis_id for h in result.hypotheses]
    attempted = result.query_accounting.attempted_queries

    update = make_observation(1.25, 0.5 * 1.25 ** 3 - 1.25 ** 2 - 2.0 * 1.25 + 3.0,
                             observation_id="obs-external")
    updated = engine.resume([update])

    assert [o.observation_id for o in updated.observations[: len(observation_ids)]] == observation_ids
    assert "obs-external" in [o.observation_id for o in updated.observations]
    assert [h.hypothesis_id for h in updated.hypotheses] == hypothesis_ids
    assert updated.query_accounting.attempted_queries == attempted  # no budget consumed
    assert len(updated.ingestion_history) == 1
    assert updated.ingestion_history[0].ranking_is_fresh is True
    assert updated.ranking_is_fresh is True
    # The external observation participates in the refreshed evaluation.
    assert updated.ranked_hypotheses[0].training_sample_count == len(updated.observations)


def test_resume_rejects_identity_collisions_and_duplicate_inputs_without_mutation():
    engine = DiscoveryEngine(CountingOracle(), make_config(max_query_budget=1, max_iterations=1))
    result = engine.run()
    existing = result.observations[0]

    with pytest.raises(DiscoveryRunError) as duplicate_id:
        engine.resume([
            make_observation(
                float(existing.input_values["x"]) + 0.5,
                1.0,
                observation_id=existing.observation_id,
            )
        ])
    assert duplicate_id.value.error.code == ERROR_DUPLICATE_OBSERVATION_ID

    with pytest.raises(DiscoveryRunError) as duplicate_input:
        engine.resume([
            make_observation(
                float(existing.input_values["x"]),
                1.0,
                observation_id="obs-other-id",
            )
        ])
    assert duplicate_input.value.error.code == ERROR_DUPLICATE_OBSERVATION_INPUT

    with pytest.raises(DiscoveryRunError) as non_success:
        engine.resume([
            make_observation(
                3.0,
                None,
                observation_id="obs-failed",
                status=ObservationStatus.ORACLE_FAILURE,
                error=StructuredError(code="ORACLE_FAILURE", message="failed"),
            )
        ])
    assert non_success.value.error.code == ERROR_INVALID_OBSERVATION

    # Rejected updates never mutated committed state or the lifecycle.
    assert len(engine.observations) == len(result.observations)
    assert engine.status == result.status
    assert engine.query_accounting == result.query_accounting


def test_add_observation_after_budget_exhaustion_is_rejected_without_mutation():
    engine = DiscoveryEngine(CountingOracle(), make_config(max_query_budget=2, max_iterations=2))
    result = engine.run()
    ranking = engine.ranking
    observation_ids = [o.observation_id for o in engine.observations]

    with pytest.raises(DiscoveryRunError) as error:
        engine.add_observation(make_observation(5.0, 1.0, observation_id="obs-late"))

    assert error.value.error.code == ERROR_RUN_NOT_ACTIVE
    assert [o.observation_id for o in engine.observations] == observation_ids
    assert engine.ranking is ranking
    assert engine.query_accounting == result.query_accounting
    assert engine.status == result.status


def test_add_observation_after_failed_run_is_rejected_without_mutation():
    oracle = FakeOracle(fail_on_calls={1})
    engine = DiscoveryEngine(
        oracle,
        make_config(
            max_query_budget=2,
            max_iterations=2,
            stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
        ),
    )
    result = engine.run()
    assert result.status == DiscoveryLifecycleStatus.FAILED
    observation_ids = [o.observation_id for o in engine.observations]

    with pytest.raises(DiscoveryRunError) as error:
        engine.add_observation(make_observation(5.0, 1.0, observation_id="obs-late"))

    assert error.value.error.code == ERROR_RUN_NOT_ACTIVE
    assert [o.observation_id for o in engine.observations] == observation_ids
    assert engine.query_accounting == result.query_accounting
    assert engine.status == DiscoveryLifecycleStatus.FAILED


def test_resume_is_rejected_for_failed_runs_without_mutation():
    oracle = FakeOracle(invalid_input_id_on_calls={1})
    engine = DiscoveryEngine(
        oracle,
        make_config(
            max_query_budget=2,
            max_iterations=2,
            stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
        ),
    )
    result = engine.run()
    assert result.status == DiscoveryLifecycleStatus.FAILED
    observation_ids = [o.observation_id for o in engine.observations]
    ranking = engine.ranking

    with pytest.raises(DiscoveryRunError) as error:
        engine.resume([make_observation(5.0, 1.0, observation_id="obs-late")])

    assert error.value.error.code == ERROR_RUN_TERMINAL_FAILURE
    assert [o.observation_id for o in engine.observations] == observation_ids
    assert engine.ranking is ranking
    assert engine.query_accounting == result.query_accounting


def test_resume_does_not_reset_budget_or_iteration_counters():
    oracle = CountingOracle()
    engine = DiscoveryEngine(oracle, make_config(max_query_budget=2, max_iterations=1))
    first = engine.run()
    assert first.termination_reason == DiscoveryTerminationReason.MAX_ITERATIONS_REACHED
    assert first.query_accounting.attempted_queries == 1

    updated = engine.resume([make_observation(2.5, 10.0, observation_id="obs-resume")])

    # The iteration bound is global to the run, not reset by resume.
    assert updated.termination_reason == DiscoveryTerminationReason.MAX_ITERATIONS_REACHED
    assert updated.query_accounting == first.query_accounting
    assert len(oracle.submitted) == 1
    assert len(updated.observations) == len(first.observations) + 1


def test_resume_rejects_duplicate_inputs_within_one_batch_without_mutation():
    engine = DiscoveryEngine(CountingOracle(), make_config(max_query_budget=1, max_iterations=1))
    result = engine.run()
    observation_ids = [o.observation_id for o in engine.observations]

    with pytest.raises(DiscoveryRunError) as error:
        engine.resume(
            [
                make_observation(1.5, 1.0, observation_id="obs-batch-a"),
                make_observation(1.5, 2.0, observation_id="obs-batch-b"),
            ]
        )

    assert error.value.error.code == ERROR_DUPLICATE_OBSERVATION_INPUT
    assert [o.observation_id for o in engine.observations] == observation_ids
    assert engine.query_accounting == result.query_accounting


def test_external_duplicate_policy_is_independent_of_query_policy():
    # Oracle repeats allowed, external repeats rejected.
    engine = DiscoveryEngine(
        CountingOracle(),
        make_config(
            max_query_budget=1,
            max_iterations=1,
            allow_duplicate_queries=True,
            allow_duplicate_external_observations=False,
        ),
    )
    result = engine.run()
    x = result.observations[0].input_values["x"]
    with pytest.raises(DiscoveryRunError) as error:
        engine.resume([make_observation(x, 999.0, observation_id="obs-dup")])
    assert error.value.error.code == ERROR_DUPLICATE_OBSERVATION_INPUT

    # Oracle repeats rejected (default), external repeats explicitly allowed.
    engine2 = DiscoveryEngine(
        CountingOracle(),
        make_config(
            max_query_budget=1,
            max_iterations=1,
            allow_duplicate_external_observations=True,
        ),
    )
    result2 = engine2.run()
    x2 = result2.observations[0].input_values["x"]
    updated = engine2.resume([make_observation(x2, 999.0, observation_id="obs-dup-2")])

    assert len(updated.observations) == len(result2.observations) + 1
    assert updated.query_accounting == result2.query_accounting


class DuplicateResultPipeline:
    """Returns one result per request but reuses the first hypothesis ID."""

    def evaluate_many(self, requests, observations, dataset_id=None):
        if not requests:
            return []
        hypothesis_id = requests[0].hypothesis.hypothesis_id
        return [EvaluationResult(hypothesis_id=hypothesis_id) for _ in requests]


def test_evaluation_with_duplicate_hypothesis_ids_fails_safely():
    result = DiscoveryEngine(
        FakeOracle(), make_config(), evaluation_pipeline=DuplicateResultPipeline()
    ).run([make_observation(-1.0, 3.0, observation_id="obs-initial")])

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    assert any(error.code == ERROR_EVALUATION_FAILURE for error in result.errors)
    assert [o.observation_id for o in result.observations] == ["obs-initial"]
    assert result.ranking is None
    assert result.ranking_is_fresh is False


def test_add_observation_requires_an_initialized_run():
    engine = DiscoveryEngine(CountingOracle(), make_config())
    with pytest.raises(DiscoveryRunError) as error:
        engine.add_observation(make_observation(0.0, 1.0))
    assert error.value.error.code == ERROR_INVALID_DISCOVERY_CONFIG


# ==============================================================================
# I. Ground-truth isolation
# ==============================================================================


def test_engine_depends_only_on_public_oracle_components():
    source = inspect.getsource(DiscoveryEngine)
    assert "GroundTruthRule" not in source
    assert "_evaluator_get_ground_truth" not in source
    assert "evaluator_only_ground_truth" not in source

    import app.domain.discovery_engine as engine_module

    assert not hasattr(engine_module, "GroundTruthRule")
    assert "GroundTruthRule" not in engine_module.__dict__


def test_evaluator_only_canary_never_reaches_result_or_history():
    oracle = FakeOracle()
    planner = RecordingPlanner()
    result = DiscoveryEngine(
        oracle, make_config(max_query_budget=2, max_iterations=2), planner=planner
    ).run([make_observation(-1.0, 3.0)])

    serialized = json.dumps(result.to_dict())
    assert CANARY not in serialized
    assert "secret_coefficients" not in serialized
    assert CANARY not in str([error.to_dict() for error in result.errors])
    assert CANARY not in str([warning.to_dict() for warning in result.warnings])

    # The planner only receives permitted discovery objects, never the oracle.
    assert planner.calls
    for call in planner.calls:
        assert set(call) == {"candidates", "evaluations", "observations", "batch", "ranking"}
        assert CANARY not in json.dumps(
            {
                "candidates": [c.hypothesis_id for c in call["candidates"]],
                "evaluations": [e.hypothesis_id for e in call["evaluations"].values()],
                "observations": [o.observation_id for o in call["observations"]],
                "batch": [c.candidate_id for c in call["batch"].candidates],
            }
        )
    assert oracle.evaluator_only_ground_truth()  # canary exists on the evaluator side


def test_result_serialization_rejects_leaked_ground_truth_keys():
    from app.domain.discovery_contracts import DiscoveryRunResult, QueryAccounting
    from app.domain.discovery_contracts import DiscoveryRunConfig

    result = DiscoveryRunResult(
        run_id="run-test",
        status=DiscoveryLifecycleStatus.FAILED,
        termination_reason=DiscoveryTerminationReason.UNRECOVERABLE_ERROR,
        config=DiscoveryRunConfig(),
        iterations_completed=0,
        query_accounting=QueryAccounting(0, 0, 0, 0),
        warnings=(StructuredError(code="X", message="y", details={"true_rule": 1.0}),),
    )
    with pytest.raises(ValueError):
        result.to_dict()


# ==============================================================================
# J. History and reproducibility
# ==============================================================================


def test_result_contains_sanitized_reproducible_configuration_and_ordered_history():
    oracle = CountingOracle()
    config = make_config(max_query_budget=2, max_iterations=3, experiment_seed=42)
    result = DiscoveryEngine(oracle, config).run()

    payload = result.to_dict()
    assert payload["config"]["max_query_budget"] == 2
    assert payload["config"]["max_iterations"] == 3
    assert payload["config"]["hypothesis_config"]["seed"] == 11
    assert payload["config"]["experiment_seed"] == 42
    assert payload["config"]["experiment_strategy"] == "grid"
    assert payload["termination_reason"] == "budget_exhausted"
    assert payload["query_accounting"]["remaining_queries"] == 0

    sequences = [record["sequence"] for record in payload["experiment_history"]]
    assert sequences == sorted(sequences)
    iterations = [record["iteration"] for record in payload["iteration_history"]]
    assert iterations == sorted(iterations)

    # Serialization is JSON-safe and round-trips through json.
    assert json.loads(json.dumps(payload))["run_id"] == result.run_id


def test_failed_run_still_returns_history_and_partial_results():
    oracle = FakeOracle(fail_on_calls={2})
    config = make_config(
        max_query_budget=3,
        max_iterations=3,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
    )
    result = DiscoveryEngine(oracle, config).run()

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert len(result.observations) == 1
    assert len(result.experiment_history) == 2
    assert len(result.iteration_history) == 2
    payload = result.to_dict()
    assert payload["observations"]
    assert payload["experiment_history"][1]["outcome"] == "invoked_failed"


def test_deterministic_scenario_is_repeatable():
    def execute():
        oracle = CountingOracle()
        config = make_config(max_query_budget=3, max_iterations=3)
        return DiscoveryEngine(oracle, config).run()

    first = execute()
    second = execute()

    assert successful_inputs(first) == successful_inputs(second)
    assert [o.to_dict()["output_values"] for o in first.observations] == [
        o.to_dict()["output_values"] for o in second.observations
    ]
    first_scores = sorted(
        (round(ranked.training_score, 9) if ranked.training_score is not None else None,
         ranked.complexity)
        for ranked in first.ranked_hypotheses
    )
    second_scores = sorted(
        (round(ranked.training_score, 9) if ranked.training_score is not None else None,
         ranked.complexity)
        for ranked in second.ranked_hypotheses
    )
    assert first_scores == second_scores
    assert first.run_id != second.run_id  # identifiers are run-scoped, not evidence


# ==============================================================================
# Configuration validation
# ==============================================================================


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_query_budget": -1},
        {"max_iterations": 0},
        {"experiment_pool_size": 0},
        {"query_exclusion_tolerance": -0.1},
        {"experiment_seed": "not-a-seed"},
        {"allow_duplicate_external_observations": "yes"},
    ],
)
def test_invalid_run_configuration_is_rejected_before_any_oracle_call(overrides):
    with pytest.raises(ValueError):
        make_config(**overrides)


def test_unsupported_ranking_metric_is_rejected_at_engine_construction():
    config = make_config(ranking_policy=RankingPolicyConfig(primary_metric="accuracy"))
    with pytest.raises(DiscoveryRunError) as error:
        DiscoveryEngine(FakeOracle(), config)
    assert error.value.error.code == ERROR_INVALID_DISCOVERY_CONFIG


def test_engine_rejects_objects_that_are_not_oracles():
    with pytest.raises(DiscoveryRunError):
        DiscoveryEngine(object(), make_config())


def test_run_cannot_be_executed_twice_on_the_same_engine():
    engine = DiscoveryEngine(FakeOracle(), make_config(max_query_budget=1, max_iterations=1))
    engine.run()
    with pytest.raises(DiscoveryRunError):
        engine.run()


def test_ungrid_strategy_still_respects_budget():
    oracle = CountingOracle()
    config = make_config(
        max_query_budget=2,
        max_iterations=4,
        experiment_strategy=ExperimentGenerationStrategy.STRATIFIED_RANDOM,
        experiment_seed=7,
    )
    result = DiscoveryEngine(oracle, config).run()

    assert len(oracle.submitted) == 2
    assert result.query_accounting.attempted_queries == 2
    assert len(set(tuple(sorted(v.items())) for v in oracle.submitted)) == 2
