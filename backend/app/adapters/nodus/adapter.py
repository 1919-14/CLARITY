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

SDK usage (verified against nodus-compute==1.0.27)
---------------------------------------------------
* ``nodus.Function.from_name(app: str, name: str, project=None)`` — public API.
  Takes positional string arguments; does not accept an App object.
* ``nodus.Client(api_key=..., api_url=..., project=...)`` — set as the default
  client before calling any SDK methods via ``nodus.api.set_default_client``.
* ``fn.spawn(**kwargs) -> FunctionCall`` — submits the remote call; kwargs are
  passed directly to the deployed function.
* ``handle.get(timeout=...) -> Any`` — waits for the result and returns the
  function's *return value* directly (not a status envelope).
* ``handle.name: str`` — provider FunctionCall identifier (property).
* ``NodusError.retryable: bool`` — class-level boolean; True for
  ``TooManyRequests`` and ``Unavailable``; False for all others.

Retry policy (reviewer change #3)
----------------------------------
Retries are allowed **only** for errors that occur *before submission*
(i.e., during ``from_name`` or ``spawn``).  Once a handle is obtained,
``get()`` is never retried on a generic error — the task is already submitted
and retrying from ``spawn`` would create duplicate/billable executions.

The only retryable conditions are SDK errors with ``NodusError.retryable=True``
(``TooManyRequests``, ``Unavailable``) that are raised *before* a handle is
obtained.  All post-submission failures are terminal.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    sanitize_result_error,
    sanitize_sdk_error,
)

logger = logging.getLogger(__name__)

# Seconds to wait between pre-submission retry attempts
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
            return self._remote_execute(task)
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

    def _remote_execute(self, task: NodusTask) -> NodusResult:
        """Execute *task* on the real Nodus platform via the SDK.

        Phase 1 — pre-submission (spawn): retried on SDK-documented
        ``retryable=True`` errors (TooManyRequests, Unavailable).

        Phase 2 — post-submission (get): never retried. The task is already
        submitted; retrying ``spawn`` would create duplicate executions.

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

        # Phase 1 — resolve function and spawn (pre-submission, retryable)
        try:
            handle = self._spawn_with_retries(task, nodus, nodus_errors)
        except NodusAdapterError as exc:
            completed_at = datetime.now(timezone.utc)
            return _make_failed_result(task, exc.to_safe_dict(), mode=self._config.execution_mode)

        if handle is None:
            # All spawn retries exhausted
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="failed",
                error={
                    "code": NodusErrorCode.RETRIES_EXHAUSTED,
                    "message": "All pre-submission retry attempts were exhausted.",
                    "context": {"max_retries": task.max_retries},
                },
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )

        # Extract provider call ID from handle before blocking on get()
        provider_task_id: str | None = None
        try:
            raw_name = handle.name
            if isinstance(raw_name, str) and raw_name:
                provider_task_id = raw_name
        except Exception:  # noqa: BLE001
            pass  # name is a best-effort attribute; never block on it

        # Phase 2 — wait for result (post-submission, NOT retried)
        try:
            result_value = asyncio.get_event_loop().run_until_complete(
                handle.get(timeout=self._config.timeout_seconds)
            )
        except asyncio.TimeoutError as exc:
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
                provider_task_id=provider_task_id,
                submitted_at=submitted_at,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=(datetime.now(timezone.utc) - submitted_at).total_seconds(),
            )
        except nodus_errors.APITimeoutError as exc:
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
                provider_task_id=provider_task_id,
                submitted_at=submitted_at,
                completed_at=datetime.now(timezone.utc),
                duration_seconds=(datetime.now(timezone.utc) - submitted_at).total_seconds(),
            )
        except nodus_errors.FunctionCallFailed as exc:
            # Terminal: function ended without a result (deadline, OOM, crash, etc.)
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="failed",
                error={
                    "code": NodusErrorCode.REMOTE_EXECUTION_FAILED,
                    "message": "Remote function call ended without a result.",
                    "context": {
                        "sdk_exception_type": "FunctionCallFailed",
                        "reason": getattr(exc, "reason", None),
                    },
                },
                provider_task_id=provider_task_id,
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )
        except Exception as exc:  # noqa: BLE001 (broad catch at SDK boundary)
            safe_error = sanitize_sdk_error(exc)
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="failed",
                error=safe_error,
                provider_task_id=provider_task_id,
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )

        completed_at = datetime.now(timezone.utc)
        try:
            return self._normalize_function_result(
                task, result_value, provider_task_id, submitted_at, completed_at
            )
        except NodusAdapterError as exc:
            return _make_failed_result(task, exc.to_safe_dict(), mode=self._config.execution_mode)

    def _spawn_with_retries(
        self,
        task: NodusTask,
        nodus: Any,
        nodus_errors: Any,
    ) -> Any:
        """Resolve the Function and spawn the call, retrying pre-submission
        errors that the SDK documents as retryable (``NodusError.retryable=True``).

        Returns the ``FunctionCall`` handle on success, or ``None`` if all
        retry attempts are exhausted.

        **Never retries post-submission** — this method only covers the window
        before a handle is obtained.
        """
        # Configure auth for this thread via the default client
        client_kwargs: dict[str, Any] = {
            "api_key": self._config.api_key,
            "project": self._config.project,
        }
        if self._config.base_url:
            client_kwargs["api_url"] = self._config.base_url

        attempt = 0
        while attempt <= task.max_retries:
            try:
                # Build a fresh client and set it as the SDK default for this call
                client = nodus.Client(**client_kwargs)
                with client:
                    # Resolve function via verified public API:
                    # nodus.Function.from_name(app: str, name: str, project=None)
                    fn = nodus.Function.from_name(
                        self._config.app_name,
                        self._config.function_name,
                        self._config.project,
                    )

                    # Spawn: passes task kwargs directly to the remote function.
                    # The remote function signature must accept these kwargs.
                    spawn_kwargs: dict[str, Any] = {
                        "operation": task.operation,
                        "payload": task.payload,
                        "task_id": task.task_id,
                    }
                    if task.correlation_id:
                        spawn_kwargs["correlation_id"] = task.correlation_id

                    handle = asyncio.get_event_loop().run_until_complete(
                        fn.spawn(**spawn_kwargs)
                    )
                    return handle  # handle obtained — submission complete

            except nodus_errors.NodusError as exc:
                if not getattr(exc, "retryable", False) or attempt >= task.max_retries:
                    safe = sanitize_sdk_error(exc)
                    raise NodusExecutionError(
                        code=safe["code"],
                        message=safe["message"],
                        context=safe["context"],
                    ) from exc
                logger.warning(
                    "Nodus pre-submission transient error (attempt %d/%d): type=%s",
                    attempt + 1,
                    task.max_retries + 1,
                    type(exc).__name__,
                )
                time.sleep(_RETRY_DELAY_SECONDS)
                attempt += 1
                continue

            except Exception as exc:  # noqa: BLE001
                # Non-SDK errors during spawn are not retried
                safe = sanitize_sdk_error(exc)
                raise NodusExecutionError(
                    code=safe["code"],
                    message=safe["message"],
                    context=safe["context"],
                ) from exc

        return None  # exhausted — caller builds the failed result

    def _normalize_function_result(
        self,
        task: NodusTask,
        result_value: Any,
        provider_task_id: str | None,
        submitted_at: datetime,
        completed_at: datetime,
    ) -> NodusResult:
        """Normalize the value returned by ``handle.get()`` into a NodusResult.

        ``handle.get()`` returns the function's *return value* directly — not a
        status envelope.  A successful dict return is treated as ``result_data``.
        A non-dict return is a result-type error.

        The provider FunctionCall ID comes from ``handle.name``, captured
        before blocking on ``get()``.
        """
        duration = (completed_at - submitted_at).total_seconds()

        if not isinstance(result_value, dict):
            raise NodusResultError(
                code=NodusErrorCode.UNEXPECTED_RESULT_TYPE,
                message="Remote function return value is not a dict.",
                context={"result_type": type(result_value).__name__},
            )

        return NodusResult(
            task_id=task.task_id,
            execution_mode=ExecutionMode.REMOTE.value,
            status="succeeded",
            result_data=result_value,
            provider_task_id=provider_task_id,
            submitted_at=submitted_at,
            completed_at=completed_at,
            duration_seconds=duration,
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
