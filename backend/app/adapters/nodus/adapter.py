"""Nodus Compute execution adapter.

Provides a single ``execute(task: NodusTask) -> NodusResult`` interface for
submitting computational tasks to the Nodus platform (or to local/mock drivers
for testing).

Execution modes
---------------
nodus_remote  Execute on the real Nodus platform using the SDK.
              Requires ``NODUS_API_KEY``.
local         Execute the payload locally using a registered handler.
              Intended for development; a handler must be registered.
mock          Return a deterministic mock result without any remote call.
              Tests MUST set this mode explicitly; production configurations
              that omit the mode variable raise ``NodusConfigurationError``
              before any execution is attempted.

SDK import strategy
-------------------
``nodus`` is imported lazily (inside ``_remote_execute``) so that the module
remains importable without the SDK installed and without valid credentials.
ImportError is surfaced as ``NodusConfigurationError``.

Retry policy
------------
Retries are performed only for error codes classified as transient
(rate-limit and generic SDK errors).  Timeout, permission, not-found, and
cancellation failures are terminal and are not retried.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .config import NodusConfig
from .contracts import ExecutionMode, NodusResult, NodusTask
from .errors import (
    NodusAdapterError,
    NodusConfigurationError,
    NodusErrorCode,
    NodusExecutionError,
    NodusResultError,
    NodusTimeoutError,
    sanitize_result_error,
    sanitize_sdk_error,
)

logger = logging.getLogger(__name__)

# Error codes for which a retry is safe (idempotent submission assumed)
_RETRYABLE_CODES = frozenset(
    {
        NodusErrorCode.RATE_LIMITED,
        NodusErrorCode.SDK_ERROR,
        NodusErrorCode.REMOTE_EXECUTION_FAILED,
    }
)

# Seconds to wait between retry attempts (simple linear backoff)
_RETRY_DELAY_SECONDS = 2.0


class NodusExecutionAdapter:
    """Adapter for submitting tasks to the Nodus Compute platform.

    Parameters
    ----------
    config:
        Validated ``NodusConfig`` instance.  Obtain via
        ``NodusConfig.from_environment()`` in production or supply
        explicitly in tests.
    local_handlers:
        Optional mapping of ``operation`` name to callable for ``local``
        execution mode.  Each handler receives the task payload dict and
        must return a JSON-serializable dict.

    Example
    -------
    ::

        config = NodusConfig.from_environment()
        adapter = NodusExecutionAdapter(config)
        result = adapter.execute(task)
    """

    def __init__(
        self,
        config: NodusConfig,
        local_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
    ) -> None:
        self._config = config
        self._local_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = (
            local_handlers or {}
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(self, task: NodusTask) -> NodusResult:
        """Execute *task* according to the configured execution mode.

        Returns a ``NodusResult`` on all terminal outcomes — including failures.
        Raises ``NodusAdapterError`` only for configuration or pre-submission
        problems (invalid mode, missing credentials).

        Parameters
        ----------
        task:
            Validated ``NodusTask`` instance.

        Returns
        -------
        NodusResult
            Always has a terminal status: ``succeeded``, ``failed``,
            ``timed_out``, or ``cancelled``.
        """
        mode = self._config.execution_mode

        logger.info(
            "Nodus execute: task_id=%s operation=%s mode=%s",
            task.task_id,
            task.operation,
            mode.value,
        )

        if mode == ExecutionMode.REMOTE:
            return self._execute_with_retries(task, self._remote_execute)
        if mode == ExecutionMode.LOCAL:
            return self._local_execute(task)
        if mode == ExecutionMode.MOCK:
            return self._mock_execute(task)

        # Should never reach here — NodusConfig validation enforces valid modes
        raise NodusConfigurationError(
            NodusErrorCode.INVALID_MODE,
            message=f"Unhandled execution mode: {mode!r}",
        )

    # ------------------------------------------------------------------
    # Remote execution
    # ------------------------------------------------------------------

    def _execute_with_retries(
        self,
        task: NodusTask,
        driver: Callable[[NodusTask], NodusResult],
    ) -> NodusResult:
        """Execute *task* via *driver*, retrying on transient failures."""
        attempt = 0
        last_result: NodusResult | None = None

        while attempt <= task.max_retries:
            try:
                result = driver(task)
            except NodusAdapterError as exc:
                if exc.code not in _RETRYABLE_CODES or attempt >= task.max_retries:
                    # Terminal or retries exhausted — wrap as failed result
                    return _make_failed_result(task, exc.to_safe_dict(), mode=self._config.execution_mode)
                logger.warning(
                    "Nodus transient error (attempt %d/%d): code=%s",
                    attempt + 1,
                    task.max_retries + 1,
                    exc.code,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
                attempt += 1
                continue
            else:
                # Successful driver invocation; propagate result directly
                return result

        # Should not reach here but guard nonetheless
        return _make_failed_result(
            task,
            {
                "code": NodusErrorCode.RETRIES_EXHAUSTED,
                "message": "All retry attempts were exhausted.",
                "context": {"max_retries": task.max_retries},
            },
            mode=self._config.execution_mode,
        )

    def _remote_execute(self, task: NodusTask) -> NodusResult:
        """Execute *task* on the real Nodus platform via the SDK.

        Lazy-imports ``nodus`` so that the module can be imported without
        the SDK installed.
        """
        try:
            import nodus  # noqa: PLC0415 (lazy import by design)
            import nodus.errors as nodus_errors  # noqa: PLC0415
        except ImportError as exc:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message=(
                    "nodus-compute package is not installed. "
                    "Add nodus-compute==1.0.27 to requirements."
                ),
            ) from exc

        submitted_at = datetime.now(timezone.utc)

        try:
            # Build the Nodus client with explicit API key
            client_kwargs: dict[str, Any] = {"api_key": self._config.api_key}
            if self._config.base_url:
                client_kwargs["base_url"] = self._config.base_url

            client = nodus.Client(**client_kwargs)

            # Resolve the App and Function
            app = nodus.App(name=self._config.app_name, client=client)
            fn = nodus.functions._Function.from_name(
                name=self._config.function_name,
                app=app,
            )

            # Submit the task — spawn returns a handle
            spawn_kwargs: dict[str, Any] = {
                "operation": task.operation,
                "payload": task.payload,
                "task_id": task.task_id,
            }
            if task.correlation_id:
                spawn_kwargs["correlation_id"] = task.correlation_id

            handle = fn.spawn(**spawn_kwargs)

            # Wait for result with timeout
            raw_result = handle.get(timeout=self._config.timeout_seconds)

        except nodus_errors.TimeoutError as exc:
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="timed_out",
                error={
                    "code": NodusErrorCode.TIMEOUT,
                    "message": "Task execution exceeded the configured timeout.",
                    "context": {"timeout_seconds": self._config.timeout_seconds},
                },
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )
        except Exception as exc:  # noqa: BLE001 (broad catch at SDK boundary)
            safe_error = sanitize_sdk_error(exc)
            raise NodusExecutionError(
                code=safe_error["code"],
                message=safe_error["message"],
                context=safe_error["context"],
            ) from exc

        completed_at = datetime.now(timezone.utc)
        return self._normalize_remote_result(task, raw_result, submitted_at, completed_at)

    def _normalize_remote_result(
        self,
        task: NodusTask,
        raw_result: Any,
        submitted_at: datetime,
        completed_at: datetime,
    ) -> NodusResult:
        """Validate and normalize the raw SDK result into a ``NodusResult``.

        Raises ``NodusResultError`` if the result structure is unacceptable.
        """
        duration = (completed_at - submitted_at).total_seconds()

        if not isinstance(raw_result, dict):
            raise NodusResultError(
                code=NodusErrorCode.UNEXPECTED_RESULT_TYPE,
                message="Remote result is not a dict.",
                context={"result_type": type(raw_result).__name__},
            )

        status_field = raw_result.get("status", "")
        if status_field not in ("succeeded", "failed", "timed_out", "cancelled"):
            # Treat ambiguous status as a failed result
            status_field = "failed"

        # Extract provider task ID safely (never fabricate)
        provider_task_id: str | None = None
        raw_ptid = raw_result.get("task_id") or raw_result.get("id")
        if isinstance(raw_ptid, str) and raw_ptid:
            provider_task_id = raw_ptid

        # Extract result_data for succeeded tasks
        result_data: dict[str, Any] | None = None
        if status_field == "succeeded":
            raw_data = raw_result.get("result") or raw_result.get("output") or raw_result.get("data")
            if raw_data is not None and isinstance(raw_data, dict):
                result_data = raw_data
            elif raw_data is not None:
                raise NodusResultError(
                    code=NodusErrorCode.UNEXPECTED_RESULT_TYPE,
                    message="Result data is not a dict.",
                    context={"data_type": type(raw_data).__name__},
                )

        # Sanitize error information for non-success
        error_dict: dict[str, Any] | None = None
        if status_field != "succeeded":
            raw_error = raw_result.get("error") or raw_result.get("message")
            error_dict = sanitize_result_error(raw_error) if raw_error is not None else {
                "code": NodusErrorCode.REMOTE_EXECUTION_FAILED,
                "message": "Remote task did not succeed.",
                "context": {},
            }

        # Extract provider metadata safely
        provider_metadata: dict[str, Any] = {}
        raw_meta = raw_result.get("metadata") or raw_result.get("meta")
        if isinstance(raw_meta, dict):
            # Accept only string keys with simple scalar values
            for k, v in raw_meta.items():
                if isinstance(k, str) and isinstance(v, (str, int, float, bool, type(None))):
                    provider_metadata[k] = v

        return NodusResult(
            task_id=task.task_id,
            execution_mode=ExecutionMode.REMOTE.value,
            status=status_field,
            result_data=result_data,
            error=error_dict,
            provider_task_id=provider_task_id,
            submitted_at=submitted_at,
            completed_at=completed_at,
            duration_seconds=duration,
            provider_metadata=provider_metadata,
        )

    # ------------------------------------------------------------------
    # Local execution
    # ------------------------------------------------------------------

    def _local_execute(self, task: NodusTask) -> NodusResult:
        """Execute *task* locally using a registered handler.

        Raises ``NodusConfigurationError`` if no handler is registered for
        the requested operation.
        """
        handler = self._local_handlers.get(task.operation)
        if handler is None:
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message=(
                    f"No local handler registered for operation {task.operation!r}. "
                    "Register a handler or use mock/remote mode."
                ),
            )

        submitted_at = datetime.now(timezone.utc)
        try:
            result_data = handler(task.payload)
        except Exception as exc:  # noqa: BLE001
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.LOCAL.value,
                status="failed",
                error={
                    "code": NodusErrorCode.REMOTE_EXECUTION_FAILED,
                    "message": "Local handler raised an exception.",
                    "context": {"exception_type": type(exc).__name__},
                },
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )

        completed_at = datetime.now(timezone.utc)
        if not isinstance(result_data, dict):
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.LOCAL.value,
                status="failed",
                error={
                    "code": NodusErrorCode.UNEXPECTED_RESULT_TYPE,
                    "message": "Local handler must return a dict.",
                    "context": {"result_type": type(result_data).__name__},
                },
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )

        return NodusResult(
            task_id=task.task_id,
            execution_mode=ExecutionMode.LOCAL.value,
            status="succeeded",
            result_data=result_data,
            submitted_at=submitted_at,
            completed_at=completed_at,
            duration_seconds=(completed_at - submitted_at).total_seconds(),
        )

    # ------------------------------------------------------------------
    # Mock execution
    # ------------------------------------------------------------------

    def _mock_execute(self, task: NodusTask) -> NodusResult:
        """Return a deterministic mock result without any remote call.

        The result includes an explicit marker so callers can detect mock
        execution and prevent it from being treated as genuine remote proof.
        """
        submitted_at = datetime.now(timezone.utc)
        completed_at = datetime.now(timezone.utc)

        return NodusResult(
            task_id=task.task_id,
            execution_mode=ExecutionMode.MOCK.value,
            status="succeeded",
            result_data={
                "mock": True,
                "operation": task.operation,
                "echo_payload": task.payload,
            },
            submitted_at=submitted_at,
            completed_at=completed_at,
            duration_seconds=0.0,
            provider_metadata={"mock": True},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_failed_result(
    task: NodusTask,
    error: dict[str, Any],
    mode: ExecutionMode,
) -> NodusResult:
    """Construct a terminal failed ``NodusResult`` from a safe error dict."""
    now = datetime.now(timezone.utc)
    return NodusResult(
        task_id=task.task_id,
        execution_mode=mode.value,
        status="failed",
        error=error,
        submitted_at=now,
        completed_at=now,
        duration_seconds=0.0,
    )


def build_adapter_from_environment(
    local_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
) -> NodusExecutionAdapter:
    """Convenience factory that loads config from environment and returns an adapter.

    Raises ``NodusConfigurationError`` if environment is misconfigured.
    """
    config = NodusConfig.from_environment()
    return NodusExecutionAdapter(config, local_handlers=local_handlers)
