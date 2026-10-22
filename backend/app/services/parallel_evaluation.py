"""Parallel hypothesis/candidate evaluation orchestration (Issue #13).

The service sits between the Issue #6 evaluation pipeline and the Issue #12
Nodus execution adapter. It owns task construction, execution-mode enforcement,
bounded concurrency, retry policy, result correlation, deterministic
aggregation, and execution metrics. It does not implement fitting, metrics,
ranking, or provider SDK details.

Submission and aggregation are deliberately separate:

* :meth:`ParallelEvaluationRunner.run` builds tasks, dispatches them through the
  selected mode, and validates every returned envelope.
* :func:`_aggregate` orders validated per-task results by the original request
  order, independent of completion order.

Execution modes are explicit. ``nodus_parallel`` requires the underlying
adapter to be configured for ``nodus_remote``; there is no silent fallback to
local or mock execution, and a remote failure never becomes a local result.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.adapters.nodus.contracts import ExecutionMode, NodusResult, NodusTask
from app.domain.contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    Observation,
    StructuredError,
)
from app.domain.budget_contracts import BudgetAdmissionDenied, ParallelBudgetPort
from app.domain.fitting import FittingConfig
from app.services.evaluation_task_runtime import (
    ERROR_INVALID_TASK_RESULT,
    EVALUATION_TASK_SCHEMA_VERSION,
    OPERATION_EVALUATE_CANDIDATE,
    TaskContractError,
    build_evaluation_task_payload,
    execute_evaluation_task_payload,
    parse_task_result_envelope,
)

PARALLEL_EVALUATION_VERSION = "1.0"
MAX_CONCURRENCY_LIMIT = 32
MAX_RETRY_LIMIT = 5

ERROR_INVALID_CONFIG = "NODUS_PARALLEL_INVALID_CONFIG"
ERROR_MODE_UNAVAILABLE = "NODUS_PARALLEL_MODE_UNAVAILABLE"
ERROR_DISPATCH_FAILED = "NODUS_PARALLEL_DISPATCH_FAILED"
ERROR_MISSING_TASK_RESULT = "NODUS_PARALLEL_MISSING_TASK_RESULT"
ERROR_DUPLICATE_TASK_RESULT = "NODUS_PARALLEL_DUPLICATE_TASK_RESULT"
ERROR_CANCELLED = "NODUS_PARALLEL_CANCELLED"
ERROR_EVALUATION_BUDGET_EXHAUSTED = "EVALUATION_LIMIT_REACHED"
ERROR_CONCURRENCY_LIMIT = "CONCURRENCY_LIMIT_REACHED"

# Bounded wait for a per-run remote slot before the batch yields backpressure.
MAX_SLOT_WAIT_SECONDS = 5.0
SLOT_POLL_SECONDS = 0.05

# Adapter error codes that may be retried by this orchestrator. Retries are
# limited to failures observed *before* the provider accepted the task
# (``provider_task_id is None``) so a billable call is never re-submitted blindly.
RETRYABLE_ADAPTER_ERROR_CODES = frozenset(
    {
        "NODUS_RATE_LIMITED",
        "NODUS_SDK_ERROR",
    }
)

MODE_LOCAL_SEQUENTIAL = "local_sequential"
MODE_NODUS_PARALLEL = "nodus_parallel"


class ParallelExecutionMode(str, Enum):
    """Explicit execution mode for one parallel evaluation batch."""

    LOCAL_SEQUENTIAL = MODE_LOCAL_SEQUENTIAL
    NODUS_PARALLEL = MODE_NODUS_PARALLEL

    @classmethod
    def from_str(cls, value: str) -> "ParallelExecutionMode":
        normalized = value.strip().lower()
        for mode in cls:
            if mode.value == normalized:
                return mode
        raise ValueError(
            f"Invalid parallel execution mode: {value!r}. Must be one of: "
            f"{', '.join(mode.value for mode in cls)}"
        )


class ParallelConfigurationError(Exception):
    """Raised when the parallel orchestration cannot start as configured."""

    def __init__(
        self,
        code: str = ERROR_INVALID_CONFIG,
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message or "Parallel evaluation configuration is invalid."
        self.details = details or {}
        super().__init__(self.message)

    def to_safe_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


def _require_positive_int(value: Any, argument: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ParallelConfigurationError(
            ERROR_INVALID_CONFIG,
            f"{argument} must be an integer.",
            {"argument": argument, "received_type": type(value).__name__},
        )
    if value < 1 or value > maximum:
        raise ParallelConfigurationError(
            ERROR_INVALID_CONFIG,
            f"{argument} must be between 1 and {maximum}.",
            {"argument": argument, "received": value, "maximum": maximum},
        )
    return value


@dataclass(frozen=True)
class ParallelEvaluationConfig:
    """Validated configuration for one parallel evaluation runner.

    Fields:
      execution_mode: Explicit mode; there is no default.
      max_concurrency: Maximum number of in-flight tasks (1..32).
      max_retries: Additional orchestration-level submissions for a task whose
        adapter failure was classified retryable and was never accepted by the
        provider (0..5).
      retry_backoff_seconds: Fixed delay applied between retry rounds.
      timeout_seconds: Per-task timeout; ``None`` uses the adapter's configured
        timeout.
    """

    execution_mode: ParallelExecutionMode
    max_concurrency: int = 4
    max_retries: int = 1
    retry_backoff_seconds: float = 1.0
    timeout_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        mode = self.execution_mode
        if isinstance(mode, str) and not isinstance(mode, ParallelExecutionMode):
            mode = ParallelExecutionMode.from_str(mode)
            object.__setattr__(self, "execution_mode", mode)
        if not isinstance(mode, ParallelExecutionMode):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "execution_mode must be a ParallelExecutionMode.",
                {"received_type": type(mode).__name__},
            )

        _require_positive_int(self.max_concurrency, "max_concurrency", maximum=MAX_CONCURRENCY_LIMIT)

        if isinstance(self.max_retries, bool) or not isinstance(self.max_retries, int):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "max_retries must be an integer.",
                {"received_type": type(self.max_retries).__name__},
            )
        if self.max_retries < 0 or self.max_retries > MAX_RETRY_LIMIT:
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                f"max_retries must be between 0 and {MAX_RETRY_LIMIT}.",
                {"received": self.max_retries},
            )

        backoff = self.retry_backoff_seconds
        if (
            isinstance(backoff, bool)
            or not isinstance(backoff, (int, float))
            or not math.isfinite(float(backoff))
            or float(backoff) < 0.0
        ):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "retry_backoff_seconds must be a finite number greater than or equal to 0.",
                {"received": str(backoff)},
            )

        if self.timeout_seconds is not None:
            timeout = self.timeout_seconds
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(float(timeout))
                or float(timeout) <= 0.0
            ):
                raise ParallelConfigurationError(
                    ERROR_INVALID_CONFIG,
                    "timeout_seconds must be a finite number greater than 0 or null.",
                    {"received": str(timeout)},
                )

    @classmethod
    def from_environment(cls) -> "ParallelEvaluationConfig":
        """Load configuration from ``NODUS_PARALLEL_*``/``NODUS_MAX_CONCURRENCY``.

        ``NODUS_PARALLEL_EXECUTION_MODE`` is required: an unset mode fails loudly
        instead of silently selecting local or remote execution.
        """
        raw_mode = os.environ.get("NODUS_PARALLEL_EXECUTION_MODE", "").strip()
        if not raw_mode:
            raise ParallelConfigurationError(
                ERROR_MODE_UNAVAILABLE,
                "NODUS_PARALLEL_EXECUTION_MODE is not set. "
                "Set it to local_sequential or nodus_parallel.",
                {"variable": "NODUS_PARALLEL_EXECUTION_MODE"},
            )
        try:
            mode = ParallelExecutionMode.from_str(raw_mode)
        except ValueError as exc:
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG, str(exc), {"variable": "NODUS_PARALLEL_EXECUTION_MODE"}
            ) from exc

        def _read_int(name: str, default: int) -> int:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError as exc:
                raise ParallelConfigurationError(
                    ERROR_INVALID_CONFIG,
                    f"{name} must be an integer.",
                    {"variable": name, "received": raw},
                ) from exc

        def _read_float(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            if not raw:
                return default
            try:
                return float(raw)
            except ValueError as exc:
                raise ParallelConfigurationError(
                    ERROR_INVALID_CONFIG,
                    f"{name} must be a number.",
                    {"variable": name, "received": raw},
                ) from exc

        raw_timeout = os.environ.get("NODUS_PARALLEL_TASK_TIMEOUT_SECONDS", "").strip()
        timeout_seconds: Optional[float] = None
        if raw_timeout:
            try:
                timeout_seconds = float(raw_timeout)
            except ValueError as exc:
                raise ParallelConfigurationError(
                    ERROR_INVALID_CONFIG,
                    "NODUS_PARALLEL_TASK_TIMEOUT_SECONDS must be a number.",
                    {"variable": "NODUS_PARALLEL_TASK_TIMEOUT_SECONDS", "received": raw_timeout},
                ) from exc

        return cls(
            execution_mode=mode,
            max_concurrency=_read_int("NODUS_MAX_CONCURRENCY", 4),
            max_retries=_read_int("NODUS_PARALLEL_MAX_RETRIES", 1),
            retry_backoff_seconds=_read_float("NODUS_PARALLEL_RETRY_BACKOFF_SECONDS", 1.0),
            timeout_seconds=timeout_seconds,
        )


class TaskOutcome(str, Enum):
    """Terminal outcome of one evaluation task."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class EvaluationTaskResult:
    """Validated, correlated result for one candidate evaluation task."""

    task_id: str
    hypothesis_id: str
    request_id: str
    outcome: TaskOutcome
    execution_mode: str
    attempts: int = 1
    evaluation: Optional[EvaluationResult] = None
    error: Optional[StructuredError] = None
    provider_task_id: Optional[str] = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)
    duration_seconds: Optional[float] = None
    submitted_at: Optional[str] = None
    completed_at: Optional[str] = None
    cancellation_requested: bool = False

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id must be a non-empty string.")
        if not self.hypothesis_id or not isinstance(self.hypothesis_id, str):
            raise ValueError("hypothesis_id must be a non-empty string.")
        if not isinstance(self.outcome, TaskOutcome):
            raise ValueError("outcome must be a TaskOutcome.")
        if self.attempts < 0:
            raise ValueError("attempts cannot be negative.")
        if self.outcome == TaskOutcome.SUCCEEDED:
            if self.evaluation is None:
                raise ValueError("A succeeded task requires a validated evaluation.")
            if self.error is not None:
                raise ValueError("A succeeded task cannot also carry a task error.")
        else:
            if self.error is None:
                raise ValueError(f"A {self.outcome.value} task requires a structured error.")
            if self.evaluation is not None:
                raise ValueError(
                    "A failed/timed-out/cancelled task must not carry an evaluation score."
                )

    @property
    def is_success(self) -> bool:
        return self.outcome == TaskOutcome.SUCCEEDED

    @property
    def evaluation_failed(self) -> bool:
        """True when the task succeeded but its scientific evaluation failed."""
        return (
            self.evaluation is not None
            and self.evaluation.status == EvaluationStatus.FAILED
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "hypothesis_id": self.hypothesis_id,
            "request_id": self.request_id,
            "outcome": self.outcome.value,
            "execution_mode": self.execution_mode,
            "attempts": self.attempts,
            "evaluation": self.evaluation.to_dict() if self.evaluation is not None else None,
            "error": self.error.to_dict() if self.error is not None else None,
            "provider_task_id": self.provider_task_id,
            "provider_metadata": dict(self.provider_metadata),
            "duration_seconds": self.duration_seconds,
            "submitted_at": self.submitted_at,
            "completed_at": self.completed_at,
            "cancellation_requested": self.cancellation_requested,
        }


@dataclass(frozen=True)
class ParallelExecutionSummary:
    """Execution metrics derived from real orchestration events."""

    requested_mode: str
    actual_mode: str
    requested_tasks: int
    submitted_tasks: int
    completed_tasks: int
    evaluation_failures: int
    failed_tasks: int
    timed_out_tasks: int
    cancelled_tasks: int
    max_concurrency: int
    retry_attempts: int
    duration_seconds: float
    provider_metadata_available: bool
    cost_status: str = "unavailable"
    estimated_cost: Optional[float] = None
    settled_cost: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": PARALLEL_EVALUATION_VERSION,
            "requested_mode": self.requested_mode,
            "actual_mode": self.actual_mode,
            "requested_tasks": self.requested_tasks,
            "submitted_tasks": self.submitted_tasks,
            "completed_tasks": self.completed_tasks,
            "evaluation_failures": self.evaluation_failures,
            "failed_tasks": self.failed_tasks,
            "timed_out_tasks": self.timed_out_tasks,
            "cancelled_tasks": self.cancelled_tasks,
            "max_concurrency": self.max_concurrency,
            "retry_attempts": self.retry_attempts,
            "duration_seconds": self.duration_seconds,
            "provider_metadata_available": self.provider_metadata_available,
            # The verified SDK surface exposes no per-call cost field. These
            # remain explicitly unavailable rather than being estimated as 0.
            "cost_status": self.cost_status,
            "estimated_cost": self.estimated_cost,
            "settled_cost": self.settled_cost,
        }


@dataclass(frozen=True)
class ParallelEvaluationBatch:
    """Ordered per-task results plus batch-level metrics."""

    results: Tuple[EvaluationTaskResult, ...]
    summary: ParallelExecutionSummary
    unexpected_task_ids: Tuple[str, ...] = ()
    duplicate_task_ids: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "results": [result.to_dict() for result in self.results],
            "summary": self.summary.to_dict(),
            "unexpected_task_ids": list(self.unexpected_task_ids),
            "duplicate_task_ids": list(self.duplicate_task_ids),
        }


@dataclass(frozen=True)
class _TaskDescriptor:
    index: int
    task: NodusTask
    hypothesis_id: str
    request_id: str


def _utc_iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment is not None else None


def _stable_task_id(index: int, payload: Mapping[str, Any]) -> str:
    """Deterministic task ID derived from the position and validated payload."""
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]
    return f"eval-{index:04d}-{digest}"


def _cancel_requested(cancel_event: Any) -> bool:
    check = getattr(cancel_event, "is_set", None)
    return bool(callable(check) and check())


def _structured_error_from_adapter(
    result: NodusResult,
    *,
    default_code: str,
    default_message: str,
) -> StructuredError:
    raw = result.error if isinstance(result.error, Mapping) else {}
    code = raw.get("code")
    message = raw.get("message")
    context = raw.get("context")
    return StructuredError(
        code=code if isinstance(code, str) and code else default_code,
        message=message if isinstance(message, str) and message else default_message,
        details=dict(context) if isinstance(context, Mapping) else None,
    )


class ParallelEvaluationRunner:
    """Dispatches independent candidate evaluations and aggregates results.

    ``adapter`` must expose ``config.execution_mode`` and
    ``execute_many(tasks, max_concurrency=...)`` (the Issue #12 adapter does).
    Local mode never calls the adapter.
    """

    def __init__(
        self,
        adapter: Any,
        config: Optional[ParallelEvaluationConfig] = None,
        *,
        fitting_config: Optional[FittingConfig] = None,
        sleep: Callable[[float], None] = time.sleep,
        budget: Optional[ParallelBudgetPort] = None,
    ) -> None:
        self._adapter = adapter
        if budget is not None and not isinstance(budget, ParallelBudgetPort):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "budget must implement the ParallelBudgetPort contract.",
                {"received_type": type(budget).__name__},
            )
        self._budget = budget
        self._config = config if config is not None else ParallelEvaluationConfig.from_environment()
        if not isinstance(self._config, ParallelEvaluationConfig):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "config must be a ParallelEvaluationConfig.",
                {"received_type": type(self._config).__name__},
            )
        self._fitting_config = fitting_config if fitting_config is not None else FittingConfig()
        config_error = self._fitting_config.validate()
        if config_error is not None:
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "fitting_config is invalid.",
                {"cause": config_error.to_dict()},
            )
        self._sleep = sleep

    @property
    def config(self) -> ParallelEvaluationConfig:
        return self._config

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        requests: Sequence[EvaluationRequest],
        observations: Mapping[str, Observation],
        *,
        dataset_id: Optional[str] = None,
        run_id: Optional[str] = None,
        cancel_event: Any = None,
    ) -> ParallelEvaluationBatch:
        """Evaluate every request and return input-ordered results.

        One failing task never discards unrelated successful results. A remote
        mode that cannot start raises :class:`ParallelConfigurationError`
        instead of falling back to local execution.
        """
        if isinstance(requests, (str, bytes)) or not isinstance(requests, Sequence):
            raise ParallelConfigurationError(
                ERROR_INVALID_CONFIG,
                "requests must be a sequence of EvaluationRequest objects.",
                {"received_type": type(requests).__name__},
            )
        descriptors = self._build_descriptors(requests, observations, dataset_id)

        # Candidate evaluations are admitted against the persisted per-run
        # budget before any fitting or remote submission starts. A denial
        # produces explicit failures rather than silently dropping candidates.
        budget_token: Optional[str] = None
        if self._budget is not None and descriptors:
            operation_id = (
                f"parallel-eval:{run_id or 'run'}:{uuid.uuid4().hex[:10]}"
            )
            try:
                budget_token = self._budget.reserve_candidate_evaluations(
                    len(descriptors), operation_id
                )
            except BudgetAdmissionDenied as exc:
                denied_mode = (
                    MODE_LOCAL_SEQUENTIAL
                    if self._config.execution_mode == ParallelExecutionMode.LOCAL_SEQUENTIAL
                    else MODE_NODUS_PARALLEL
                )
                denied_results = tuple(
                    self._failure_result(
                        descriptor,
                        attempts=0,
                        code=ERROR_EVALUATION_BUDGET_EXHAUSTED,
                        message=(
                            "The candidate evaluation budget for this run is exhausted; "
                            "the task was not dispatched."
                        ),
                        details=exc.to_dict(),
                        execution_mode=denied_mode,
                    )
                    for descriptor in descriptors
                )
                summary = self._summarize(
                    denied_results, 0, denied_mode, 0.0
                )
                return ParallelEvaluationBatch(
                    results=denied_results,
                    summary=summary,
                    unexpected_task_ids=(),
                    duplicate_task_ids=(),
                )

        started = time.perf_counter()
        if self._config.execution_mode == ParallelExecutionMode.LOCAL_SEQUENTIAL:
            results, submitted = self._run_local(descriptors, cancel_event)
            actual_mode = MODE_LOCAL_SEQUENTIAL
            unexpected: Tuple[str, ...] = ()
            duplicates: Tuple[str, ...] = ()
        else:
            (
                results,
                submitted,
                actual_mode,
                unexpected,
                duplicates,
            ) = self._run_remote(descriptors, run_id, cancel_event)
        duration = time.perf_counter() - started

        if budget_token is not None:
            if submitted > 0:
                self._budget.complete_candidate_evaluation(budget_token, True)
            else:
                # Nothing was ever dispatched: return the reserved capacity.
                self._budget.release_candidate_evaluations(budget_token)

        ordered = _aggregate(descriptors, results)
        summary = self._summarize(ordered, submitted, actual_mode, duration)
        return ParallelEvaluationBatch(
            results=ordered,
            summary=summary,
            unexpected_task_ids=unexpected,
            duplicate_task_ids=duplicates,
        )

    # ------------------------------------------------------------------
    # Task construction
    # ------------------------------------------------------------------

    def _build_descriptors(
        self,
        requests: Sequence[EvaluationRequest],
        observations: Mapping[str, Observation],
        dataset_id: Optional[str],
    ) -> List[_TaskDescriptor]:
        descriptors: List[_TaskDescriptor] = []
        for index, request in enumerate(requests):
            if not isinstance(request, EvaluationRequest):
                raise ParallelConfigurationError(
                    ERROR_INVALID_CONFIG,
                    "requests must contain EvaluationRequest objects.",
                    {"index": index, "received_type": type(request).__name__},
                )
            try:
                payload = build_evaluation_task_payload(
                    request,
                    observations,
                    dataset_id=dataset_id,
                    fitting_config=self._fitting_config,
                )
            except TaskContractError as exc:
                raise ParallelConfigurationError(
                    exc.code,
                    exc.message,
                    {**exc.details, "request_index": index},
                ) from exc

            task = NodusTask(
                task_id=_stable_task_id(index, payload),
                operation=OPERATION_EVALUATE_CANDIDATE,
                payload=payload,
                correlation_id=dataset_id,
                timeout_seconds=self._timeout_seconds(),
                # The orchestrator owns application-level retries (see
                # _should_retry). Disabling the adapter's own retry loop keeps the
                # total attempt count bounded by one documented policy.
                max_retries=0,
                metadata={
                    "hypothesis_id": request.hypothesis.hypothesis_id,
                    "request_id": request.request_id,
                    "request_index": index,
                    "schema_version": EVALUATION_TASK_SCHEMA_VERSION,
                    "parallel_execution_mode": self._config.execution_mode.value,
                },
            )
            descriptors.append(
                _TaskDescriptor(
                    index=index,
                    task=task,
                    hypothesis_id=request.hypothesis.hypothesis_id,
                    request_id=request.request_id,
                )
            )
        return descriptors

    def _timeout_seconds(self) -> float:
        if self._config.timeout_seconds is not None:
            return float(self._config.timeout_seconds)
        adapter_config = getattr(self._adapter, "config", None)
        adapter_timeout = getattr(adapter_config, "timeout_seconds", None)
        if isinstance(adapter_timeout, (int, float)) and float(adapter_timeout) > 0:
            return float(adapter_timeout)
        return 30.0

    # ------------------------------------------------------------------
    # Local sequential execution
    # ------------------------------------------------------------------

    def _run_local(
        self, descriptors: Sequence[_TaskDescriptor], cancel_event: Any
    ) -> Tuple[List[EvaluationTaskResult], int]:
        results: List[EvaluationTaskResult] = []
        submitted = 0
        for descriptor in descriptors:
            if _cancel_requested(cancel_event):
                results.append(self._cancelled_result(descriptor, attempts=0))
                continue
            started = time.perf_counter()
            submitted += 1
            envelope = execute_evaluation_task_payload(dict(descriptor.task.payload))
            duration = time.perf_counter() - started
            results.append(
                self._map_envelope(
                    descriptor,
                    envelope,
                    execution_mode=MODE_LOCAL_SEQUENTIAL,
                    attempts=1,
                    duration_seconds=duration,
                )
            )
        return results, submitted

    # ------------------------------------------------------------------
    # Remote parallel execution
    # ------------------------------------------------------------------

    def _run_remote(
        self,
        descriptors: Sequence[_TaskDescriptor],
        run_id: Optional[str],
        cancel_event: Any,
    ) -> Tuple[
        List[EvaluationTaskResult],
        int,
        str,
        Tuple[str, ...],
        Tuple[str, ...],
    ]:
        self._assert_remote_mode()

        pending: List[_TaskDescriptor] = list(descriptors)
        settled: Dict[str, EvaluationTaskResult] = {}
        attempts_by_task: Dict[str, int] = {descriptor.task.task_id: 0 for descriptor in descriptors}
        submitted: set[str] = set()
        unexpected: set[str] = set()
        duplicates: set[str] = set()

        rounds = self._config.max_retries + 1
        for round_index in range(rounds):
            if not pending:
                break
            if _cancel_requested(cancel_event):
                for descriptor in pending:
                    settled[descriptor.task.task_id] = self._cancelled_result(
                        descriptor, attempts=attempts_by_task[descriptor.task.task_id]
                    )
                pending = []
                break

            round_descriptors = list(pending)
            for descriptor in round_descriptors:
                attempts_by_task[descriptor.task.task_id] += 1
                submitted.add(descriptor.task.task_id)

            slot_tokens: Optional[Sequence[str]] = None
            if self._budget is not None and round_descriptors:
                slot_tokens = self._acquire_remote_slots(
                    len(round_descriptors),
                    f"{run_id or 'run'}:slots:{round_index}:{uuid.uuid4().hex[:8]}",
                )
                if slot_tokens is None:
                    # Concurrency is temporary backpressure, not exhaustion.
                    for descriptor in round_descriptors:
                        settled[descriptor.task.task_id] = self._failure_result(
                            descriptor,
                            attempts=attempts_by_task[descriptor.task.task_id],
                            code=ERROR_CONCURRENCY_LIMIT,
                            message=(
                                "The per-run remote concurrency limit stayed saturated; the "
                                "task was not dispatched and no score was produced."
                            ),
                            details={
                                "max_concurrent_remote_tasks": getattr(
                                    self._budget, "max_concurrent_remote_tasks", None
                                ),
                                "wait_seconds": MAX_SLOT_WAIT_SECONDS,
                            },
                            execution_mode=self._actual_adapter_mode(),
                        )
                    pending = []
                    break

            effective_concurrency = self._config.max_concurrency
            if slot_tokens is not None:
                effective_concurrency = min(effective_concurrency, len(slot_tokens))

            try:
                raw_results = self._adapter.execute_many(
                    [descriptor.task for descriptor in round_descriptors],
                    max_concurrency=effective_concurrency,
                )
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                # A batch-level exception may mean partial submission. Do not
                # resubmit: record an explicit failure for every pending task.
                for descriptor in round_descriptors:
                    settled[descriptor.task.task_id] = self._failure_result(
                        descriptor,
                        attempts=attempts_by_task[descriptor.task.task_id],
                        code=ERROR_DISPATCH_FAILED,
                        message="Dispatching the evaluation task failed; no score was produced.",
                        details={"exception_type": type(exc).__name__},
                        execution_mode=self._actual_adapter_mode(),
                    )
                pending = []
                break
            finally:
                if slot_tokens is not None:
                    self._budget.release_remote_slots(slot_tokens)

            if isinstance(raw_results, (str, bytes)) or not isinstance(raw_results, Sequence):
                for descriptor in round_descriptors:
                    settled[descriptor.task.task_id] = self._failure_result(
                        descriptor,
                        attempts=attempts_by_task[descriptor.task.task_id],
                        code=ERROR_INVALID_TASK_RESULT,
                        message="The execution adapter returned an invalid result collection.",
                        details={"received_type": type(raw_results).__name__},
                        execution_mode=self._actual_adapter_mode(),
                    )
                pending = []
                break

            expected_ids = {descriptor.task.task_id for descriptor in round_descriptors}
            by_task: Dict[str, NodusResult] = {}
            for raw in raw_results:
                if not isinstance(raw, NodusResult):
                    continue
                if raw.task_id not in expected_ids:
                    unexpected.add(str(raw.task_id))
                    continue
                if raw.task_id in by_task:
                    duplicates.add(raw.task_id)
                    continue
                by_task[raw.task_id] = raw

            retry_next: List[_TaskDescriptor] = []
            for descriptor in round_descriptors:
                task_id = descriptor.task.task_id
                attempts = attempts_by_task[task_id]
                nodus_result = by_task.get(task_id)
                if nodus_result is None:
                    # Ambiguous/missing results are never retried automatically.
                    settled[task_id] = self._failure_result(
                        descriptor,
                        attempts=attempts,
                        code=ERROR_MISSING_TASK_RESULT,
                        message=(
                            "The execution adapter returned no result for this task; "
                            "no score was produced."
                        ),
                        details={"task_id": task_id},
                        execution_mode=self._actual_adapter_mode(),
                    )
                    continue
                if (
                    self._should_retry(nodus_result)
                    and attempts < rounds
                ):
                    retry_next.append(descriptor)
                    continue
                settled[task_id] = self._map_adapter_result(
                    descriptor, nodus_result, attempts
                )

            pending = retry_next
            if pending and self._config.retry_backoff_seconds > 0:
                self._sleep(self._config.retry_backoff_seconds)

        return (
            list(settled.values()),
            len(submitted),
            self._actual_adapter_mode(),
            tuple(sorted(unexpected)),
            tuple(sorted(duplicates)),
        )

    def _acquire_remote_slots(
        self, count: int, operation_id: str
    ) -> Optional[Sequence[str]]:
        """Acquire *count* persisted slots, or None after bounded backpressure.

        Slot acquisition is all-or-none for a batch: a partial acquisition is
        never held while waiting for the rest, so two concurrent runners cannot
        deadlock on each other's slots.
        """
        if self._budget is None:
            return None
        polls = max(1, int(MAX_SLOT_WAIT_SECONDS / SLOT_POLL_SECONDS))
        for _ in range(polls):
            tokens = self._budget.try_acquire_remote_slots(count, operation_id)
            if tokens is not None:
                return tokens
            self._sleep(SLOT_POLL_SECONDS)
        return self._budget.try_acquire_remote_slots(count, operation_id)

    def _assert_remote_mode(self) -> None:
        adapter_config = getattr(self._adapter, "config", None)
        adapter_mode = getattr(adapter_config, "execution_mode", None)
        if adapter_mode != ExecutionMode.REMOTE:
            received = getattr(adapter_mode, "value", adapter_mode)
            raise ParallelConfigurationError(
                ERROR_MODE_UNAVAILABLE,
                "NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel requires "
                "NODUS_EXECUTION_MODE=nodus_remote; refusing to fall back to "
                "local or mock execution.",
                {"adapter_execution_mode": received},
            )
        dispatch = getattr(self._adapter, "execute_many", None)
        if not callable(dispatch):
            raise ParallelConfigurationError(
                ERROR_MODE_UNAVAILABLE,
                "The execution adapter does not support batch parallel execution.",
                {"received_type": type(self._adapter).__name__},
            )

    def _actual_adapter_mode(self) -> str:
        adapter_config = getattr(self._adapter, "config", None)
        adapter_mode = getattr(adapter_config, "execution_mode", None)
        return getattr(adapter_mode, "value", str(adapter_mode))

    def _should_retry(self, result: NodusResult) -> bool:
        """Retry only pre-acceptance, explicitly retryable failures."""
        if result.status != "failed":
            return False
        if result.provider_task_id:
            # The provider accepted the task; resubmitting could duplicate
            # billable work. Post-submission failures are terminal here.
            return False
        error = result.error if isinstance(result.error, Mapping) else {}
        code = error.get("code")
        return code in RETRYABLE_ADAPTER_ERROR_CODES

    # ------------------------------------------------------------------
    # Result mapping
    # ------------------------------------------------------------------

    def _map_adapter_result(
        self,
        descriptor: _TaskDescriptor,
        result: NodusResult,
        attempts: int,
    ) -> EvaluationTaskResult:
        execution_mode = result.execution_mode or self._actual_adapter_mode()
        provider_metadata = dict(result.provider_metadata or {})
        provider_task_id = result.provider_task_id
        error_context = (
            dict(result.error.get("context") or {})
            if isinstance(result.error, Mapping)
            else {}
        )

        if result.status == "succeeded":
            return self._map_envelope(
                descriptor,
                result.result_data,
                execution_mode=execution_mode,
                attempts=attempts,
                duration_seconds=result.duration_seconds,
                provider_task_id=provider_task_id,
                provider_metadata=provider_metadata,
                submitted_at=_utc_iso(result.submitted_at),
                completed_at=_utc_iso(result.completed_at),
            )

        if result.status == "timed_out":
            return self._failure_result(
                descriptor,
                attempts=attempts,
                code="NODUS_TIMEOUT",
                message="Task execution exceeded the configured timeout.",
                details=error_context or None,
                execution_mode=execution_mode,
                outcome=TaskOutcome.TIMED_OUT,
                provider_task_id=provider_task_id,
                provider_metadata=provider_metadata,
                duration_seconds=result.duration_seconds,
                submitted_at=_utc_iso(result.submitted_at),
                completed_at=_utc_iso(result.completed_at),
                cancellation_requested=bool(error_context.get("cancellation_requested")),
            )

        if result.status == "cancelled":
            return self._failure_result(
                descriptor,
                attempts=attempts,
                code="NODUS_CANCELLED",
                message="Task was cancelled before a result was produced.",
                details=error_context or None,
                execution_mode=execution_mode,
                outcome=TaskOutcome.CANCELLED,
                provider_task_id=provider_task_id,
                provider_metadata=provider_metadata,
                duration_seconds=result.duration_seconds,
                submitted_at=_utc_iso(result.submitted_at),
                completed_at=_utc_iso(result.completed_at),
                cancellation_requested=bool(error_context.get("cancellation_requested")),
            )

        error = result.error if isinstance(result.error, Mapping) else {}
        return self._failure_result(
            descriptor,
            attempts=attempts,
            code=error.get("code") or "NODUS_TASK_FAILED",
            message=(
                error.get("message")
                or "Remote task execution failed; no score was produced."
            ),
            details=error_context or None,
            execution_mode=execution_mode,
            provider_task_id=provider_task_id,
            provider_metadata=provider_metadata,
            duration_seconds=result.duration_seconds,
            submitted_at=_utc_iso(result.submitted_at),
            completed_at=_utc_iso(result.completed_at),
        )

    def _map_envelope(
        self,
        descriptor: _TaskDescriptor,
        envelope: Any,
        *,
        execution_mode: str,
        attempts: int,
        duration_seconds: Optional[float] = None,
        provider_task_id: Optional[str] = None,
        provider_metadata: Optional[Mapping[str, Any]] = None,
        submitted_at: Optional[str] = None,
        completed_at: Optional[str] = None,
    ) -> EvaluationTaskResult:
        try:
            parsed = parse_task_result_envelope(
                envelope, expected_hypothesis_id=descriptor.hypothesis_id
            )
        except TaskContractError as exc:
            return self._failure_result(
                descriptor,
                attempts=attempts,
                code=exc.code,
                message=exc.message,
                details=exc.details or None,
                execution_mode=execution_mode,
                provider_task_id=provider_task_id,
                provider_metadata=provider_metadata,
                duration_seconds=duration_seconds,
                submitted_at=submitted_at,
                completed_at=completed_at,
            )

        if parsed.status != "succeeded":
            return self._failure_result(
                descriptor,
                attempts=attempts,
                code=parsed.error.code if parsed.error is not None else ERROR_INVALID_TASK_RESULT,
                message=(
                    parsed.error.message
                    if parsed.error is not None
                    else "The evaluation worker reported a failure."
                ),
                details=dict(parsed.error.details) if parsed.error and parsed.error.details else None,
                execution_mode=execution_mode,
                provider_task_id=provider_task_id,
                provider_metadata=provider_metadata,
                duration_seconds=duration_seconds,
                submitted_at=submitted_at,
                completed_at=completed_at,
            )

        return EvaluationTaskResult(
            task_id=descriptor.task.task_id,
            hypothesis_id=descriptor.hypothesis_id,
            request_id=descriptor.request_id,
            outcome=TaskOutcome.SUCCEEDED,
            execution_mode=execution_mode,
            attempts=attempts,
            evaluation=parsed.evaluation,
            provider_task_id=provider_task_id,
            provider_metadata=dict(provider_metadata or {}),
            duration_seconds=duration_seconds,
            submitted_at=submitted_at,
            completed_at=completed_at,
        )

    def _failure_result(
        self,
        descriptor: _TaskDescriptor,
        *,
        attempts: int,
        code: str,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
        execution_mode: str,
        outcome: TaskOutcome = TaskOutcome.FAILED,
        provider_task_id: Optional[str] = None,
        provider_metadata: Optional[Mapping[str, Any]] = None,
        duration_seconds: Optional[float] = None,
        submitted_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        cancellation_requested: bool = False,
    ) -> EvaluationTaskResult:
        return EvaluationTaskResult(
            task_id=descriptor.task.task_id,
            hypothesis_id=descriptor.hypothesis_id,
            request_id=descriptor.request_id,
            outcome=outcome,
            execution_mode=execution_mode,
            attempts=attempts,
            evaluation=None,
            error=StructuredError(
                code=code,
                message=message,
                details=dict(details) if details else None,
            ),
            provider_task_id=provider_task_id,
            provider_metadata=dict(provider_metadata or {}),
            duration_seconds=duration_seconds,
            submitted_at=submitted_at,
            completed_at=completed_at,
            cancellation_requested=cancellation_requested,
        )

    def _cancelled_result(
        self, descriptor: _TaskDescriptor, *, attempts: int
    ) -> EvaluationTaskResult:
        return self._failure_result(
            descriptor,
            attempts=attempts,
            code=ERROR_CANCELLED,
            message="The task was cancelled before execution; no score was produced.",
            details={"task_id": descriptor.task.task_id},
            execution_mode=(
                self._actual_adapter_mode()
                if self._config.execution_mode == ParallelExecutionMode.NODUS_PARALLEL
                else MODE_LOCAL_SEQUENTIAL
            ),
            outcome=TaskOutcome.CANCELLED,
        )

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _summarize(
        self,
        ordered: Sequence[EvaluationTaskResult],
        submitted: int,
        actual_mode: str,
        duration_seconds: float,
    ) -> ParallelExecutionSummary:
        completed = sum(1 for result in ordered if result.outcome == TaskOutcome.SUCCEEDED)
        evaluation_failures = sum(1 for result in ordered if result.evaluation_failed)
        failed = sum(1 for result in ordered if result.outcome == TaskOutcome.FAILED)
        timed_out = sum(1 for result in ordered if result.outcome == TaskOutcome.TIMED_OUT)
        cancelled = sum(1 for result in ordered if result.outcome == TaskOutcome.CANCELLED)
        retries = sum(max(0, result.attempts - 1) for result in ordered)
        provider_metadata_available = any(
            result.provider_task_id or result.provider_metadata for result in ordered
        )
        return ParallelExecutionSummary(
            requested_mode=self._config.execution_mode.value,
            actual_mode=actual_mode,
            requested_tasks=len(ordered),
            submitted_tasks=submitted,
            completed_tasks=completed,
            evaluation_failures=evaluation_failures,
            failed_tasks=failed,
            timed_out_tasks=timed_out,
            cancelled_tasks=cancelled,
            max_concurrency=self._config.max_concurrency,
            retry_attempts=retries,
            duration_seconds=duration_seconds,
            provider_metadata_available=provider_metadata_available,
        )


def _aggregate(
    descriptors: Sequence[_TaskDescriptor],
    results: Sequence[EvaluationTaskResult],
) -> Tuple[EvaluationTaskResult, ...]:
    """Order results by the original request order, never by completion order."""
    by_task_id = {result.task_id: result for result in results}
    ordered: List[EvaluationTaskResult] = []
    for descriptor in descriptors:
        result = by_task_id.get(descriptor.task.task_id)
        if result is None:
            raise ParallelConfigurationError(
                ERROR_MISSING_TASK_RESULT,
                "Aggregation received no result for a submitted task.",
                {"task_id": descriptor.task.task_id},
            )
        ordered.append(result)
    return tuple(ordered)


def _failure_evaluation_result(
    request: EvaluationRequest,
    task_result: EvaluationTaskResult,
) -> EvaluationResult:
    """Represent a failed task as an evaluation failure, never as a score."""
    error = task_result.error or StructuredError(
        code=ERROR_INVALID_TASK_RESULT,
        message="The evaluation task produced no validated result.",
    )
    return EvaluationResult(
        hypothesis_id=request.hypothesis.hypothesis_id,
        status=EvaluationStatus.FAILED,
        metrics={},
        sample_count=0,
        error=error,
        model_family=request.hypothesis.model_family,
        request_id=request.request_id,
    )


class ParallelEvaluationPipeline:
    """``EvaluationPipeline.evaluate_many``-compatible facade.

    The discovery engine (#8) calls ``evaluate_many`` on its injected pipeline.
    This facade returns a real ``EvaluationResult`` per request, mapping a failed
    task to ``status=FAILED`` with its structured error and no metrics, so a
    failed or timed-out task can never look like a favorable candidate.
    """

    def __init__(
        self,
        runner: ParallelEvaluationRunner,
        *,
        dataset_id: Optional[str] = None,
        run_id: Optional[str] = None,
        cancel_event: Any = None,
    ) -> None:
        if not isinstance(runner, ParallelEvaluationRunner):
            raise TypeError("runner must be a ParallelEvaluationRunner.")
        self._runner = runner
        self._default_dataset_id = dataset_id
        self._run_id = run_id
        self._cancel_event = cancel_event
        self._last_batch: Optional[ParallelEvaluationBatch] = None

    @property
    def last_batch(self) -> Optional[ParallelEvaluationBatch]:
        return self._last_batch

    def evaluate_many(
        self,
        requests: Sequence[EvaluationRequest],
        observations: Mapping[str, Observation],
        dataset_id: Optional[str] = None,
    ) -> List[EvaluationResult]:
        batch = self._runner.run(
            requests,
            observations,
            dataset_id=dataset_id if dataset_id is not None else self._default_dataset_id,
            run_id=self._run_id,
            cancel_event=self._cancel_event,
        )
        self._last_batch = batch
        results: List[EvaluationResult] = []
        for request, task_result in zip(requests, batch.results):
            if task_result.evaluation is not None:
                results.append(task_result.evaluation)
            else:
                results.append(_failure_evaluation_result(request, task_result))
        return results


__all__ = [
    "PARALLEL_EVALUATION_VERSION",
    "MAX_CONCURRENCY_LIMIT",
    "MAX_RETRY_LIMIT",
    "MODE_LOCAL_SEQUENTIAL",
    "MODE_NODUS_PARALLEL",
    "RETRYABLE_ADAPTER_ERROR_CODES",
    "EvaluationTaskResult",
    "ParallelConfigurationError",
    "ParallelEvaluationBatch",
    "ParallelEvaluationConfig",
    "ParallelEvaluationPipeline",
    "ParallelEvaluationRunner",
    "ParallelExecutionMode",
    "ParallelExecutionSummary",
    "TaskOutcome",
]
