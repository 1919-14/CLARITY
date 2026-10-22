"""Opt-in remote smoke test for parallel Nodus execution (Issue #13).

Usage
-----
::

    python -m app.nodus_parallel_smoke_test

This command is **not** part of the standard test suite or CI. It contacts the
real Nodus platform and may consume credits.

What it verifies
----------------
1. ``NODUS_EXECUTION_MODE=nodus_remote`` (a mocked/local adapter is refused).
2. ``NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel`` (batch execution requested).
3. Several independent, inexpensive ``smoke_test`` tasks with unique echo values
   are submitted through ``NodusExecutionAdapter.execute_many``.
4. Every response echoes the value that belongs to its own task, proving
   task/result correlation rather than just "some calls succeeded".
5. The batch reports genuine provider task IDs, measured durations, and the
   actual execution mode. Missing provider metadata is reported as unavailable.

Exit codes
----------
0   All submitted tasks succeeded and correlated with their own inputs.
1   A task failed, timed out, or returned a mismatched result.
2   Mode guard triggered or configuration/credentials are unusable.

Current status: NOT YET RUN against the live service (no Nodus credits/access).
Any run output must come from an actual execution; never fabricate it.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.adapters.nodus import (
    ExecutionMode,
    NodusAdapterError,
    NodusConfig,
    NodusConfigurationError,
    NodusExecutionAdapter,
    NodusTask,
)
from app.nodus_worker import ECHO_VALUE_KEY, SEQUENCE_KEY, SMOKE_TEST_OPERATION
from app.services.parallel_evaluation import ParallelEvaluationConfig, ParallelExecutionMode

_SMOKE_TASK_COUNT = 4
_MAX_BATCH_CONCURRENCY = 2


def _print_json(label: str, data: Any) -> None:
    print(f"\n=== {label} ===")
    print(json.dumps(data, indent=2, default=str))


def _parallel_config() -> ParallelEvaluationConfig:
    """Load the parallel mode explicitly; never default it."""
    return ParallelEvaluationConfig.from_environment()


def _build_tasks(config: NodusConfig, run_id: str) -> List[NodusTask]:
    tasks: List[NodusTask] = []
    for index in range(_SMOKE_TASK_COUNT):
        echo_value = f"{run_id}-task-{index}"
        tasks.append(
            NodusTask(
                task_id=f"parallel-smoke-{index}",
                operation=SMOKE_TEST_OPERATION,
                payload={ECHO_VALUE_KEY: echo_value, SEQUENCE_KEY: index},
                correlation_id=run_id,
                timeout_seconds=config.timeout_seconds,
                # A smoke test must fail loudly rather than silently retry.
                max_retries=0,
                metadata={"purpose": "parallel-remote-smoke-test", "index": index},
            )
        )
    return tasks


def run_smoke_test() -> int:
    """Run the smoke test and return an exit code."""
    print("[nodus_parallel_smoke_test] Starting parallel remote smoke test…")
    print(f"[nodus_parallel_smoke_test] Timestamp: {datetime.now(timezone.utc).isoformat()}")

    try:
        config = NodusConfig.from_environment()
    except NodusConfigurationError as exc:
        print(f"\n[FAIL] Configuration error: {exc.message}")
        return 1

    if config.execution_mode != ExecutionMode.REMOTE:
        print(
            f"\n[ABORT] Remote execution required, got {config.execution_mode.value!r}.\n"
            "Set NODUS_EXECUTION_MODE=nodus_remote and provide NODUS_API_KEY."
        )
        return 2

    try:
        parallel_config = _parallel_config()
    except Exception as exc:  # noqa: BLE001 - configuration boundary
        print(f"\n[FAIL] Parallel configuration error: {exc}")
        return 1

    if parallel_config.execution_mode != ParallelExecutionMode.NODUS_PARALLEL:
        print(
            f"\n[ABORT] Parallel execution required, got "
            f"{parallel_config.execution_mode.value!r}.\n"
            "Set NODUS_PARALLEL_EXECUTION_MODE=nodus_parallel."
        )
        return 2

    print(f"[nodus_parallel_smoke_test] Adapter config: {config!r}")
    print(f"[nodus_parallel_smoke_test] Parallel config: {parallel_config!r}")

    run_id = f"parallel-smoke-{uuid.uuid4().hex[:10]}"
    tasks = _build_tasks(config, run_id)
    _print_json(
        "Tasks submitted",
        [
            {
                "task_id": task.task_id,
                "operation": task.operation,
                "payload": task.payload,
                "correlation_id": task.correlation_id,
            }
            for task in tasks
        ],
    )

    adapter = NodusExecutionAdapter(config)
    try:
        results = adapter.execute_many(
            tasks, max_concurrency=min(_MAX_BATCH_CONCURRENCY, len(tasks))
        )
    except NodusAdapterError as exc:
        print(f"\n[FAIL] Adapter error: {exc.code} — {exc.message}")
        return 1

    _print_json("Results received", [result.to_dict() for result in results])

    failures: List[str] = []
    if len(results) != len(tasks):
        failures.append(
            f"expected {len(tasks)} results, received {len(results)}"
        )

    by_task = {result.task_id: result for result in results}
    observed_modes: Dict[str, int] = {}
    provider_ids = 0
    for task in tasks:
        result = by_task.get(task.task_id)
        if result is None:
            failures.append(f"{task.task_id}: missing result")
            continue
        observed_modes[result.execution_mode] = observed_modes.get(result.execution_mode, 0) + 1
        if result.provider_task_id:
            provider_ids += 1
        if result.execution_mode != ExecutionMode.REMOTE.value:
            failures.append(
                f"{task.task_id}: execution_mode={result.execution_mode!r} is not "
                "'nodus_remote'"
            )
        if result.status != "succeeded":
            failures.append(
                f"{task.task_id}: status={result.status!r} "
                f"error={(result.error or {}).get('code')!r}"
            )
            continue
        data = result.result_data or {}
        if data.get(ECHO_VALUE_KEY) != task.payload[ECHO_VALUE_KEY]:
            failures.append(
                f"{task.task_id}: echo mismatch "
                f"(expected {task.payload[ECHO_VALUE_KEY]!r}, "
                f"got {data.get(ECHO_VALUE_KEY)!r})"
            )
        if data.get(SEQUENCE_KEY) != task.payload[SEQUENCE_KEY]:
            failures.append(f"{task.task_id}: sequence mismatch")

    measured = [
        result.duration_seconds
        for result in results
        if isinstance(result.duration_seconds, (int, float))
    ]
    summary = {
        "run_id": run_id,
        "requested_execution_mode": parallel_config.execution_mode.value,
        "actual_execution_modes": observed_modes,
        "max_concurrency": min(_MAX_BATCH_CONCURRENCY, len(tasks)),
        "requested_tasks": len(tasks),
        "received_tasks": len(results),
        "succeeded_tasks": sum(1 for result in results if result.status == "succeeded"),
        "failed_tasks": sum(1 for result in results if result.status != "succeeded"),
        "provider_task_ids_reported": provider_ids,
        "provider_metadata_available": provider_ids > 0,
        "measured_task_durations_seconds": measured or None,
        "measured_total_submit_and_wait_seconds": (
            sum(measured) if measured else None
        ),
        "cost_status": "unavailable",
        "note": (
            "Measured values come from this run only. Task durations overlap; "
            "their sum is not wall-clock time. Provider cost is not exposed by "
            "the verified SDK surface and is not estimated."
        ),
    }
    _print_json("Summary", summary)

    if failures:
        print("\n[FAIL] Parallel smoke test failed:")
        for failure in failures:
            print(f"       - {failure}")
        return 1

    print(
        "\n[PASS] Parallel smoke test passed: every task succeeded, ran in "
        "nodus_remote mode, and returned its own input echo."
    )
    return 0


if __name__ == "__main__":
    sys.exit(run_smoke_test())
