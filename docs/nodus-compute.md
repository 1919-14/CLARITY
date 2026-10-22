# Nodus Compute Integration

This document describes how CLARITY integrates with Nodus Compute for remote
task execution, including installation, authentication, function deployment,
configuration, and how to run the smoke test.

---

## SDK Version

```
nodus-compute==1.0.27
```

The SDK is pinned in `backend/requirements.txt`. All adapter code uses only
the public, verified API surface documented in this file.

---

## Installation

```bash
pip install nodus-compute==1.0.27
```

The adapter lazy-imports `nodus` only inside the remote execution path.
Ordinary application startup does not require the SDK to be installed.

---

## Authentication & Client Lifecycle

The Nodus SDK authenticates using an API key passed to `nodus.Client`:

```python
client = nodus.Client(api_key="...", project="my-project")
```

In CLARITY the key is read exclusively from the `NODUS_API_KEY` environment
variable. It is never logged, serialized, or included in repr output.

The adapter manages the client lifecycle using a scoped context manager
(`_nodus_client_context`):
1. Instantiates `nodus.Client` with configured credentials and project.
2. Sets it as the SDK default via `nodus.api.set_default_client(client)` so
   that both `fn.spawn(...)` and `handle.get(...)` share the authenticated client.
3. Automatically restores any previous default client and closes the client
   transport via `client.close()` upon task completion or failure.

---

## Event Loop & Execution Architecture

Nodus SDK calls (`fn.spawn` and `handle.get`) are asynchronous coroutines.
The adapter supports both synchronous and asynchronous caller patterns:

### 1. Synchronous Interface: `adapter.execute(task)`
Safe to call from any context:
- **Inside a running event loop** (e.g. FastAPI routes, Jupyter, async tests):
  automatically offloaded to a dedicated single-worker thread running `asyncio.run()`,
  preventing `RuntimeError: This event loop is already running`.
- **In threads without an event loop** (e.g. background worker threads):
  automatically executed via a freshly scoped `asyncio.run()`, preventing
  `RuntimeError: There is no current event loop in thread`.
- **In standard synchronous code**: executes cleanly via `asyncio.run()`.

### 2. Asynchronous Interface: `await adapter.execute_async(task)`
A native coroutine alternative for callers already operating within an async context:
```python
result = await adapter.execute_async(task)
```

### 3. Timeout Handling
Execution timeouts are bounded by `NODUS_TIMEOUT_SECONDS`. The adapter catches
all timeout variants — standard Python `TimeoutError`, `asyncio.TimeoutError`, and
`nodus.errors.APITimeoutError` — returning a terminal `NodusResult(status="timed_out")`.

---

## Function Deployment and Registration

A Nodus Function is a Python callable deployed to an App in the Nodus platform.
CLARITY does not deploy Functions itself — the Function must already be
registered and deployed before `nodus_remote` mode can be used.

### Concepts

| Term | Meaning |
|---|---|
| **App** | A named deployment unit in the Nodus platform. Set via `NODUS_APP_NAME`. |
| **Function** | A named callable within an App. Set via `NODUS_FUNCTION_NAME`. |
| **FunctionCall** | One invocation of a Function; has a unique `name` (provider call ID). |
| **Project** | Optional Nodus project scope. Set via `NODUS_PROJECT` if required. |

### Registering a Function

A deployed Nodus Function for CLARITY must:

1. Accept the following keyword arguments:
   ```python
   def clarity_fn(*, operation: str, payload: dict, task_id: str,
                  correlation_id: str | None = None): ...
   ```
2. Return a JSON-serializable **`dict`**. The adapter treats the return value
   directly as `result_data` in the `NodusResult`.

> **Important:** The adapter does not wrap or unwrap any status envelope.
> The return value of `handle.get()` is used verbatim as the function result.
> Returning `{"status": "succeeded", "result": {...}}` would expose those
> keys in `result_data`, not map them to the result status.

### Looking Up a Function

The adapter resolves the function using the verified public API:

```python
fn = nodus.Function.from_name(app, name, project=None)
# e.g.
fn = nodus.Function.from_name("clarity-app", "clarity-fn")
```

`nodus.Function.from_name` takes positional string arguments:
`app` first, then `name`, then an optional `project`. It does **not** accept
an `App` object or a `Client` object.

### Submitting and Retrieving a Call

```python
handle = await fn.spawn(operation=..., payload=..., task_id=...)
result  = await handle.get(timeout=30.0)   # returns the function return value
call_id = handle.name                       # provider FunctionCall identifier
```

`handle.get()` blocks until the function completes and returns its **return
value directly** (not a status envelope). Raising an exception in the remote
function causes `handle.get()` to raise `nodus.errors.FunctionCallFailed`.

---

## Execution Modes

`NODUS_EXECUTION_MODE` must be set explicitly. There is no default — omitting
it raises `NodusConfigurationError` before any task is submitted.

| Mode | Value | Description |
|---|---|---|
| **Remote** | `nodus_remote` | Executes on the real Nodus platform. Requires `NODUS_API_KEY`. |
| **Local** | `local` | Executes via a registered local handler. For development. |
| **Mock** | `mock` | Returns a deterministic stub. Never contacts the platform. Tests MUST use this. |

### Remote Mode

```bash
NODUS_EXECUTION_MODE=nodus_remote
NODUS_API_KEY=your-api-key
NODUS_APP_NAME=clarity-app
NODUS_FUNCTION_NAME=clarity-fn
# Optional:
NODUS_PROJECT=my-project
NODUS_TIMEOUT_SECONDS=30
NODUS_MAX_RETRIES=2
NODUS_BASE_URL=          # leave blank for default platform URL
```

### Mock Mode (tests and local development)

```bash
NODUS_EXECUTION_MODE=mock
NODUS_APP_NAME=clarity-app      # required by config validation even in mock mode
NODUS_FUNCTION_NAME=clarity-fn  # required by config validation even in mock mode
```

No `NODUS_API_KEY` is needed in mock mode. The adapter returns a deterministic
dict with `{"mock": true, "operation": ..., "echo_payload": ...}` without
contacting the platform.

---

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `NODUS_EXECUTION_MODE` | **Yes** | — | One of `nodus_remote`, `local`, `mock` |
| `NODUS_APP_NAME` | **Yes** | — | Registered Nodus App name |
| `NODUS_FUNCTION_NAME` | **Yes** | — | Registered Function within the App |
| `NODUS_API_KEY` | Remote only | — | Nodus API key (never logged) |
| `NODUS_PROJECT` | No | `None` | Nodus project scope |
| `NODUS_TIMEOUT_SECONDS` | No | `30` | Per-task execution timeout |
| `NODUS_MAX_RETRIES` | No | `2` | Max pre-submission retries |
| `NODUS_BASE_URL` | No | SDK default | Override platform base URL |

---

## Retry Policy

Retries apply **only to pre-submission errors** (errors raised during
`Function.from_name` or `fn.spawn`). Once a `FunctionCall` handle is obtained,
the task is already submitted on the platform and `handle.get()` is **never
retried** — doing so would re-spawn and create duplicate/billable executions.

The only retryable errors are SDK errors with `NodusError.retryable = True`:

| Error class | retryable |
|---|---|
| `TooManyRequests` (429) | `True` |
| `Unavailable` (503) | `True` |
| All others | `False` |

`FunctionCallFailed`, `APITimeoutError`, `NotFound`, `Unauthorized`, and
`Forbidden` are always terminal.

---

## Smoke Test

The smoke test verifies that a real remote execution path works end-to-end.
It refuses to run in `local` or `mock` mode — a mocked result is never
accepted as evidence of a successful remote call.

### Prerequisites

1. Deploy a Nodus Function that accepts `(operation, payload, task_id)` and
   returns `{"echo_value": payload["echo_value"]}` (i.e. echoes the payload).
2. Set all required environment variables (remote mode).

### Running

```bash
NODUS_EXECUTION_MODE=nodus_remote \
NODUS_API_KEY=your-key \
NODUS_APP_NAME=clarity-app \
NODUS_FUNCTION_NAME=clarity-fn \
python -m app.nodus_smoke_test
```

### Exit Codes

| Code | Meaning |
|---|---|
| `0` | Smoke test passed — remote echo verified |
| `1` | Configuration error or result validation failed |
| `2` | Mode guard triggered — execution mode is not `nodus_remote` |

---

## Adapter Module Reference

```
backend/app/adapters/nodus/
├── __init__.py       Public API surface
├── contracts.py      NodusTask, NodusResult, ExecutionMode, validate_json_payload
├── config.py         NodusConfig — loaded from env; api_key redacted from repr
├── errors.py         NodusErrorCode, sanitize_sdk_error, sanitize_result_error
└── adapter.py        NodusExecutionAdapter — remote / local / mock drivers

backend/app/nodus_smoke_test.py   Standalone remote smoke test
```

### Key Invariants

- **No default execution mode.** `NODUS_EXECUTION_MODE` must always be set.
- **Lazy SDK import.** `import nodus` only inside the remote driver; app
  imports work without the SDK installed.
- **Secrets never logged.** `api_key` is excluded from `repr`, logging, and
  all serialized outputs.
- **No post-submission retries.** Retries only cover the window before a handle
  is obtained. Post-submission failures are terminal.
- **No fabricated provider data.** `provider_task_id` is taken from
  `handle.name` only; it is `None` if the attribute is unavailable.
