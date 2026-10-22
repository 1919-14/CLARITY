"""Standalone remote smoke test for the Nodus Compute integration.

Usage
-----
::

    python -m app.nodus_smoke_test

The smoke test:
1. Refuses to run in ``local`` or ``mock`` mode — only ``nodus_remote``
   is accepted.  This prevents a misconfigured CI environment from being
   silently passed off as a genuine remote verification.
2. Submits a deterministic task (``smoke_test`` operation with a known
   ``echo_value``) to the real Nodus platform.
3. Validates that the returned result contains the expected ``echo_value``.
4. Prints a structured summary and exits with code 0 on success or 1 on
   failure.

Environment variables required
-------------------------------
NODUS_EXECUTION_MODE=nodus_remote
NODUS_API_KEY=<your key>
NODUS_APP_NAME=<your app>
NODUS_FUNCTION_NAME=<your function>

Optional
--------
NODUS_TIMEOUT_SECONDS   Default 30.
NODUS_BASE_URL          Override platform URL.

Exit codes
----------
0   Smoke test passed.
1   Smoke test failed (result mismatch, remote error, or configuration error).
2   Mode guard triggered — execution mode is not ``nodus_remote``.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone

from app.adapters.nodus import (
    ExecutionMode,
    NodusAdapterError,
    NodusConfig,
    NodusConfigurationError,
    NodusExecutionAdapter,
    NodusTask,
    NodusSmokeTestError,
)

# Deterministic echo value the remote function must return unchanged
_ECHO_VALUE = "clarity-smoke-test-v1"

# Expected key in the result_data returned by the remote function
_RESULT_KEY = "echo_value"


def _print_json(label: str, data: object) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(data, indent=2, default=str))


def run_smoke_test() -> int:
    """Run the smoke test and return an exit code."""
    print("[nodus_smoke_test] Starting Nodus Compute remote smoke test…")
    print(f"[nodus_smoke_test] Timestamp: {datetime.now(timezone.utc).isoformat()}")

    # --- 1. Load and validate configuration ---------------------------------
    try:
        config = NodusConfig.from_environment()
    except NodusConfigurationError as exc:
        print(f"\n[FAIL] Configuration error: {exc.message}")
        return 1

    # --- 2. Mode guard: refuse local and mock --------------------------------
    if config.execution_mode != ExecutionMode.REMOTE:
        print(
            f"\n[ABORT] Smoke test requires NODUS_EXECUTION_MODE=nodus_remote, "
            f"got {config.execution_mode.value!r}.\n"
            "Set NODUS_EXECUTION_MODE=nodus_remote and provide NODUS_API_KEY."
        )
        return 2

    print(f"[nodus_smoke_test] Config: {config!r}")

    # --- 3. Build task -------------------------------------------------------
    task_id = f"smoke-{uuid.uuid4().hex[:12]}"
    task = NodusTask(
        task_id=task_id,
        operation="smoke_test",
        payload={_RESULT_KEY: _ECHO_VALUE},
        correlation_id="clarity-smoke-test",
        timeout_seconds=config.timeout_seconds,
        max_retries=0,  # smoke test: no retries — failure must be loud
        metadata={"purpose": "integration-smoke-test"},
    )

    _print_json("Task submitted", task.to_dict())

    # --- 4. Execute ----------------------------------------------------------
    adapter = NodusExecutionAdapter(config)
    try:
        result = adapter.execute(task)
    except NodusAdapterError as exc:
        print(f"\n[FAIL] Adapter error during execution: {exc.code} — {exc.message}")
        return 1

    _print_json("Result received", result.to_dict())

    # --- 5. Validate result --------------------------------------------------
    if not result.is_success:
        print(f"\n[FAIL] Task did not succeed. Status: {result.status!r}")
        if result.error:
            print(f"       Error: {result.error}")
        return 1

    if result.execution_mode != ExecutionMode.REMOTE.value:
        print(
            f"\n[FAIL] Result execution_mode is {result.execution_mode!r}, "
            "expected 'nodus_remote'. A local or mock result is not acceptable."
        )
        return 1

    result_data = result.result_data or {}
    returned_echo = result_data.get(_RESULT_KEY)

    if returned_echo != _ECHO_VALUE:
        print(
            f"\n[FAIL] Echo validation failed.\n"
            f"       Expected {_ECHO_VALUE!r}, got {returned_echo!r}."
        )
        return 1

    print(
        f"\n[PASS] Smoke test passed.\n"
        f"       provider_task_id={result.provider_task_id!r}\n"
        f"       duration_seconds={result.duration_seconds}\n"
        f"       echo_value verified: {returned_echo!r}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_smoke_test())
