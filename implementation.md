# Implementation Plan: CLARITY Issue #12 — Integrate Nodus Compute SDK and Verify Remote Execution

## 1. Executive Summary

- **Branch**: `12-integrate-nodus-compute-sdk-and-verify-remote-execution`
- **Base Branch**: `main`
- **Issue**: #12 — Integrate Nodus Compute SDK and Verify Remote Execution
- **Downstream Dependency**: Issue #13 (Parallel Hypothesis & Experiment Evaluation)
- **Primary Objective**: Integrate the official `nodus-compute` Python SDK into CLARITY, establishing a dedicated, robust, and independently testable execution adapter boundary with explicit execution modes (`nodus_remote`, `local`, `mock`), rigorous error sanitization, bounded retries, and a standalone remote smoke test.

---

## 2. Nodus Compute SDK Primary-Source Verification

Verified directly against PyPI package metadata and the `nodus_compute-1.0.27-py3-none-win_amd64.whl` distribution archive:

1. **Package Identity**:
   - **Package**: `nodus-compute`
   - **Verified Version**: `1.0.27`
   - **Python Compatibility**: `>=3.10` (CLARITY runs Python 3.11)
   - **Root Namespace**: `import nodus`
2. **Verified Signatures & Exports**:
   - `nodus.api.Client(context=None, api_key=None, api_url=None, org=None, project=None, transport=None)`
   - `nodus.App(name: str)` with `.function(...)`, `.deploy()`, `.run()`
   - `nodus.functions._Function.from_name(app: str, name: str, project: str | None = None)`
   - `nodus.functions._Function.remote(*args, **kwargs)`
   - `nodus.functions._Function.spawn(*args, **kwargs) -> _FunctionCall`
   - `nodus.functions._FunctionCall.get(timeout: float | None = None)`
   - `nodus.functions._FunctionCall.name() -> str` (provider task ID)
   - `nodus.functions._FunctionCall.status() -> str | None`
   - `nodus.functions._FunctionCall.cancel()`
3. **Verified Error Hierarchy**:
   - `nodus.errors.NodusError` (base class with `message`, `reason`, `fix`, `docs`, `request_id`, `status`, `idempotency_key`, `retryable`)
   - `nodus.errors.APIConnectionError`, `nodus.errors.APITimeoutError`
   - `nodus.errors.Unauthorized`, `nodus.errors.Forbidden`
   - `nodus.errors.InsufficientCredits`, `nodus.errors.BudgetExceeded`, `nodus.errors.QuotaExceeded`
   - `nodus.errors.NotFound`, `nodus.errors.FunctionNotFound`
   - `nodus.errors.FunctionCallFailed`, `nodus.errors.RemoteError`
4. **Internal SDK Retry Behavior (ADR-030)**:
   - The SDK's underlying `Transport` automatically retries idempotent HTTP methods (`GET`, `HEAD`, `PUT`, `PATCH`, `DELETE`) and creates with `Idempotency-Key` headers up to 5 minutes against rate limits and 503s.
   - Non-idempotent or non-transient calls (auth failure, invalid payloads) are never retried by the SDK.
   - CLARITY's adapter respects this: application-level retries are only applied to transient submission connection errors before task acceptance, bounded by `task.max_retries`.

---

## 3. Remote Execution Architecture & Lifecycle

1. **Function Execution Mechanism**:
   - **Worker Registration**: The Nodus worker function is exposed under a named application (default: `app="clarity-eval"`, function `name="evaluate"`).
   - **Persistent Lookup (Production)**: In deployed environments, the adapter retrieves the persistent worker via `nodus.Function.from_name(app, name, project=project)`.
   - **Ephemeral Session (Development / Smoke Test)**: For ad-hoc execution where a persistent function is not pre-deployed, the adapter can execute within an ephemeral `with app.run():` session.
2. **Payload Delivery & Result Retrieval**:
   - The worker function signature is `def evaluate(task_payload: dict[str, Any]) -> dict[str, Any]: ...`.
   - Calling `call = fn.spawn(task.payload)` submits the task asynchronously.
   - `provider_task_id = call.name()` is captured immediately from Nodus.
   - The result is retrieved with timeout enforcement: `result_data = call.get(timeout=task.timeout_seconds)`.
   - If execution fails remotely, `nodus.errors.RemoteError` or `nodus.errors.FunctionCallFailed` is caught and mapped to a structured application error.

---

## 4. Execution Mode Enforcement & Configuration

1. **Explicit Mode Requirement**:
   - Modes: `nodus_remote`, `local`, `mock`.
   - In production or standard runtime, `NODUS_EXECUTION_MODE` must be explicitly configured.
   - If `NODUS_EXECUTION_MODE` is unconfigured, the adapter refuses to run and raises `NodusConfigurationError` requiring explicit configuration. Silent fallback to mock or local mode is strictly prohibited.
   - Automated tests explicitly supply `execution_mode="mock"`.
2. **Lazy Initialization**:
   - Neither `nodus` nor credentials (`NODUS_API_KEY`) are required to import `backend.app` or `backend.app.adapters.nodus`.
   - `nodus` is imported lazily inside `_execute_remote()`.
   - If `execution_mode == "nodus_remote"` is executed without `NODUS_API_KEY`, it fails immediately with a sanitized error (`MISSING_CREDENTIALS`).

---

## 5. Contract Specification

1. **`NodusTask`**:
   - `task_id: str`: Unique, stable identifier.
   - `operation: str`: Name of computation (e.g. `smoke_test`, `evaluate_candidate`, `fit_model`).
   - `payload: dict[str, Any]`: Validated JSON-compatible dictionary. Non-serializable objects (functions, file descriptors, live database sessions, secrets) are rejected with `INVALID_PAYLOAD`.
   - `correlation_id: str | None`: Optional trace identifier.
   - `timeout_seconds: float`: Execution timeout (default: 30.0s).
   - `max_retries: int`: Retry limit (default: 2).
   - `metadata: dict[str, Any]`: Optional context.
2. **`NodusResult`**:
   - `task_id: str`
   - `execution_mode: str` (`nodus_remote`, `local`, `mock`)
   - `status: str` (`succeeded`, `failed`, `timed_out`, `cancelled`)
   - `result_data: dict[str, Any] | None`
   - `error: dict[str, Any] | None` (code, message, details - all sanitized)
   - `provider_task_id: str | None` (real provider task identifier, never fabricated)
   - `submitted_at: datetime | None`
   - `completed_at: datetime | None`
   - `duration_seconds: float | None`
   - `provider_metadata: dict[str, Any]` (real metadata from Nodus, empty if unavailable)

---

## 6. Adapter Implementation (`backend/app/adapters/nodus/`)

- `contracts.py`: `NodusTask`, `NodusResult`, `ExecutionMode`, `validate_json_payload`.
- `config.py`: `NodusConfig` with environment loading, mode validation, and secret hiding.
- `errors.py`: `NodusAdapterError`, structured error codes, and secret sanitization filter.
- `adapter.py`: `NodusExecutionAdapter`:
  - `execute(task: NodusTask) -> NodusResult`: Single-task execution boundary.
  - Driver dispatch: `_execute_remote`, `_execute_mock`, `_execute_local`.
  - Non-silent fallback enforcement.
  - Bounded retry logic for transient errors.
- `__init__.py`: Clean public interface.

---

## 7. Standalone Remote Smoke Test CLI (`backend/app/nodus_smoke_test.py`)

- Command: `python -m app.nodus_smoke_test`.
- Enforces `execution_mode == "nodus_remote"`; fails immediately if set to `mock` or `local`.
- Submits deterministic task: `operation="smoke_test"`, payload `{"a": 21, "b": 2}`.
- Verifies output: `{"sum": 23, "product": 42}`.
- Prints redacted execution summary (provider task ID, duration, status).
- Exits 0 on genuine remote success; exits non-zero on failure or missing credentials.

---

## 8. Verification Strategy

1. **Unit & Mocked SDK Tests (`backend/tests/test_nodus_adapter.py`)**:
   - Import safety without credentials.
   - Rejection of unconfigured execution mode.
   - Rejection of invalid / non-JSON payloads.
   - Explicit mode tagging (`mock`, `local`, `nodus_remote`).
   - Prevention of silent fallback from remote to mock/local.
   - Mocked SDK behavior:
     - Remote success with provider task ID and timing.
     - Auth failure (`nodus.errors.Unauthorized` -> `AUTHENTICATION_FAILED`).
     - Insufficient credits (`nodus.errors.InsufficientCredits` -> `INSUFFICIENT_CREDITS`).
     - Timeout handling (`nodus.errors.APITimeoutError` -> `TIMEOUT`).
     - Remote exception mapping (`nodus.errors.RemoteError` -> `REMOTE_TASK_FAILED`).
     - Bounded retry behavior on connection failure.
   - Smoke test CLI behavior: failure on missing credentials, rejection of mock mode.
2. **Full Backend Suite**: `python -m pytest backend/ -q`.
3. **Decision Record Update**: Inspect and update `docs/decisions.md` D-002 status accurately.
4. **Remote Verification Status**: Explicitly labeled as `pending credentials/credits` until genuine remote execution is performed.
