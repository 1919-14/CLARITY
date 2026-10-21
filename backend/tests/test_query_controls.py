import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import ExperimentInput, Observation, OracleQueryError, StructuredError
from app.domain.discovery_contracts import (
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryRunError,
    DiscoveryTerminationReason,
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
        make_config(
            max_query_budget=2,
            max_iterations=2,
            max_query_retries=0,
            stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
        ),
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
    # Note: This test verifies single-engine lifecycle concurrency protection via
    # engine-level lock (_lifecycle_lock). It does NOT cover multi-engine or
    # distributed worker deduplication or shared budget enforcement, which is
    # deferred scope coordinated by the per-run budget manager in Issue #14.
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


# ==============================================================================
# Additional Reviewer-Required Tests (T-01, T-02, T-03, T-08, T-09)
# ==============================================================================


def test_retry_succeeds_and_observation_committed():
    """T-01: Oracle fails once then succeeds; budget and observation are properly tracked."""
    oracle = TimeoutThenSuccessOracle()
    config = make_config(max_query_budget=2, max_iterations=2, max_query_retries=2)
    result = DiscoveryEngine(oracle, config).run()

    assert oracle.calls == 2
    assert result.query_accounting.attempted_queries == 2
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 1
    assert result.query_accounting.retry_count == 1
    assert len(result.observations) == 1
    assert result.experiment_history[0].outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS


class AlwaysTimeoutOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def query(self, experiment_input):
        self.calls += 1
        raise TimeoutError("permanent network timeout")


def test_retry_budget_exhausted_before_retry_attempt():
    """T-02: Retry bounded by query budget ceiling."""
    oracle = AlwaysTimeoutOracle()
    config = make_config(max_query_budget=2, max_iterations=3, max_query_retries=2)
    result = DiscoveryEngine(oracle, config).run()

    assert oracle.calls == 2
    assert result.query_accounting.attempted_queries == 2
    assert result.query_accounting.failed_queries == 2
    assert result.query_accounting.successful_queries == 0
    assert result.query_accounting.retry_count == 1
    assert result.query_accounting.remaining_queries == 0
    assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
    assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED


class NonRetryableFailureOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def query(self, experiment_input):
        self.calls += 1
        raise ValueError("Non-transient domain calculation error")


def test_non_retryable_failure_no_retry():
    """T-03: Non-retryable exception terminates immediately with retry_count=0."""
    oracle = NonRetryableFailureOracle()
    config = make_config(
        max_query_budget=3,
        max_iterations=2,
        max_query_retries=2,
        stopping_criteria=StoppingCriteriaConfig(consecutive_failure_limit=1),
    )
    result = DiscoveryEngine(oracle, config).run()

    assert oracle.calls == 1
    assert result.query_accounting.attempted_queries == 1
    assert result.query_accounting.failed_queries == 1
    assert result.query_accounting.successful_queries == 0
    assert result.query_accounting.retry_count == 0
    assert result.experiment_history[0].outcome == ExperimentExecutionOutcome.INVOKED_FAILED


class SecretLeakingOracle(PolynomialLocalOracle):
    def query(self, experiment_input):
        raise OracleQueryError(
            StructuredError(
                code="ORACLE_FAILURE",
                message="Sensors failed",
                details={"hidden_rule": "y = 2x + 1", "secret_key": "xyz123"},
            )
        )


def test_oracle_error_details_do_not_leak_hidden_rule():
    """T-08: Oracle error details are sanitized and do not leak private evaluator attributes."""
    oracle = SecretLeakingOracle()
    config = make_config(max_query_budget=2, max_iterations=1, max_query_retries=0)
    result = DiscoveryEngine(oracle, config).run()

    payload_str = json.dumps(result.to_dict())
    assert "hidden_rule" not in payload_str
    assert "secret_key" not in payload_str

    for warning in result.warnings:
        warning_dict = warning.to_dict()
        assert "hidden_rule" not in json.dumps(warning_dict)
        if warning.details:
            assert "hidden_rule" not in warning.details

    for error in result.errors:
        error_dict = error.to_dict()
        assert "hidden_rule" not in json.dumps(error_dict)
        if error.details:
            assert "hidden_rule" not in error.details

    for record in result.experiment_history:
        if record.error:
            record_dict = record.to_dict()
            assert "hidden_rule" not in json.dumps(record_dict)
            if record.error.details:
                assert "hidden_rule" not in record.error.details


class FaultyValidationOracle(PolynomialLocalOracle):
    def validate_input(self, experiment_input):
        raise RuntimeError("Oracle validation sensor crashed")


def test_component_failure_leaves_engine_in_failed_status():
    """T-09: Component failure sets FAILED status while preserving earlier observations."""
    oracle = FaultyValidationOracle()
    initial_obs = Observation(
        observation_id="obs-preserved",
        input_id="input-init",
        input_values={"x": 0.5},
        output_values={"y": 1.5},
    )
    config = make_config(max_query_budget=3, max_iterations=2)
    engine = DiscoveryEngine(oracle, config)
    result = engine.run([initial_obs])

    assert result.status == DiscoveryLifecycleStatus.FAILED
    assert result.termination_reason == DiscoveryTerminationReason.COMPONENT_FAILURE
    assert len(result.observations) == 1
    assert result.observations[0].observation_id == "obs-preserved"


