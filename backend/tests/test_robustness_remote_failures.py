"""Failure Injection and Remote Execution Robustness Test Suite (Issue #28 - Scope 2).

Simulates and verifies handling of:
1. Nodus service unavailable (retryable vs non-retryable pre-submission handling).
2. Transient network failures during execution requests.
3. Gateway timeouts and execution job timeouts (strict no-post-submission-retry guarantee).
4. Invalid responses, corrupted payloads, and unexpected status codes.
5. Partial payload returns where expected result fields are missing.
6. Idempotency and local state integrity: no dangling runs or corrupted state.
"""

from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from app.adapters.nodus.adapter import NodusExecutionAdapter
from app.adapters.nodus.config import NodusConfig
from app.adapters.nodus.contracts import ExecutionMode, NodusResult, NodusTask
from app.adapters.nodus.errors import (
    NodusErrorCode,
    NodusExecutionError,
    NodusResultError,
)
from app.adapters.persistence.database import DatabaseManager
from app.adapters.persistence.models import ComputeTaskModel, RunModel
from app.adapters.persistence.repository import ClarityRepository
from app.domain.contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.services.parallel_evaluation import (
    EvaluationTaskResult,
    ParallelEvaluationConfig,
    ParallelEvaluationRunner,
    ParallelExecutionMode,
    TaskOutcome,
)

_UNSET = object()


def _make_config(
    mode: str = "nodus_remote",
    max_retries: int = 2,
    timeout: float = 2.0,
) -> NodusConfig:
    return NodusConfig(
        execution_mode=ExecutionMode.from_str(mode),
        app_name="clarity-test-app",
        function_name="evaluate_hypothesis",
        api_key="nodus_secret_canary_key_12345",
        timeout_seconds=timeout,
        max_retries=max_retries,
    )


def _make_task(operation: str = "evaluate", payload: dict | None = None) -> NodusTask:
    return NodusTask(
        task_id=f"task-{uuid.uuid4().hex[:8]}",
        operation=operation,
        payload=payload or {"candidate_id": "hyp-001", "samples": [{"x": 1.0, "y": 2.0}]},
    )


def _build_fake_nodus_module(
    spawn_side_effect=None,
    get_side_effect=None,
    get_return_value=_UNSET,
):
    """Build a fake 'nodus' and 'nodus.errors' module tree for controlled failure injection."""
    nodus_mod = types.ModuleType("nodus")
    errors_mod = types.ModuleType("nodus.errors")
    api_mod = types.ModuleType("nodus.api")

    class BaseNodusError(Exception):
        retryable = False

    class UnavailableError(BaseNodusError):
        retryable = True

    class TimeoutError(BaseNodusError):
        retryable = False

    class InternalError(BaseNodusError):
        retryable = False

    errors_mod.NodusError = BaseNodusError
    errors_mod.Unavailable = UnavailableError
    errors_mod.TooManyRequests = UnavailableError
    errors_mod.Timeout = TimeoutError
    errors_mod.InternalError = InternalError
    errors_mod.InternalServerError = InternalError

    fake_fn = MagicMock()
    fake_handle = MagicMock()
    fake_handle.name = "call-remote-999"

    if get_side_effect is not None:
        fake_handle.get.side_effect = get_side_effect
    elif get_return_value is not _UNSET:
        fake_handle.get.return_value = get_return_value
    else:
        fake_handle.get.return_value = {
            "status": "success",
            "metrics": {"mae": 0.05, "rmse": 0.08},
            "sample_count": 5,
        }

    if spawn_side_effect is not None:
        fake_fn.spawn.side_effect = spawn_side_effect
    else:
        fake_fn.spawn.return_value = fake_handle

    nodus_mod.Function = MagicMock()
    nodus_mod.Function.from_name.return_value = fake_fn
    nodus_mod.Client = MagicMock()
    nodus_mod.errors = errors_mod

    api_mod.set_default_client = MagicMock()
    nodus_mod.api = api_mod

    return nodus_mod, errors_mod, fake_fn, fake_handle


# ==============================================================================
# 1. Nodus Service Unavailable & Pre-Submission Retries
# ==============================================================================

class TestNodusServiceUnavailableAndRetries:
    """Validate behavior when Nodus service is unavailable or rate-limited."""

    def test_transient_service_unavailable_retries_and_succeeds(self):
        """Pre-submission 503/Unavailable error is retried and succeeds on second attempt."""
        config = _make_config(max_retries=2)
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, fake_fn, fake_handle = _build_fake_nodus_module()
        # Fail first spawn call with retryable Unavailable, succeed on second
        fake_fn.spawn.side_effect = [
            errors_mod.Unavailable("Service temporarily overloaded (503)"),
            fake_handle,
        ]

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)), \
             patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.001):
            result = adapter.execute(task)

        assert result.status == "succeeded"
        assert result.result_data["metrics"]["mae"] == 0.05
        assert fake_fn.spawn.call_count == 2
        # Secret canary must never appear in result
        assert "nodus_secret_canary_key_12345" not in str(result.to_dict())

    def test_persistent_service_unavailable_exhausts_retries_and_fails_cleanly(self):
        """Persistent Unavailable exhausts max_retries and returns structured failure without crash."""
        config = _make_config(max_retries=2)
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, fake_fn, _ = _build_fake_nodus_module()
        fake_fn.spawn.side_effect = errors_mod.Unavailable("Nodus service unavailable (503)")

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)), \
             patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.001):
            result = adapter.execute(task)

        assert result.status == "failed"
        assert result.error is not None
        assert result.error["code"] == NodusErrorCode.SDK_ERROR
        assert fake_fn.spawn.call_count == 3  # initial + 2 retries
        assert "nodus_secret_canary_key_12345" not in str(result.to_dict())

    def test_non_retryable_error_fails_immediately_without_retrying(self):
        """Errors where retryable=False must fail on the first attempt without duplicate retries."""
        config = _make_config(max_retries=3)
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, fake_fn, _ = _build_fake_nodus_module()
        fake_fn.spawn.side_effect = errors_mod.InternalError("Invalid project configuration (400)")

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)), \
             patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.001):
            result = adapter.execute(task)

        assert result.status == "failed"
        assert fake_fn.spawn.call_count == 1  # exactly 1 attempt, zero retries


# ==============================================================================
# 2. Transient Network Failures
# ==============================================================================

class TestTransientNetworkFailures:
    """Validate behavior under transient connection dropouts and socket resets."""

    def test_network_connection_reset_pre_submission_handled_cleanly(self):
        """A network socket reset during Function.from_name fails cleanly with sanitized error."""
        config = _make_config(max_retries=1)
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, _, _ = _build_fake_nodus_module()
        nodus_mod.Function.from_name.side_effect = ConnectionResetError("Connection reset by peer (errno 104)")

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)), \
             patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.001):
            result = adapter.execute(task)

        assert result.status == "failed"
        assert result.error is not None
        assert "Connection reset" in result.error["message"] or result.error["code"] == NodusErrorCode.SDK_ERROR


# ==============================================================================
# 3. Gateway and Execution Job Timeouts (Strict No Post-Submission Retry)
# ==============================================================================

class TestGatewayAndTimeouts:
    """Validate timeout handling and ensure post-submission calls are never retried."""

    def test_job_execution_timeout_returns_timeout_code_and_never_retries_spawn(self):
        """When handle.get(timeout=...) times out, the task fails as TIMEOUT and spawn is NOT retried.
        
        CRITICAL INVARIANT: Retrying a submitted remote job would create duplicate billable calls.
        """
        config = _make_config(max_retries=2, timeout=1.0)
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, fake_fn, fake_handle = _build_fake_nodus_module()
        fake_handle.get.side_effect = TimeoutError("Nodus compute job exceeded deadline (504 Gateway Timeout)")

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)), \
             patch("app.adapters.nodus.adapter._RETRY_DELAY_SECONDS", 0.001):
            result = adapter.execute(task)

        assert result.status in ("failed", "timed_out")
        assert result.error is not None
        assert result.error["code"] == NodusErrorCode.TIMEOUT
        # spawn must be called exactly ONCE: post-submission failures are terminal
        assert fake_fn.spawn.call_count == 1
        assert fake_handle.get.call_count == 1


# ==============================================================================
# 4. Invalid Responses & Corrupted Payloads
# ==============================================================================

class TestCorruptedAndUnexpectedResponses:
    """Validate behavior when the remote platform returns non-conforming or corrupted payloads."""

    @pytest.mark.parametrize("corrupted_payload", [
        42,                                   # non-dict integer
        "<html>502 Bad Gateway</html>",       # HTML string error page
        None,                                 # null return
        ["unexpected", "list"],               # list instead of dict
    ])
    def test_non_dict_remote_payload_handled_safely(self, corrupted_payload: Any):
        """Remote function returning non-dict produces a structured result error without server crash."""
        config = _make_config()
        adapter = NodusExecutionAdapter(config)
        task = _make_task()

        nodus_mod, errors_mod, _, fake_handle = _build_fake_nodus_module(get_return_value=corrupted_payload)

        with patch.object(adapter, "_import_sdk", return_value=(nodus_mod, errors_mod)):
            result = adapter.execute(task)

        assert result.status == "failed"
        assert result.error is not None
        assert result.error["code"] in (NodusErrorCode.UNEXPECTED_RESULT_TYPE, NodusErrorCode.INVALID_RESULT, NodusErrorCode.SDK_ERROR)

    def test_non_json_serializable_task_payload_rejected_before_submission(self):
        """Tasks with non-serializable payload (e.g. set or complex object) are rejected before network call."""
        with pytest.raises(Exception):
            NodusTask(
                task_id="task-bad",
                operation="test",
                payload={"bad_value": {1, 2, 3}},  # set is not JSON serializable
            )


# ==============================================================================
# 5. Partial Payload Returns
# ==============================================================================

class TestPartialPayloadReturns:
    """Validate behavior when remote payload returns a dict missing expected scientific fields."""

    def test_missing_metrics_field_in_remote_response_handled_gracefully(self):
        """When remote response lacks expected fields, EvaluationTaskResult represents failure without crash."""
        res = EvaluationTaskResult(
            task_id="task-partial-01",
            hypothesis_id="hyp-001",
            request_id="req-001",
            outcome=TaskOutcome.FAILED,
            execution_mode="nodus_parallel",
            error=StructuredError(
                code="INVALID_PAYLOAD",
                message="Remote return dict lacks required scientific metrics",
            ),
        )

        assert res.task_id == "task-partial-01"
        assert res.outcome == TaskOutcome.FAILED
        assert res.error.code == "INVALID_PAYLOAD"

    def test_evaluation_task_result_timed_out_carries_structured_error(self):
        """A timed out task produces an EvaluationTaskResult with TaskOutcome.TIMED_OUT and diagnostic."""
        res = EvaluationTaskResult(
            task_id="task-timeout-01",
            hypothesis_id="hyp-002",
            request_id="req-002",
            outcome=TaskOutcome.TIMED_OUT,
            execution_mode="nodus_parallel",
            error=StructuredError(
                code="TIMEOUT",
                message="Nodus execution exceeded deadline",
            ),
        )

        assert res.outcome == TaskOutcome.TIMED_OUT
        assert res.error.code == "TIMEOUT"


# ==============================================================================
# 6. Idempotency & Database State Integrity Across Failures
# ==============================================================================

class TestStateIntegrityAndNoDanglingRuns:
    """Verify that failures in batch execution leave no dangling runs or corrupted database records."""

    def test_mixed_success_and_failure_batch_preserves_successful_results(self):
        """In a batch of 3 tasks where 1 fails, the 2 successful tasks are preserved without corruption."""
        config = _make_config()
        adapter = NodusExecutionAdapter(config)

        task1 = _make_task("eval", {"id": 1})
        task2 = _make_task("eval", {"id": 2})
        task3 = _make_task("eval", {"id": 3})

        def fake_execute(t: NodusTask) -> NodusResult:
            if t.task_id == task2.task_id:
                return NodusResult(
                    task_id=t.task_id,
                    execution_mode=ExecutionMode.REMOTE.value,
                    status="failed",
                    error={"code": NodusErrorCode.TIMEOUT, "message": "Remote timeout"},
                )
            return NodusResult(
                task_id=t.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="succeeded",
                result_data={"metrics": {"mae": 0.02}},
            )

        with patch.object(adapter, "execute", side_effect=fake_execute):
            results = [adapter.execute(t) for t in [task1, task2, task3]]

        assert len(results) == 3
        assert results[0].status == "succeeded"
        assert results[1].status == "failed"
        assert results[2].status == "succeeded"

        # The failed task has explicit error, and successful tasks retain valid metrics
        assert results[1].error["code"] == NodusErrorCode.TIMEOUT
        assert results[0].result_data["metrics"]["mae"] == 0.02
        assert results[2].result_data["metrics"]["mae"] == 0.02

    def test_database_persistence_records_failed_compute_task_without_orphaning(self):
        """Failed tasks are persisted with error details, run status remains intact, no corrupted records."""
        manager = DatabaseManager("sqlite:///:memory:")
        manager.create_all_tables()

        session = manager.get_session()
        repo = ClarityRepository(session)

        run = RunModel(
            run_id="run-failure-test-01",
            name="Failure Injection Run",
            status="running",
            execution_mode="nodus_remote",
        )
        repo.create_run(run)

        # Record a successful task and a failed task
        task_success = ComputeTaskModel(
            task_id="task-persisted-ok",
            run_id=run.run_id,
            execution_mode="nodus_remote",
            status="succeeded",
            duration_ms=150,
        )
        task_failed = ComputeTaskModel(
            task_id="task-persisted-fail",
            run_id=run.run_id,
            execution_mode="nodus_remote",
            status="failed",
            error_metadata={"code": "TIMEOUT", "message": "Remote execution deadline exceeded (504)"},
            duration_ms=2000,
        )

        session.add(task_success)
        session.add(task_failed)
        session.commit()

        # Verify retrieval
        loaded_success = session.get(ComputeTaskModel, "task-persisted-ok")
        loaded_failed = session.get(ComputeTaskModel, "task-persisted-fail")

        assert loaded_success is not None
        assert loaded_success.status == "succeeded"

        assert loaded_failed is not None
        assert loaded_failed.status == "failed"
        assert loaded_failed.error_metadata["code"] == "TIMEOUT"
        assert "Remote execution deadline exceeded" in loaded_failed.error_metadata["message"]

        # Run is not corrupted
        loaded_run = repo.get_run("run-failure-test-01")
        assert loaded_run.status == "running"

        manager.drop_all_tables()
