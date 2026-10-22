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
  Because ``nodus.api._client._default`` is process-global, ``_nodus_client_context``
  acquires a process-global mutex (``_NODUS_CLIENT_LOCK``) across the entire
  lookup/spawn/get lifecycle to prevent concurrent tasks or threads from
  clobbering or prematurely closing another execution's client transport.
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
import concurrent.futures
import contextlib
import inspect
import logging
import threading
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


async def _await_if_awaitable(target: Any) -> Any:
    """Await target if it is awaitable, else return as-is."""
    if inspect.isawaitable(target):
        return await target
    return target


async def _await_target(awaitable: Any) -> Any:
    """Helper coroutine to await an awaitable."""
    return await awaitable


def _run_coroutine_sync(awaitable: Any) -> Any:
    """Safely execute an awaitable from synchronous code.

    Handles three execution environments:
    1. A running event loop already exists in the current thread (e.g. inside
       an async test or async application loop):
       offloads execution to a dedicated single-thread worker running ``asyncio.run()``.
       Prevents ``RuntimeError: This event loop is already running``.
    2. A thread without an active event loop (e.g. background worker threads):
       runs via ``asyncio.run()``.
    3. Normal synchronous context:
       runs via ``asyncio.run()``.
    """
    if not inspect.isawaitable(awaitable):
        return awaitable

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, _await_target(awaitable)).result()
    else:
        return asyncio.run(_await_target(awaitable))


# Process-global mutex to serialize default client configuration and usage.
# Pinned nodus SDK (1.0.27) uses a single process-global _default Client in nodus.api._client.
# Holding this lock across the entire lookup/spawn/get lifecycle guarantees that concurrent
# executions cannot clobber or close each other's active client transport.
_NODUS_CLIENT_LOCK = threading.Lock()


class _nodus_client_context:
    """Context manager configuring the process-wide default client for Nodus SDK.

    Supports both synchronous (``with``) and asynchronous (``async with``) usage.
    Acquires the process-global execution lock (``_NODUS_CLIENT_LOCK``) to serialize
    the default-client swap and the entire lookup/spawn/get lifecycle across concurrent
    tasks and threads, preventing race conditions on the process-wide default client.
    """

    def __init__(self, nodus: Any, client_kwargs: dict[str, Any]) -> None:
        self.nodus = nodus
        self.client_kwargs = client_kwargs
        self.client: Any = None
        self.prev_client: Any = None

    def __enter__(self) -> Any:
        _NODUS_CLIENT_LOCK.acquire(blocking=True)
        return self._setup()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            self._teardown()
        finally:
            _NODUS_CLIENT_LOCK.release()

    async def __aenter__(self) -> Any:
        while not _NODUS_CLIENT_LOCK.acquire(blocking=False):
            await asyncio.sleep(0.01)
        return self._setup()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            self._teardown()
        finally:
            _NODUS_CLIENT_LOCK.release()

    def _setup(self) -> Any:
        if hasattr(self.nodus, "Client"):
            try:
                self.client = self.nodus.Client(**self.client_kwargs)
            except Exception:
                self.client = None

        if hasattr(self.nodus, "api") and hasattr(self.nodus.api, "default_client"):
            try:
                self.prev_client = self.nodus.api.default_client()
            except Exception:
                self.prev_client = None

        if self.client is not None and hasattr(self.nodus, "api") and hasattr(self.nodus.api, "set_default_client"):
            try:
                self.nodus.api.set_default_client(self.client)
            except Exception:
                pass

        return self.client

    def _teardown(self) -> None:
        if hasattr(self.nodus, "api") and hasattr(self.nodus.api, "set_default_client"):
            try:
                self.nodus.api.set_default_client(self.prev_client)
            except Exception:
                pass
        if self.client is not None and hasattr(self.client, "close"):
            try:
                self.client.close()
            except Exception:
                pass


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

    @property
    def config(self) -> NodusConfig:
        """The validated configuration this adapter was constructed with."""
        return self._config

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

    async def execute_async(self, task: NodusTask) -> NodusResult:
        """Asynchronously execute *task* according to the configured execution mode.

        Native coroutine alternative to ``execute()`` for callers running within
        an async context (e.g. FastAPI route handlers or asyncio tasks).

        Parameters
        ----------
        task:
            Validated ``NodusTask`` instance.

        Returns
        -------
        NodusResult
            Terminal execution result.
        """
        mode = self._config.execution_mode

        logger.info(
            "Nodus execute_async: task_id=%s operation=%s mode=%s",
            task.task_id,
            task.operation,
            mode.value,
        )

        if mode == ExecutionMode.REMOTE:
            return await self._remote_execute_async(task)
        if mode == ExecutionMode.LOCAL:
            return self._local_execute(task)
        if mode == ExecutionMode.MOCK:
            return self._mock_execute(task)

        raise NodusConfigurationError(
            NodusErrorCode.INVALID_MODE,
            message=f"Unhandled execution mode: {mode!r}",
        )

    def execute_many(
        self,
        tasks: list[NodusTask],
        max_concurrency: int = 1,
    ) -> list[NodusResult]:
        """Execute independent *tasks*, returning results in input order.

        Remote mode configures the SDK's process-global default client once for
        the batch and runs up to ``max_concurrency`` tasks concurrently. Each
        task resolves the Function, spawns its own call, and keeps its own
        provider ``FunctionCall`` handle, timeout, retry policy, and error
        mapping, so one failure never discards or mutates another task's result.

        The global client lock is held for the whole batch. That is required by
        ``nodus-compute==1.0.27`` (one process-wide default client) and is what
        makes concurrent execution safe: the client cannot be swapped or closed
        while any batch task is in flight.

        Mock/local modes run sequentially and never contact the platform.

        Raises ``NodusConfigurationError`` for invalid arguments. Individual task
        failures are returned as terminal ``NodusResult`` objects (never raised).
        """
        if isinstance(tasks, (str, bytes)) or not isinstance(tasks, list):
            raise NodusConfigurationError(
                NodusErrorCode.CONFIGURATION_ERROR,
                message="execute_many requires a list of NodusTask instances.",
            )
        limit = _validate_concurrency(max_concurrency)
        for task in tasks:
            if not isinstance(task, NodusTask):
                raise NodusConfigurationError(
                    NodusErrorCode.CONFIGURATION_ERROR,
                    message="execute_many requires a list of NodusTask instances.",
                )
        if not tasks:
            return []

        mode = self._config.execution_mode
        if mode == ExecutionMode.REMOTE:
            return self._remote_batch_execute(tasks, limit)
        # Mock/local drivers are deterministic in-process work; running them
        # concurrently would only add scheduling noise.
        return [self.execute(task) for task in tasks]

    def _remote_batch_execute(
        self, tasks: list[NodusTask], max_concurrency: int
    ) -> list[NodusResult]:
        """Execute remote tasks under one client scope with bounded concurrency."""
        nodus, nodus_errors = self._import_sdk()
        results: list[NodusResult | None] = [None] * len(tasks)
        with _nodus_client_context(nodus, self._client_kwargs()):
            if max_concurrency == 1 or len(tasks) == 1:
                for index, task in enumerate(tasks):
                    results[index] = self._run_remote_task_sync(task, nodus, nodus_errors)
                return [
                    result
                    if result is not None
                    else _make_failed_result(
                        tasks[index],
                        {
                            "code": NodusErrorCode.SDK_ERROR,
                            "message": "No result was produced for this task.",
                            "context": {},
                        },
                        mode=self._config.execution_mode,
                    )
                    for index, result in enumerate(results)
                ]

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(max_concurrency, len(tasks))
            ) as pool:
                futures = {
                    pool.submit(self._run_remote_task_sync, task, nodus, nodus_errors): index
                    for index, task in enumerate(tasks)
                }
                for future in concurrent.futures.as_completed(futures):
                    results[futures[future]] = future.result()
        return [
            result
            if result is not None
            else _make_failed_result(
                tasks[index],
                {
                    "code": NodusErrorCode.SDK_ERROR,
                    "message": "No result was produced for this task.",
                    "context": {},
                },
                mode=self._config.execution_mode,
            )
            for index, result in enumerate(results)
        ]

    def _run_remote_task_sync(
        self, task: NodusTask, nodus: Any, nodus_errors: Any
    ) -> NodusResult:
        """Run one task inside a batch worker thread.

        The caller already owns the client scope, so this never acquires the
        process-global lock (doing so from every thread would serialize the batch
        and could deadlock against the batch owner).
        """
        try:
            return _run_coroutine_sync(
                self._execute_remote_in_scope(
                    task, nodus, nodus_errors, cancel_on_timeout=True
                )
            )
        except NodusAdapterError as exc:
            return _make_failed_result(task, exc.to_safe_dict(), mode=self._config.execution_mode)
        except Exception as exc:  # noqa: BLE001 - one task must not crash the batch
            return _make_failed_result(
                task, sanitize_sdk_error(exc), mode=self._config.execution_mode
            )

    # ------------------------------------------------------------------
    # Remote execution
    # ------------------------------------------------------------------

    def _remote_execute(self, task: NodusTask) -> NodusResult:
        """Synchronously execute *task* on the real Nodus platform via the SDK.

        Safely executes across environments:
        - Within a running event loop (offloaded to dedicated thread)
        - In threads without an active event loop
        - In standard synchronous execution
        """
        return _run_coroutine_sync(self._remote_execute_async(task))

    async def _remote_execute_async(self, task: NodusTask) -> NodusResult:
        """Execute *task* on the real Nodus platform via the SDK asynchronously.

        Phase 1 — pre-submission (spawn): retried on SDK-documented
        ``retryable=True`` errors (TooManyRequests, Unavailable).

        Phase 2 — post-submission (get): never retried. The task is already
        submitted; retrying ``spawn`` would create duplicate executions.

        Lazy-imports ``nodus`` so that the module can be imported without
        the SDK installed.
        """
        nodus, nodus_errors = self._import_sdk()
        async with _nodus_client_context(nodus, self._client_kwargs()):
            return await self._execute_remote_in_scope(
                task, nodus, nodus_errors, cancel_on_timeout=False
            )

    def _import_sdk(self) -> tuple[Any, Any]:
        """Lazy-import the SDK, surfacing a missing package as configuration error."""
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
        return nodus, nodus_errors

    def _client_kwargs(self) -> dict[str, Any]:
        client_kwargs: dict[str, Any] = {
            "api_key": self._config.api_key,
            "project": self._config.project,
        }
        if self._config.base_url:
            client_kwargs["api_url"] = self._config.base_url
        return client_kwargs

    async def _execute_remote_in_scope(
        self,
        task: NodusTask,
        nodus: Any,
        nodus_errors: Any,
        *,
        cancel_on_timeout: bool,
    ) -> NodusResult:
        """Run one remote task while the caller already owns the client scope.

        The process-global default client must be configured by the caller (via
        ``_nodus_client_context``) and must stay stable for the whole call.
        """
        submitted_at = datetime.now(timezone.utc)

        # Phase 1 — resolve function and spawn (pre-submission, retryable)
        try:
            handle = await self._spawn_with_retries_async(task, nodus, nodus_errors)
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

        # Extract provider call ID from handle before waiting on get()
        provider_task_id: str | None = None
        try:
            raw_name = handle.name
            if isinstance(raw_name, str) and raw_name:
                provider_task_id = raw_name
        except Exception:  # noqa: BLE001
            pass  # name is a best-effort attribute; never block on it

        # Build tuple of timeout exceptions
        timeout_exceptions: list[type[BaseException]] = [asyncio.TimeoutError, TimeoutError]
        if hasattr(nodus_errors, "APITimeoutError"):
            timeout_exceptions.append(nodus_errors.APITimeoutError)
        timeout_types = tuple(set(timeout_exceptions))

        # Phase 2 — wait for result (post-submission, NOT retried)
        try:
            raw_get = handle.get(timeout=self._config.timeout_seconds)
            result_value = await _await_if_awaitable(raw_get)
        except timeout_types:
            cancel_requested = False
            if cancel_on_timeout:
                cancel_requested = await _request_handle_cancel(handle)
            completed_at = datetime.now(timezone.utc)
            return NodusResult(
                task_id=task.task_id,
                execution_mode=ExecutionMode.REMOTE.value,
                status="timed_out",
                error={
                    "code": NodusErrorCode.TIMEOUT,
                    "message": "Task execution exceeded the configured timeout.",
                    "context": {
                        "timeout_seconds": self._config.timeout_seconds,
                        # A cancel request is not confirmation that the remote
                        # task stopped; it is recorded separately.
                        "cancellation_requested": cancel_requested,
                        "cancellation_confirmed": False,
                    },
                },
                provider_task_id=provider_task_id,
                submitted_at=submitted_at,
                completed_at=completed_at,
                duration_seconds=(completed_at - submitted_at).total_seconds(),
            )
        except getattr(nodus_errors, "FunctionCallFailed", ()) as exc:
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
        """Resolve the Function and spawn the call (synchronous wrapper)."""
        return _run_coroutine_sync(self._spawn_with_retries_async(task, nodus, nodus_errors))

    async def _spawn_with_retries_async(
        self,
        task: NodusTask,
        nodus: Any,
        nodus_errors: Any,
    ) -> Any:
        """Resolve the Function and spawn the call asynchronously, retrying
        pre-submission errors that the SDK documents as retryable
        (``NodusError.retryable=True``).

        Returns the ``FunctionCall`` handle on success, or ``None`` if all
        retry attempts are exhausted.

        **Never retries post-submission** — this method only covers the window
        before a handle is obtained.
        """
        attempt = 0
        while attempt <= task.max_retries:
            try:
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

                raw_spawn = fn.spawn(**spawn_kwargs)
                handle = await _await_if_awaitable(raw_spawn)
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
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
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

_MAX_BATCH_CONCURRENCY = 32


def _validate_concurrency(max_concurrency: Any) -> int:
    """Validate a batch concurrency limit; bounds unbounded fan-out."""
    if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int):
        raise NodusConfigurationError(
            NodusErrorCode.CONFIGURATION_ERROR,
            message="max_concurrency must be an integer.",
        )
    if max_concurrency < 1 or max_concurrency > _MAX_BATCH_CONCURRENCY:
        raise NodusConfigurationError(
            NodusErrorCode.CONFIGURATION_ERROR,
            message=(
                f"max_concurrency must be between 1 and {_MAX_BATCH_CONCURRENCY}, "
                f"got {max_concurrency}."
            ),
        )
    return max_concurrency


async def _request_handle_cancel(handle: Any) -> bool:
    """Best-effort cancellation of a submitted call.

    Returns True only when a cancel request was issued without raising. It does
    not confirm that the remote task stopped; callers must not report it as
    confirmed termination.
    """
    cancel = getattr(handle, "cancel", None)
    if not callable(cancel):
        return False
    try:
        outcome = cancel()
        if inspect.isawaitable(outcome):
            await outcome
        return True
    except Exception:  # noqa: BLE001 - cleanup must never mask the timeout
        return False


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
