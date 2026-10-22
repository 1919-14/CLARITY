"""Offline tests for parallel hypothesis evaluation (Issue #13).

Every test runs without Nodus credentials, credits, or network access. Remote
behaviour is exercised against a faithful fake that validates payloads through
the adapter's own validator, round-trips them through JSON, and executes
successes with the real worker function. A separate section stubs the SDK module
to test the adapter's bounded-concurrency batch path.

Coverage
--------
A. Configuration and explicit execution modes.
B. Task payload/result contracts, serialization, and identity invariants.
C. Local sequential mode.
D. Remote orchestration: correlation, ordering, concurrency, metrics.
E. Failures, timeouts, malformed/missing/duplicate results, retries, cancel.
F. Scientific correctness: no fabricated scores, ground-truth isolation.
G. Discovery-loop integration and oracle-query non-duplication.
H. Adapter batch path (one client scope, bounded concurrency, timeout cancel).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from app.adapters.nodus.adapter import NodusExecutionAdapter
from app.adapters.nodus.config import NodusConfig
from app.adapters.nodus.contracts import (
    ExecutionMode,
    NodusResult,
    NodusTask,
    validate_json_payload,
)
from app.adapters.nodus.errors import NodusConfigurationError
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import (
    EvaluationRequest,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.discovery_contracts import DiscoveryRunConfig
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.evaluation import EvaluationPipeline
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.domain.ranking_contracts import RankingPolicyConfig
from app.services.evaluation_task_runtime import (
    ERROR_INVALID_TASK_PAYLOAD,
    ERROR_INVALID_TASK_RESULT,
    EVALUATION_TASK_SCHEMA_VERSION,
    TASK_RESULT_FAILED,
    TASK_RESULT_SUCCEEDED,
    TaskContractError,
    build_evaluation_task_payload,
    evaluation_result_from_dict,
    execute_evaluation_task_payload,
    parse_task_result_envelope,
    validate_evaluation_task_payload,
)
from app.services.parallel_evaluation import (
    ERROR_CANCELLED,
    ERROR_DISPATCH_FAILED,
    ERROR_MISSING_TASK_RESULT,
    MODE_LOCAL_SEQUENTIAL,
    MODE_NODUS_PARALLEL,
    EvaluationTaskResult,
    ParallelConfigurationError,
    ParallelEvaluationConfig,
    ParallelEvaluationPipeline,
    ParallelEvaluationRunner,
    ParallelExecutionMode,
    TaskOutcome,
)

SECRET_CANARY = "nodus_sk_canary_8f31c2_should_never_be_logged"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def truth(x: float) -> float:
    return 3.0 - (2.0 * x) - (x**2) + (0.5 * (x**3))


def make_candidate(degree: int = 2, hypothesis_id: str = "hyp-1") -> HypothesisRepresentation:
    coefficients = [0.0] * (degree + 1)
    coefficients[degree] = 1.0
    specification = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
        "input_variable": "x",
        "output_variable": "y",
    }
    return HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula=f"candidate-degree-{degree}",
        parameters=specification,
        complexity=degree + 1,
        hypothesis_id=hypothesis_id,
    )


def make_observations(count: int = 5, start: int = 0) -> Dict[str, Observation]:
    points = [-2.0, -1.0, 0.0, 1.0, 2.0, 3.0, 4.0]
    observations: Dict[str, Observation] = {}
    for index in range(count):
        x = points[(start + index) % len(points)]
        observation_id = f"obs-{start + index}"
        observations[observation_id] = Observation(
            observation_id=observation_id,
            input_id=f"inp-{start + index}",
            input_values={"x": x},
            output_values={"y": truth(x)},
            status=ObservationStatus.SUCCESS,
            sequence_number=index,
        )
    return observations


def make_request(
    hypothesis_id: str = "hyp-1",
    *,
    degree: int = 2,
    observations: Optional[Dict[str, Observation]] = None,
    observation_ids: Optional[List[str]] = None,
    request_id: str = "evreq-1",
) -> EvaluationRequest:
    observations = observations if observations is not None else make_observations()
    ids = observation_ids if observation_ids is not None else sorted(observations)
    return EvaluationRequest(
        hypothesis=make_candidate(degree=degree, hypothesis_id=hypothesis_id),
        observation_ids=list(ids),
        held_out_observation_ids=[],
        request_id=request_id,
        metric_names=["mae", "rmse"],
    )


def make_parallel_config(mode: str = "local_sequential", **overrides: Any) -> ParallelEvaluationConfig:
    defaults: Dict[str, Any] = dict(
        execution_mode=ParallelExecutionMode.from_str(mode),
        max_concurrency=2,
        max_retries=1,
        retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return ParallelEvaluationConfig(**defaults)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_DEFAULT_PROVIDER_ID = object()


def success_result(
    task: NodusTask,
    payload: Dict[str, Any],
    *,
    provider_task_id: Any = _DEFAULT_PROVIDER_ID,
    duration_seconds: float = 0.0,
    provider_metadata: Optional[Dict[str, Any]] = None,
) -> NodusResult:
    envelope = execute_evaluation_task_payload(payload)
    resolved_provider_task_id = (
        f"prov-{task.task_id}"
        if provider_task_id is _DEFAULT_PROVIDER_ID
        else provider_task_id
    )
    return NodusResult(
        task_id=task.task_id,
        execution_mode=ExecutionMode.REMOTE.value,
        status="succeeded",
        result_data=envelope,
        provider_task_id=resolved_provider_task_id,
        submitted_at=_now(),
        completed_at=_now(),
        duration_seconds=duration_seconds,
        provider_metadata=dict(provider_metadata or {}),
    )


def failure_result(
    task: NodusTask,
    code: str,
    *,
    provider_task_id: Optional[str] = None,
    status: str = "failed",
) -> NodusResult:
    return NodusResult(
        task_id=task.task_id,
        execution_mode=ExecutionMode.REMOTE.value,
        status=status,
        error={"code": code, "message": "sanitized adapter error", "context": {}},
        provider_task_id=provider_task_id,
        submitted_at=_now(),
        completed_at=_now(),
        duration_seconds=0.01,
    )


class FakeBatchAdapter:
    """Faithful in-process stand-in for ``NodusExecutionAdapter.execute_many``.

    It validates and JSON-round-trips every payload (so serialization errors are
    not hidden), records observed concurrency, and runs successful tasks through
    the real worker function. Only failures are scripted by tests.
    """

    def __init__(
        self,
        *,
        execution_mode: ExecutionMode = ExecutionMode.REMOTE,
        handler: Optional[Callable[[NodusTask, Dict[str, Any], int, int], Any]] = None,
        delay: float = 0.0,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.config = SimpleNamespace(
            execution_mode=execution_mode, timeout_seconds=timeout_seconds
        )
        self._handler = handler
        self._delay = delay
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.batches: List[List[str]] = []
        self.attempts: Dict[str, int] = {}
        self.round_trip_payloads: List[Dict[str, Any]] = []

    def execute_many(
        self, tasks: List[NodusTask], max_concurrency: int = 1
    ) -> List[NodusResult]:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self.batches.append([task.task_id for task in tasks])
        collected: List[Any] = []

        def run(index: int, task: NodusTask) -> Any:
            validate_json_payload(task.payload)
            payload = json.loads(json.dumps(task.payload))
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
                attempt = self.attempts.get(task.task_id, 0) + 1
                self.attempts[task.task_id] = attempt
                self.round_trip_payloads.append(payload)
            try:
                if self._delay:
                    time.sleep(self._delay)
                if self._handler is not None:
                    return self._handler(task, payload, index, attempt)
                return success_result(task, payload, duration_seconds=self._delay)
            finally:
                with self._lock:
                    self._active -= 1

        with ThreadPoolExecutor(max_workers=min(max_concurrency, len(tasks))) as pool:
            futures = {
                pool.submit(run, index, task): index for index, task in enumerate(tasks)
            }
            for future in as_completed(futures):
                value = future.result()
                if isinstance(value, list):
                    collected.extend(value)
                elif value is not None:
                    collected.append(value)
        return collected


class RaisingAdapter(FakeBatchAdapter):
    def execute_many(self, tasks: List[NodusTask], max_concurrency: int = 1):
        self.batches.append([task.task_id for task in tasks])
        raise RuntimeError("adapter transport exploded")


def local_runner(**config: Any) -> ParallelEvaluationRunner:
    adapter = FakeBatchAdapter(execution_mode=ExecutionMode.MOCK)
    return ParallelEvaluationRunner(
        adapter, make_parallel_config("local_sequential", **config)
    )


def remote_runner(adapter: Any = None, **config: Any) -> ParallelEvaluationRunner:
    adapter = adapter if adapter is not None else FakeBatchAdapter()
    return ParallelEvaluationRunner(adapter, make_parallel_config("nodus_parallel", **config))


# ===========================================================================
# A. Configuration
# ===========================================================================


class TestConfiguration:
    def test_from_environment_requires_mode(self, monkeypatch):
        monkeypatch.delenv("NODUS_PARALLEL_EXECUTION_MODE", raising=False)
        with pytest.raises(ParallelConfigurationError) as excinfo:
            ParallelEvaluationConfig.from_environment()
        assert "NODUS_PARALLEL_EXECUTION_MODE" in excinfo.value.message

    def test_from_environment_rejects_unknown_mode(self, monkeypatch):
        monkeypatch.setenv("NODUS_PARALLEL_EXECUTION_MODE", "carrier_pigeon")
        with pytest.raises(ParallelConfigurationError):
            ParallelEvaluationConfig.from_environment()

    def test_from_environment_reads_configured_values(self, monkeypatch):
        monkeypatch.setenv("NODUS_PARALLEL_EXECUTION_MODE", "nodus_parallel")
        monkeypatch.setenv("NODUS_MAX_CONCURRENCY", "6")
        monkeypatch.setenv("NODUS_PARALLEL_MAX_RETRIES", "2")
        monkeypatch.setenv("NODUS_PARALLEL_RETRY_BACKOFF_SECONDS", "0.25")
        monkeypatch.setenv("NODUS_PARALLEL_TASK_TIMEOUT_SECONDS", "12.5")
        config = ParallelEvaluationConfig.from_environment()
        assert config.execution_mode == ParallelExecutionMode.NODUS_PARALLEL
        assert config.max_concurrency == 6
        assert config.max_retries == 2
        assert config.retry_backoff_seconds == 0.25
        assert config.timeout_seconds == 12.5

    def test_from_environment_rejects_non_numeric_concurrency(self, monkeypatch):
        monkeypatch.setenv("NODUS_PARALLEL_EXECUTION_MODE", "local_sequential")
        monkeypatch.setenv("NODUS_MAX_CONCURRENCY", "many")
        with pytest.raises(ParallelConfigurationError):
            ParallelEvaluationConfig.from_environment()

    @pytest.mark.parametrize("value", [0, -1, 33, True, 1.5, "2"])
    def test_invalid_concurrency_is_rejected(self, value):
        with pytest.raises(ParallelConfigurationError):
            make_parallel_config(max_concurrency=value)

    @pytest.mark.parametrize("value", [-1, 6, True, 1.5])
    def test_invalid_retries_are_rejected(self, value):
        with pytest.raises(ParallelConfigurationError):
            make_parallel_config(max_retries=value)

    @pytest.mark.parametrize("value", [0.0, -1.0, float("inf"), "1"])
    def test_invalid_timeout_is_rejected(self, value):
        with pytest.raises(ParallelConfigurationError):
            make_parallel_config(timeout_seconds=value)

    def test_invalid_backoff_is_rejected(self):
        with pytest.raises(ParallelConfigurationError):
            make_parallel_config(retry_backoff_seconds=-0.5)

    def test_runner_rejects_invalid_config_type(self):
        with pytest.raises(ParallelConfigurationError):
            ParallelEvaluationRunner(FakeBatchAdapter(), config={"mode": "x"})


# ===========================================================================
# B. Task and result contracts
# ===========================================================================


class TestTaskContract:
    def test_payload_round_trips_and_matches_local_pipeline(self):
        observations = make_observations()
        request = make_request(observations=observations)
        payload = build_evaluation_task_payload(request, observations)
        assert payload["schema_version"] == EVALUATION_TASK_SCHEMA_VERSION
        assert payload["hypothesis_id"] == "hyp-1"
        assert payload["request_id"] == "evreq-1"
        # Strict JSON round trip, as it would cross the provider boundary.
        restored = json.loads(json.dumps(payload, allow_nan=False))
        validate_evaluation_task_payload(restored)

        envelope = execute_evaluation_task_payload(restored)
        assert envelope["status"] == TASK_RESULT_SUCCEEDED
        parsed = parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")

        direct = EvaluationPipeline().evaluate(request, observations)
        assert parsed.evaluation is not None
        assert parsed.evaluation.status == EvaluationStatus.COMPLETED
        assert parsed.evaluation.metrics == pytest.approx(direct.metrics)
        assert parsed.evaluation.sample_count == direct.sample_count
        assert dict(parsed.evaluation.fitted_specification or {}) == dict(
            direct.fitted_specification or {}
        )
        assert dict(parsed.evaluation.observation_counts or {}) == dict(
            direct.observation_counts or {}
        )

    def test_payload_rejects_ground_truth_keys(self):
        observations = make_observations()
        tainted = Observation(
            observation_id="obs-0",
            input_id="inp-0",
            input_values={"x": 1.0, "ground_truth_y": truth(1.0)},
            output_values={"y": truth(1.0)},
        )
        observations["obs-0"] = tainted
        with pytest.raises(TaskContractError) as excinfo:
            build_evaluation_task_payload(make_request(observations=observations), observations)
        assert excinfo.value.code == ERROR_INVALID_TASK_PAYLOAD

    def test_payload_rejects_non_serializable_candidate(self):
        observations = make_observations()
        candidate = make_candidate()
        parameters = dict(candidate.parameters)
        coefficients = dict(parameters["coefficients"])
        coefficients["c0"] = object()  # not JSON-serializable
        parameters["coefficients"] = coefficients
        candidate = HypothesisRepresentation(
            model_family=candidate.model_family,
            formula=candidate.formula,
            parameters=parameters,
            complexity=candidate.complexity,
            hypothesis_id="hyp-1",
        )
        request = EvaluationRequest(
            hypothesis=candidate, observation_ids=sorted(observations)
        )
        with pytest.raises(TaskContractError) as excinfo:
            build_evaluation_task_payload(request, observations)
        assert excinfo.value.code == ERROR_INVALID_TASK_PAYLOAD

    def test_validate_rejects_wrong_schema_version(self):
        observations = make_observations()
        payload = build_evaluation_task_payload(make_request(observations=observations), observations)
        payload["schema_version"] = "0.9"
        with pytest.raises(TaskContractError, match="schema version"):
            validate_evaluation_task_payload(payload)

    def test_validate_rejects_duplicate_observations(self):
        observations = make_observations()
        payload = build_evaluation_task_payload(make_request(observations=observations), observations)
        payload["observations"] = [payload["observations"][0], payload["observations"][0]]
        with pytest.raises(TaskContractError, match="duplicate"):
            validate_evaluation_task_payload(payload)

    def test_worker_returns_failed_envelope_for_invalid_payload(self):
        envelope = execute_evaluation_task_payload(
            {
                "schema_version": "invalid",
                "hypothesis_id": "hyp-1",
                "request_id": "evreq-1",
            }
        )
        assert envelope["status"] == TASK_RESULT_FAILED
        assert envelope["evaluation"] is None
        assert envelope["error"]["code"] == ERROR_INVALID_TASK_PAYLOAD

    def test_parse_rejects_hypothesis_mismatch(self):
        observations = make_observations()
        request = make_request(observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        with pytest.raises(TaskContractError) as excinfo:
            parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-other")
        assert excinfo.value.code == ERROR_INVALID_TASK_RESULT

    def test_parse_rejects_non_finite_metrics(self):
        observations = make_observations()
        request = make_request(observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        envelope["evaluation"]["metrics"]["mae"] = float("nan")
        with pytest.raises(TaskContractError, match="finite"):
            parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")

    def test_parse_rejects_failed_evaluation_without_error(self):
        observations = make_observations()
        request = make_request(observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        envelope["evaluation"]["status"] = "failed"
        envelope["evaluation"].pop("error", None)
        with pytest.raises(TaskContractError, match="failed evaluation"):
            parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")

    def test_parse_rejects_malformed_optional_fields(self):
        observations = make_observations()
        request = make_request(observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        envelope["evaluation"]["model_family"] = [POLYNOMIAL_FAMILY_ID]
        with pytest.raises(TaskContractError, match="model_family"):
            parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")

    def test_parse_rejects_negative_observation_counts(self):
        observations = make_observations()
        request = make_request(observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        envelope["evaluation"]["observation_counts"] = {"accepted_training": -1}
        with pytest.raises(TaskContractError, match="non-negative integer"):
            parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")

    def test_evaluation_result_round_trip_preserves_fields(self):
        observations = make_observations()
        request = make_request(observations=observations)
        direct = EvaluationPipeline().evaluate(request, observations)
        restored = evaluation_result_from_dict(direct.to_dict())
        assert restored.hypothesis_id == direct.hypothesis_id
        assert restored.status == direct.status
        assert restored.metrics == pytest.approx(direct.metrics)
        assert restored.fitted_specification == direct.fitted_specification
        assert restored.evaluation_id == direct.evaluation_id

    def test_scientific_failure_is_a_valid_task_result(self):
        observations = make_observations(count=2)  # too few for degree 3
        request = make_request(degree=3, observations=observations)
        envelope = execute_evaluation_task_payload(
            build_evaluation_task_payload(request, observations)
        )
        assert envelope["status"] == TASK_RESULT_SUCCEEDED
        parsed = parse_task_result_envelope(envelope, expected_hypothesis_id="hyp-1")
        assert parsed.evaluation is not None
        assert parsed.evaluation.status == EvaluationStatus.FAILED
        assert parsed.evaluation.metrics == {}
        assert parsed.evaluation.error is not None


# ===========================================================================
# C. Local sequential mode
# ===========================================================================


class TestLocalSequential:
    def test_runs_every_request_in_input_order(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", degree=index % 3, observations=observations, request_id=f"evreq-{index}")
            for index in range(4)
        ]
        batch = local_runner(max_concurrency=4).run(requests, observations, dataset_id="ds-1")
        assert [result.hypothesis_id for result in batch.results] == [
            "hyp-0",
            "hyp-1",
            "hyp-2",
            "hyp-3",
        ]
        assert all(result.outcome == TaskOutcome.SUCCEEDED for result in batch.results)
        assert all(result.execution_mode == MODE_LOCAL_SEQUENTIAL for result in batch.results)
        assert batch.summary.requested_mode == MODE_LOCAL_SEQUENTIAL
        assert batch.summary.actual_mode == MODE_LOCAL_SEQUENTIAL
        assert batch.summary.completed_tasks == 4
        assert batch.summary.failed_tasks == 0

    def test_local_mode_never_calls_the_adapter(self):
        class ExplodingAdapter(FakeBatchAdapter):
            def execute_many(self, tasks, max_concurrency=1):
                raise AssertionError("local mode must not dispatch through the adapter")

        runner = ParallelEvaluationRunner(
            ExplodingAdapter(), make_parallel_config("local_sequential")
        )
        observations = make_observations()
        batch = runner.run([make_request(observations=observations)], observations)
        assert batch.summary.completed_tasks == 1

    def test_empty_batch(self):
        batch = local_runner().run([], {})
        assert batch.results == ()
        assert batch.summary.requested_tasks == 0
        assert batch.summary.completed_tasks == 0

    def test_cancellation_prevents_execution_and_produces_no_score(self):
        observations = make_observations()
        requests = [make_request(f"hyp-{i}", observations=observations) for i in range(3)]
        cancel_event = threading.Event()
        cancel_event.set()
        batch = local_runner().run(requests, observations, cancel_event=cancel_event)
        assert all(result.outcome == TaskOutcome.CANCELLED for result in batch.results)
        assert all(result.evaluation is None for result in batch.results)
        assert batch.summary.cancelled_tasks == 3
        assert batch.summary.submitted_tasks == 0

    def test_local_cancellation_between_tasks_preserves_completed_results(self):
        observations = make_observations()
        requests = [make_request(f"hyp-{i}", observations=observations) for i in range(3)]

        class FlipAfterFirstCheck:
            def __init__(self) -> None:
                self.checks = 0

            def is_set(self) -> bool:
                self.checks += 1
                return self.checks > 1  # first task runs, later ones cancel

        batch = local_runner().run(
            requests, observations, cancel_event=FlipAfterFirstCheck()
        )
        assert batch.results[0].outcome == TaskOutcome.SUCCEEDED
        assert batch.results[1].outcome == TaskOutcome.CANCELLED
        assert batch.results[2].outcome == TaskOutcome.CANCELLED


# ===========================================================================
# D. Remote orchestration
# ===========================================================================


class TestRemoteOrchestration:
    def test_requires_remote_adapter_and_never_falls_back(self):
        adapter = FakeBatchAdapter(execution_mode=ExecutionMode.MOCK)
        runner = remote_runner(adapter)
        observations = make_observations()
        with pytest.raises(ParallelConfigurationError) as excinfo:
            runner.run([make_request(observations=observations)], observations)
        assert "NODUS_EXECUTION_MODE=nodus_remote" in excinfo.value.message
        assert adapter.batches == []

    def test_requires_batch_capable_adapter(self):
        adapter = FakeBatchAdapter()
        adapter.execute_many = None  # type: ignore[assignment]
        runner = remote_runner(adapter)
        observations = make_observations()
        with pytest.raises(ParallelConfigurationError):
            runner.run([make_request(observations=observations)], observations)

    def test_results_correlate_with_tasks_and_carry_provider_ids(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations, request_id=f"evreq-{index}")
            for index in range(3)
        ]
        adapter = FakeBatchAdapter()
        batch = remote_runner(adapter).run(requests, observations)
        assert [result.hypothesis_id for result in batch.results] == ["hyp-0", "hyp-1", "hyp-2"]
        assert [result.request_id for result in batch.results] == [
            "evreq-0",
            "evreq-1",
            "evreq-2",
        ]
        assert all(result.provider_task_id for result in batch.results)
        assert all(
            result.execution_mode == ExecutionMode.REMOTE.value for result in batch.results
        )
        assert batch.summary.actual_mode == ExecutionMode.REMOTE.value
        assert batch.summary.provider_metadata_available is True
        # Payloads reached the worker as JSON, not as live objects.
        assert all(isinstance(payload, dict) for payload in adapter.round_trip_payloads)

    def test_aggregation_order_is_independent_of_completion_order(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations, request_id=f"evreq-{index}")
            for index in range(4)
        ]

        def reversed_delays(task, payload, index, attempt):
            time.sleep(0.05 * (4 - index))
            return success_result(task, payload, duration_seconds=0.05 * (4 - index))

        adapter = FakeBatchAdapter(handler=reversed_delays)
        batch = remote_runner(adapter, max_concurrency=4).run(requests, observations)
        assert [result.hypothesis_id for result in batch.results] == [
            "hyp-0",
            "hyp-1",
            "hyp-2",
            "hyp-3",
        ]
        assert [result.request_id for result in batch.results] == [
            "evreq-0",
            "evreq-1",
            "evreq-2",
            "evreq-3",
        ]

    def test_repeated_runs_are_deterministic(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(3)
        ]
        first = remote_runner(FakeBatchAdapter(delay=0.01)).run(requests, observations)
        second = remote_runner(FakeBatchAdapter(delay=0.02)).run(requests, observations)
        assert [result.hypothesis_id for result in first.results] == [
            result.hypothesis_id for result in second.results
        ]
        assert [
            dict(result.evaluation.metrics) if result.evaluation else None
            for result in first.results
        ] == [
            dict(result.evaluation.metrics) if result.evaluation else None
            for result in second.results
        ]
        assert [result.task_id for result in first.results] == [
            result.task_id for result in second.results
        ]

    def test_concurrency_limit_is_enforced(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(6)
        ]
        adapter = FakeBatchAdapter(delay=0.03)
        batch = remote_runner(adapter, max_concurrency=2).run(requests, observations)
        assert len(batch.results) == 6
        assert adapter.max_active <= 2
        assert adapter.max_active >= 2  # the fake actually overlapped work
        assert batch.summary.max_concurrency == 2

    def test_concurrency_of_one_is_sequential(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(4)
        ]
        adapter = FakeBatchAdapter(delay=0.01)
        remote_runner(adapter, max_concurrency=1).run(requests, observations)
        assert adapter.max_active == 1

    def test_large_collection_respects_limit(self):
        observations = make_observations()
        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(40)
        ]
        adapter = FakeBatchAdapter(delay=0.001)
        batch = remote_runner(adapter, max_concurrency=4, max_retries=0).run(
            requests, observations
        )
        assert len(batch.results) == 40
        assert adapter.max_active <= 4
        assert batch.summary.completed_tasks == 40

    def test_metrics_reflect_real_outcomes(self):
        observations = make_observations()

        def scripted(task, payload, index, attempt):
            if index == 1:
                return failure_result(task, "NODUS_REMOTE_EXECUTION_FAILED", provider_task_id="prov-1")
            if index == 2:
                return failure_result(task, "NODUS_TIMEOUT", provider_task_id="prov-2", status="timed_out")
            if index == 3:
                return failure_result(task, "NODUS_CANCELLED", status="cancelled")
            return success_result(task, payload)

        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(5)
        ]
        batch = remote_runner(
            FakeBatchAdapter(handler=scripted), max_concurrency=2, max_retries=0
        ).run(requests, observations)
        summary = batch.summary
        assert summary.requested_tasks == 5
        assert summary.submitted_tasks == 5
        assert summary.completed_tasks == 2
        assert summary.failed_tasks == 1
        assert summary.timed_out_tasks == 1
        assert summary.cancelled_tasks == 1
        assert summary.retry_attempts == 0
        assert summary.cost_status == "unavailable"
        assert summary.estimated_cost is None
        assert summary.settled_cost is None
        assert summary.duration_seconds >= 0.0

    def test_unavailable_provider_metadata_is_not_fabricated(self):
        observations = make_observations()

        def without_provider_metadata(task, payload, index, attempt):
            return success_result(
                task, payload, provider_task_id=None, provider_metadata={}
            )

        batch = remote_runner(FakeBatchAdapter(handler=without_provider_metadata)).run(
            [make_request(observations=observations)], observations
        )
        result = batch.results[0]
        assert result.provider_task_id is None
        assert dict(result.provider_metadata) == {}
        assert batch.summary.provider_metadata_available is False


# ===========================================================================
# E. Failures, timeouts, retries, cancellation
# ===========================================================================


class TestFailureHandling:
    def test_one_failure_does_not_discard_other_successes(self):
        observations = make_observations()

        def scripted(task, payload, index, attempt):
            if index == 1:
                return failure_result(task, "NODUS_REMOTE_EXECUTION_FAILED")
            return success_result(task, payload)

        requests = [
            make_request(f"hyp-{index}", observations=observations) for index in range(3)
        ]
        batch = remote_runner(FakeBatchAdapter(handler=scripted)).run(requests, observations)
        assert [result.outcome for result in batch.results] == [
            TaskOutcome.SUCCEEDED,
            TaskOutcome.FAILED,
            TaskOutcome.SUCCEEDED,
        ]
        assert batch.results[1].evaluation is None
        assert batch.results[1].error is not None
        assert batch.results[0].evaluation is not None
        assert batch.results[2].evaluation is not None

    def test_timeout_is_explicit_and_has_no_score(self):
        observations = make_observations()

        def timed_out(task, payload, index, attempt):
            return failure_result(task, "NODUS_TIMEOUT", provider_task_id="prov-x", status="timed_out")

        batch = remote_runner(FakeBatchAdapter(handler=timed_out)).run(
            [make_request(observations=observations)], observations
        )
        result = batch.results[0]
        assert result.outcome == TaskOutcome.TIMED_OUT
        assert result.evaluation is None
        assert result.error is not None
        assert result.error.code == "NODUS_TIMEOUT"
        assert batch.summary.timed_out_tasks == 1
        assert batch.summary.completed_tasks == 0

    def test_malformed_result_is_rejected(self):
        observations = make_observations()

        def malformed(task, payload, index, attempt):
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="succeeded",
                result_data={"schema_version": "0.0"},
                provider_task_id="prov-malformed",
            )

        batch = remote_runner(FakeBatchAdapter(handler=malformed)).run(
            [make_request(observations=observations)], observations
        )
        result = batch.results[0]
        assert result.outcome == TaskOutcome.FAILED
        assert result.evaluation is None
        assert result.error is not None
        assert result.error.code == ERROR_INVALID_TASK_RESULT

    def test_mismatched_hypothesis_result_is_rejected(self):
        observations = make_observations()

        def wrong_id(task, payload, index, attempt):
            result = success_result(task, payload)
            result.result_data["hypothesis_id"] = "hyp-impostor"
            return result

        batch = remote_runner(FakeBatchAdapter(handler=wrong_id)).run(
            [make_request(observations=observations)], observations
        )
        assert batch.results[0].outcome == TaskOutcome.FAILED
        assert batch.results[0].evaluation is None

    def test_missing_result_is_explicit(self):
        observations = make_observations()
        adapter = FakeBatchAdapter(handler=lambda *args: None)
        batch = remote_runner(adapter, max_retries=0).run(
            [make_request(observations=observations)], observations
        )
        result = batch.results[0]
        assert result.outcome == TaskOutcome.FAILED
        assert result.evaluation is None
        assert result.error is not None
        assert result.error.code == ERROR_MISSING_TASK_RESULT
        assert len(adapter.batches) == 1  # ambiguous results are not resubmitted

    def test_duplicate_result_is_recorded_once(self):
        observations = make_observations()

        def duplicate(task, payload, index, attempt):
            result = success_result(task, payload)
            return [result, result]

        batch = remote_runner(FakeBatchAdapter(handler=duplicate)).run(
            [make_request(observations=observations)], observations
        )
        assert len(batch.results) == 1
        assert batch.results[0].outcome == TaskOutcome.SUCCEEDED
        assert batch.results[0].task_id in batch.duplicate_task_ids

    def test_unexpected_result_is_recorded_and_excluded(self):
        observations = make_observations()

        def with_unexpected(task, payload, index, attempt):
            extra = NodusResult(
                task_id="unexpected-task",
                execution_mode=ExecutionMode.REMOTE.value,
                status="succeeded",
                result_data=success_result(task, payload).result_data,
            )
            return [success_result(task, payload), extra]

        batch = remote_runner(FakeBatchAdapter(handler=with_unexpected)).run(
            [make_request(observations=observations)], observations
        )
        assert len(batch.results) == 1
        assert "unexpected-task" in batch.unexpected_task_ids

    def test_retryable_failure_is_retried_once(self):
        observations = make_observations()

        def flaky(task, payload, index, attempt):
            if attempt == 1:
                return failure_result(task, "NODUS_RATE_LIMITED", provider_task_id=None)
            return success_result(task, payload)

        adapter = FakeBatchAdapter(handler=flaky)
        batch = remote_runner(adapter, max_retries=1).run(
            [make_request(observations=observations)], observations
        )
        result = batch.results[0]
        assert result.outcome == TaskOutcome.SUCCEEDED
        assert result.attempts == 2
        assert batch.summary.retry_attempts == 1
        assert len(adapter.batches) == 2

    def test_non_retryable_failure_is_not_retried(self):
        observations = make_observations()

        def unauthorized(task, payload, index, attempt):
            return failure_result(task, "NODUS_PERMISSION_DENIED")

        adapter = FakeBatchAdapter(handler=unauthorized)
        batch = remote_runner(adapter, max_retries=2).run(
            [make_request(observations=observations)], observations
        )
        assert batch.results[0].outcome == TaskOutcome.FAILED
        assert batch.results[0].attempts == 1
        assert len(adapter.batches) == 1

    def test_post_submission_failure_is_never_resubmitted(self):
        observations = make_observations()

        def accepted_then_failed(task, payload, index, attempt):
            return failure_result(task, "NODUS_RATE_LIMITED", provider_task_id="prov-accepted")

        adapter = FakeBatchAdapter(handler=accepted_then_failed)
        batch = remote_runner(adapter, max_retries=3).run(
            [make_request(observations=observations)], observations
        )
        assert batch.results[0].attempts == 1
        assert len(adapter.batches) == 1
        assert batch.summary.retry_attempts == 0

    def test_retries_are_bounded(self):
        observations = make_observations()

        def always_retryable(task, payload, index, attempt):
            return failure_result(task, "NODUS_SDK_ERROR", provider_task_id=None)

        adapter = FakeBatchAdapter(handler=always_retryable)
        batch = remote_runner(adapter, max_retries=2).run(
            [make_request(observations=observations)], observations
        )
        assert batch.results[0].outcome == TaskOutcome.FAILED
        assert batch.results[0].attempts == 3
        assert len(adapter.batches) == 3
        assert batch.summary.retry_attempts == 2

    def test_adapter_exception_marks_all_pending_failed_without_retry(self):
        observations = make_observations()
        adapter = RaisingAdapter()
        batch = remote_runner(adapter).run(
            [
                make_request(f"hyp-{index}", observations=observations)
                for index in range(2)
            ],
            observations,
        )
        assert all(result.outcome == TaskOutcome.FAILED for result in batch.results)
        assert all(result.evaluation is None for result in batch.results)
        assert all(result.error.code == ERROR_DISPATCH_FAILED for result in batch.results)
        assert len(adapter.batches) == 1

    def test_cancel_before_dispatch_produces_no_submission(self):
        observations = make_observations()
        adapter = FakeBatchAdapter()
        cancel_event = threading.Event()
        cancel_event.set()
        batch = remote_runner(adapter).run(
            [make_request(observations=observations)],
            observations,
            cancel_event=cancel_event,
        )
        assert batch.results[0].outcome == TaskOutcome.CANCELLED
        assert batch.results[0].evaluation is None
        assert batch.results[0].error.code == ERROR_CANCELLED
        assert adapter.batches == []

    def test_failed_tasks_never_gain_metrics(self):
        observations = make_observations()

        def failed(task, payload, index, attempt):
            return failure_result(task, "NODUS_REMOTE_EXECUTION_FAILED")

        result = remote_runner(FakeBatchAdapter(handler=failed)).run(
            [make_request(observations=observations)], observations
        ).results[0]
        assert result.outcome == TaskOutcome.FAILED
        assert result.evaluation is None
        assert result.to_dict()["evaluation"] is None
        assert result.to_dict()["error"] is not None

    def test_secrets_are_not_logged_or_serialized(self, caplog):
        config = NodusConfig(
            execution_mode=ExecutionMode.MOCK,
            app_name="test-app",
            function_name="test-fn",
            api_key=SECRET_CANARY,
            timeout_seconds=5.0,
            max_retries=0,
        )
        assert SECRET_CANARY not in repr(config)
        adapter = NodusExecutionAdapter(config)
        with caplog.at_level(logging.DEBUG):
            results = adapter.execute_many(
                [NodusTask(task_id="log-check", operation="op", payload={"i": 1})],
                max_concurrency=1,
            )
        assert results[0].status == "succeeded"
        assert SECRET_CANARY not in caplog.text


# ===========================================================================
# F/G. Discovery integration and scientific isolation
# ===========================================================================


class CountingOracle:
    """Local deterministic oracle that records submitted inputs."""

    def __init__(self) -> None:
        from app.adapters.oracle.local_simulator import PolynomialLocalOracle

        self._inner = PolynomialLocalOracle()
        self.submitted: List[Dict[str, Any]] = []

    def validate_input(self, experiment_input):
        return self._inner.validate_input(experiment_input)

    def query(self, experiment_input):
        self.submitted.append(dict(experiment_input.values))
        return self._inner.query(experiment_input)


def make_discovery_config(**overrides: Any) -> DiscoveryRunConfig:
    defaults: Dict[str, Any] = dict(
        max_query_budget=3,
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
        ),
        experiment_pool_size=5,
    )
    defaults.update(overrides)
    return DiscoveryRunConfig(**defaults)


def run_discovery(evaluation_pipeline) -> Any:
    oracle = CountingOracle()
    engine = DiscoveryEngine(
        oracle=oracle,
        config=make_discovery_config(),
        evaluation_pipeline=evaluation_pipeline,
        run_id="run-fixed",
    )
    observations = make_observations(count=3)
    result = engine.run(initial_observations=list(observations.values()))
    return result, oracle, observations


def ranking_signature(result: Any) -> List[Any]:
    formulas = {hypothesis.hypothesis_id: hypothesis.formula for hypothesis in result.hypotheses}
    return [
        (
            formulas.get(candidate.hypothesis_id),
            candidate.training_score,
            candidate.complexity,
        )
        for candidate in result.ranked_hypotheses
    ]


class TestDiscoveryIntegration:
    def test_parallel_pipeline_matches_local_pipeline(self):
        local_result, local_oracle, _ = run_discovery(EvaluationPipeline())
        parallel_result, parallel_oracle, _ = run_discovery(
            ParallelEvaluationPipeline(remote_runner(FakeBatchAdapter()))
        )
        assert parallel_result.termination_reason == local_result.termination_reason
        # Compare scientific content, not hypothesis IDs: IDs are freshly
        # generated per run and only tie-break equal scores, so an equal-content
        # comparison avoids a false failure from unrelated ID ordering.
        assert sorted(ranking_signature(parallel_result)) == sorted(
            ranking_signature(local_result)
        )
        assert len(parallel_result.ranked_hypotheses) == len(local_result.ranked_hypotheses)
        assert len(parallel_oracle.submitted) == len(local_oracle.submitted)

    def test_parallel_evaluation_does_not_duplicate_oracle_queries(self):
        result, oracle, _ = run_discovery(
            ParallelEvaluationPipeline(remote_runner(FakeBatchAdapter()))
        )
        inputs = [json.dumps(values, sort_keys=True) for values in oracle.submitted]
        assert len(inputs) == len(set(inputs))
        assert result.query_accounting.attempted_queries <= 3

    def test_failed_task_becomes_failed_evaluation_not_a_score(self):
        observations = make_observations()

        def failed(task, payload, index, attempt):
            return failure_result(task, "NODUS_REMOTE_EXECUTION_FAILED")

        pipeline = ParallelEvaluationPipeline(remote_runner(FakeBatchAdapter(handler=failed)))
        request = make_request(observations=observations)
        results = pipeline.evaluate_many([request], observations)
        assert len(results) == 1
        assert results[0].status == EvaluationStatus.FAILED
        assert results[0].metrics == {}
        assert results[0].fitted_specification is None
        assert results[0].error is not None

    def test_discovery_payloads_contain_no_evaluator_only_keys(self):
        observations = make_observations()
        payload = build_evaluation_task_payload(
            make_request(observations=observations), observations
        )
        serialized = json.dumps(payload)
        for forbidden in ("ground_truth", "hidden_rule", "evaluator_labels", "oracle_internal"):
            assert forbidden not in serialized

    def test_batch_serialization_is_strict_json(self):
        observations = make_observations()
        batch = remote_runner().run(
            [make_request(observations=observations)], observations
        )
        json.dumps(batch.to_dict(), allow_nan=False)


# ===========================================================================
# H. Adapter batch path
# ===========================================================================


def _install_fake_nodus(monkeypatch, *, timeout: bool = False) -> Dict[str, Any]:
    """Install a thread-safe fake ``nodus`` module for batch-path tests."""
    state: Dict[str, Any] = {
        "active": 0,
        "max_active": 0,
        "lock": threading.Lock(),
        "client_creations": 0,
        "default_client_sets": 0,
        "cancelled": 0,
    }

    class NodusError(Exception):
        retryable = False

    class TooManyRequests(NodusError):
        retryable = True

    class Unavailable(NodusError):
        retryable = True

    class APITimeoutError(NodusError):
        pass

    class FunctionCallFailed(NodusError):
        pass

    errors_mod = types.ModuleType("nodus.errors")
    for cls in (NodusError, TooManyRequests, Unavailable, APITimeoutError, FunctionCallFailed):
        setattr(errors_mod, cls.__name__, cls)

    class _Handle:
        def __init__(self, payload: Dict[str, Any]) -> None:
            self._payload = payload
            self.name = f"prov-{payload.get('echo_value', '?')}"
            self._done = False

        async def get(self, timeout=None):
            try:
                await asyncio.sleep(0.03)
                if timeout:
                    return {"echo_value": self._payload.get("echo_value")}
                return {"echo_value": self._payload.get("echo_value")}
            finally:
                if not self._done:
                    self._done = True
                    with state["lock"]:
                        state["active"] -= 1

        async def cancel(self):
            with state["lock"]:
                state["cancelled"] += 1

    class _Function:
        @classmethod
        def from_name(cls, app, name, project=None):
            return cls()

        async def spawn(self, **kwargs):
            with state["lock"]:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            await asyncio.sleep(0.02)
            return _Handle(kwargs.get("payload") or {})

    class _Client:
        def __init__(self, **kwargs):
            with state["lock"]:
                state["client_creations"] += 1

        def close(self):
            return None

    api_mod = types.ModuleType("nodus.api")
    api_mod.default_client = lambda: None

    def _set_default_client(client):
        with state["lock"]:
            state["default_client_sets"] += 1

    api_mod.set_default_client = _set_default_client

    nodus_mod = types.ModuleType("nodus")
    nodus_mod.Function = _Function
    nodus_mod.Client = _Client
    nodus_mod.api = api_mod
    nodus_mod.errors = errors_mod

    if timeout:
        class _TimeoutHandle(_Handle):
            async def get(self, timeout=None):
                try:
                    raise TimeoutError("deadline exceeded")
                finally:
                    if not self._done:
                        self._done = True
                        with state["lock"]:
                            state["active"] -= 1

        class _TimeoutFunction(_Function):
            async def spawn(self, **kwargs):
                with state["lock"]:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                return _TimeoutHandle(kwargs.get("payload") or {})

        nodus_mod.Function = _TimeoutFunction

    monkeypatch.setitem(sys.modules, "nodus", nodus_mod)
    monkeypatch.setitem(sys.modules, "nodus.api", api_mod)
    monkeypatch.setitem(sys.modules, "nodus.errors", errors_mod)
    return state


def _adapter_config(mode: str = "nodus_remote") -> NodusConfig:
    return NodusConfig(
        execution_mode=ExecutionMode.from_str(mode),
        app_name="test-app",
        function_name="test-fn",
        api_key="test-key" if mode == "nodus_remote" else None,
        timeout_seconds=5.0,
        max_retries=0,
    )


class TestAdapterBatch:
    def test_mock_mode_batch_is_sequential_and_ordered(self):
        adapter = NodusExecutionAdapter(_adapter_config("mock"))
        tasks = [
            NodusTask(task_id=f"t-{index}", operation="op", payload={"i": index})
            for index in range(3)
        ]
        results = adapter.execute_many(tasks, max_concurrency=3)
        assert [result.task_id for result in results] == ["t-0", "t-1", "t-2"]
        assert all(result.execution_mode == ExecutionMode.MOCK.value for result in results)
        assert adapter.config.execution_mode == ExecutionMode.MOCK

    @pytest.mark.parametrize("value", [0, -1, 33, True, 1.5])
    def test_invalid_concurrency_is_rejected(self, value):
        adapter = NodusExecutionAdapter(_adapter_config("mock"))
        with pytest.raises(NodusConfigurationError):
            adapter.execute_many(
                [NodusTask(task_id="t", operation="op", payload={"i": 1})],
                max_concurrency=value,
            )

    def test_non_task_argument_is_rejected(self):
        adapter = NodusExecutionAdapter(_adapter_config("mock"))
        with pytest.raises(NodusConfigurationError):
            adapter.execute_many([{"task_id": "t"}], max_concurrency=1)

    def test_remote_batch_uses_one_client_scope_and_runs_concurrently(self, monkeypatch):
        state = _install_fake_nodus(monkeypatch)
        adapter = NodusExecutionAdapter(_adapter_config("nodus_remote"))
        tasks = [
            NodusTask(
                task_id=f"t-{index}",
                operation="smoke_test",
                payload={"echo_value": f"v-{index}"},
            )
            for index in range(4)
        ]
        results = adapter.execute_many(tasks, max_concurrency=3)
        assert [result.task_id for result in results] == [task.task_id for task in tasks]
        assert all(result.status == "succeeded" for result in results)
        assert [result.result_data["echo_value"] for result in results] == [
            "v-0",
            "v-1",
            "v-2",
            "v-3",
        ]
        # One client for the whole batch, not one per task. The second
        # set_default_client call is the teardown restore of the previous client.
        assert state["client_creations"] == 1
        assert state["default_client_sets"] == 2
        assert state["max_active"] > 1

    def test_remote_batch_concurrency_of_one(self, monkeypatch):
        state = _install_fake_nodus(monkeypatch)
        adapter = NodusExecutionAdapter(_adapter_config("nodus_remote"))
        tasks = [
            NodusTask(task_id=f"t-{index}", operation="smoke_test", payload={"echo_value": f"v-{index}"})
            for index in range(3)
        ]
        adapter.execute_many(tasks, max_concurrency=1)
        assert state["max_active"] == 1

    def test_remote_timeout_requests_cancellation_without_claiming_it(self, monkeypatch):
        state = _install_fake_nodus(monkeypatch, timeout=True)
        adapter = NodusExecutionAdapter(_adapter_config("nodus_remote"))
        result = adapter.execute_many(
            [NodusTask(task_id="t-0", operation="smoke_test", payload={"echo_value": "v"})],
            max_concurrency=1,
        )[0]
        assert result.status == "timed_out"
        assert result.error["context"]["cancellation_requested"] is True
        assert result.error["context"]["cancellation_confirmed"] is False
        assert state["cancelled"] == 1
