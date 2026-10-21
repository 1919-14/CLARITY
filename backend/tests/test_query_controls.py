"""Focused tests for Issue #11 query validation, accounting, retries, and budgets."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import ExperimentInput, Observation, StructuredError
from app.domain.discovery_contracts import (
    DiscoveryRunConfig,
    DiscoveryRunError,
    ExperimentExecutionOutcome,
    StoppingCriteriaConfig,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
    NumericalDomainBounds,
)
from app.domain.ranking_contracts import RankingPolicyConfig


def make_config(**overrides):
    values = {
        "max_query_budget": 3,
        "max_iterations": 5,
        "domain_bounds": NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="1d_polynomial"
        ),
        "hypothesis_config": HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=5, seed=11
        ),
        "ranking_policy": RankingPolicyConfig(
            min_samples_for_plausibility=3, performance_acceptance_threshold=1e9
        ),
        "experiment_pool_size": 4,
    }
    values.update(overrides)
    return DiscoveryRunConfig(**values)


class TimeoutThenSuccessOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def query(self, experiment_input):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("transient timeout")
        return super().query(experiment_input)


class RejectingOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def validate_input(self, experiment_input):
        return StructuredError(code="OUT_OF_RANGE", message="Input is outside permitted range.")

    def query(self, experiment_input):
        self.calls += 1
        return super().query(experiment_input)


class FixedBatchGenerator:
    def __init__(self, experiment_input):
        self.experiment_input = experiment_input

    def generate_batch(self, config_override=None):
        candidate = CandidateExperiment(input_data=self.experiment_input)
        return CandidateExperimentBatch(
            candidates=[candidate], requested_count=1, generated_count=1
        )


def test_transient_timeout_retry_is_bounded_and_each_invocation_consumes_budget():
    oracle = TimeoutThenSuccessOracle()
    result = DiscoveryEngine(
        oracle,
        make_config(max_query_budget=2, max_iterations=3, max_query_retries=1),
    ).run()

    assert oracle.calls == 2
    assert result.query_accounting.attempted_queries == 2
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 1
    assert result.query_accounting.retry_count == 1
    assert result.query_accounting.remaining_queries == 0
    assert len(result.observations) == 1
    assert result.experiment_history[0].outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS


def test_retry_default_is_disabled():
    oracle = TimeoutThenSuccessOracle()
    result = DiscoveryEngine(
        oracle,
        make_config(max_query_budget=2, max_iterations=2, max_query_retries=0),
    ).run()

    assert oracle.calls == 1
    assert result.query_accounting.attempted_queries == 1
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 0
    assert result.query_accounting.retry_count == 0


def test_invalid_candidate_is_rejected_before_oracle_invocation_and_counted():
    oracle = RejectingOracle()
    result = DiscoveryEngine(oracle, make_config(max_query_budget=3)).run()

    assert oracle.calls == 0
    assert result.query_accounting.attempted_queries == 0
    assert result.query_accounting.rejected_queries >= 1
    assert result.query_accounting.remaining_queries == 3
    assert any(record.outcome == ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION
               for record in result.experiment_history)


def test_duplicate_candidate_is_counted_separately_and_not_submitted():
    oracle = PolynomialLocalOracle()
    duplicate = ExperimentInput(values={"x": 0.0}, domain="1d_polynomial")
    initial = Observation(
        observation_id="seed-observation",
        input_id="seed-input",
        input_values={"x": 0.0},
        output_values={"y": 1.0},
    )
    result = DiscoveryEngine(
        oracle,
        make_config(max_query_budget=3, max_iterations=2),
        experiment_generator=FixedBatchGenerator(duplicate),
    ).run([initial])

    assert result.query_accounting.attempted_queries == 0
    assert result.query_accounting.duplicate_queries == 1
    assert result.query_accounting.remaining_queries == 3
    assert any(record.outcome == ExperimentExecutionOutcome.SKIPPED_DUPLICATE
               for record in result.experiment_history)


def test_retry_limit_rejects_invalid_configuration():
    with pytest.raises(ValueError, match="max_query_retries"):
        make_config(max_query_retries=6)


def test_concurrent_run_start_cannot_start_the_same_engine_twice():
    engine = DiscoveryEngine(PolynomialLocalOracle(), make_config(max_query_budget=2))

    def run_engine():
        try:
            return engine.run()
        except DiscoveryRunError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run_engine(), range(2)))

    successful_results = [item for item in results if not isinstance(item, DiscoveryRunError)]
    rejected_starts = [item for item in results if isinstance(item, DiscoveryRunError)]
    assert len(successful_results) == 1
    assert len(rejected_starts) == 1
    assert successful_results[0].query_accounting.attempted_queries <= 2
