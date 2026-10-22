# Task List: CLARITY Issue #12 — Integrate Nodus Compute SDK and Verify Remote Execution

## Context & Role Boundary
- **Issue**: #12 — Integrate Nodus Compute SDK and Verify Remote Execution
- **Branch**: `12-integrate-nodus-compute-sdk-and-verify-remote-execution`
- **Goal**: Implement production-grade Nodus Compute SDK integration, dedicated adapter, execution modes (`nodus_remote`, `local`, `mock`), contracts, smoke test, and documentation according to the 12 review requirements.

---

### Phase 1: SDK Verification & Configuration (`config.py`)
- [ ] Verify `nodus-compute==1.0.27` against official PyPI wheel metadata (`win_amd64`, `manylinux`), confirm exact class names and method signatures.
- [ ] Implement `backend/app/adapters/nodus/config.py`:
  - `NodusConfig`:
    - `execution_mode`: strictly required in production (`nodus_remote`, `local`, `mock`). Fails if unconfigured or invalid.
    - `api_key`: `NODUS_API_KEY` (secret, never printed or logged).
    - `project`: `NODUS_PROJECT` (default: `"default"`).
    - `api_url`: `NODUS_API_URL` (default: `"https://api.nodus-compute.ai"`).
    - `app_name`: `NODUS_APP_NAME` (default: `"clarity-eval"`).
    - `function_name`: `NODUS_FUNCTION_NAME` (default: `"evaluate"`).
    - `timeout_seconds`: `NODUS_TIMEOUT_SECONDS` (default: `30.0`).
    - `max_retries`: `NODUS_MAX_RETRIES` (default: `2`).
  - Ensure zero credential requirements upon import (strictly lazy).

### Phase 2: Contracts & Error Definitions (`contracts.py`, `errors.py`)
- [ ] Implement `backend/app/adapters/nodus/contracts.py`:
  - `ExecutionMode` enum (`nodus_remote`, `local`, `mock`).
  - `NodusTask`: `task_id`, `operation`, `payload`, `correlation_id`, `timeout_seconds`, `max_retries`, `metadata`.
  - JSON payload validator strictly rejecting non-serializable objects (callables, handles, ground truth secrets).
  - `NodusResult`: `task_id`, `execution_mode`, `status`, `result_data`, `error`, `provider_task_id`, `submitted_at`, `completed_at`, `duration_seconds`, `provider_metadata`.
  - Enforce rule: never fabricate provider metadata or provider task IDs.
- [ ] Implement `backend/app/adapters/nodus/errors.py`:
  - Structured error codes: `MISSING_CREDENTIALS`, `AUTHENTICATION_FAILED`, `INITIALIZATION_FAILED`, `INVALID_PAYLOAD`, `SUBMISSION_FAILED`, `REMOTE_TASK_FAILED`, `TIMEOUT`, `SERIALIZATION_ERROR`, `UNSUPPORTED_CAPABILITY`, `RETRY_EXHAUSTED`, `PROVIDER_UNAVAILABLE`, `CONFIG_ERROR`.
  - Secret sanitization filter stripping API keys (`nodus_sk_...`), authorization headers, and simulator internals.

### Phase 3: Dedicated Execution Adapter (`adapter.py`, `__init__.py`)
- [ ] Implement `backend/app/adapters/nodus/adapter.py`:
  - `NodusExecutionAdapter`:
    - Focused single-task `execute(task: NodusTask) -> NodusResult` interface.
    - Validates task payload.
    - Dispatches to `_execute_remote()`, `_execute_mock()`, or `_execute_local()`.
    - Enforces non-silent fallback: remote failures or missing credentials must never produce mock or local results.
  - Remote Driver (`_execute_remote`):
    - Lazy `import nodus`.
    - Validates `NODUS_API_KEY`; raises sanitized `MISSING_CREDENTIALS` if missing.
    - Resolves persistent function via `nodus.Function.from_name(app, name, project=project)` or ephemeral `app.run()` context.
    - Spawns call, captures `provider_task_id = call.name()`.
    - Awaits result with timeout: `call.get(timeout=task.timeout_seconds)`.
    - Handles exceptions: `Unauthorized`, `InsufficientCredits`, `RemoteError`, `APITimeoutError`, `APIConnectionError`.
    - Retries only verified retryable errors (transient connection failures before acceptance), bounded by `task.max_retries`.
  - Mock Driver (`_execute_mock`):
    - Deterministic responses for tests.
    - Explicit `execution_mode="mock"`.
  - Local Driver (`_execute_local`):
    - In-process execution for local development.
    - Explicit `execution_mode="local"`.
- [ ] Export public classes in `backend/app/adapters/nodus/__init__.py`.

### Phase 4: Standalone Remote Smoke Test CLI (`nodus_smoke_test.py`)
- [ ] Implement `backend/app/nodus_smoke_test.py`:
  - CLI runner callable via `python -m app.nodus_smoke_test`.
  - Enforces `execution_mode == "nodus_remote"`; refuses `mock` or `local` modes.
  - Validates configuration without leaking credentials.
  - Submits deterministic smoke task (`operation="smoke_test"`, payload `{"a": 21, "b": 2}`).
  - Validates computed result (`sum == 23`, `product == 42`).
  - Reports genuine provider task ID, duration, and metadata (no fabricated values).
  - Exits 0 on genuine remote success; exits non-zero on failure.

### Phase 5: Automated Tests Without Credentials (`tests/test_nodus_adapter.py`)
- [ ] Implement `backend/tests/test_nodus_adapter.py`:
  - Application and adapter import cleanly without credentials or SDK.
  - Rejection of unconfigured execution mode.
  - JSON payload validation and rejection of invalid objects.
  - Explicit mode tagging (`mock`, `local`, `nodus_remote`).
  - Prevention of silent fallback (remote failure produces structured error, never mock).
  - Mocked SDK tests:
    - Remote success with real provider task ID and timing metadata.
    - Authentication failure mapping (`nodus.errors.Unauthorized`).
    - Insufficient credits mapping (`nodus.errors.InsufficientCredits`).
    - Timeout handling (`nodus.errors.APITimeoutError`).
    - Remote exception mapping (`nodus.errors.RemoteError`).
    - Bounded retry behavior on transient connection failures.
  - Smoke test CLI tests (rejection of mock mode, failure on missing credentials, success on remote mock).

### Phase 6: Documentation, Dependencies & Final Verification
- [ ] Add `nodus-compute==1.0.27` to `backend/requirements.txt`.
- [ ] Update `.env.example` with safe placeholder variables.
- [ ] Create `docs/nodus-compute.md` covering architecture, configuration, smoke testing, and troubleshooting.
- [ ] Inspect and update `docs/decisions.md` D-002 status accurately.
- [ ] Run focused tests: `python -m pytest backend/tests/test_nodus_adapter.py -v`.
- [ ] Run complete backend test suite: `python -m pytest backend/ -q`.
- [ ] Verify smoke CLI failure without credentials: `python -m app.nodus_smoke_test`.
- [ ] Verify git diff for any unredacted secrets or stray files.
