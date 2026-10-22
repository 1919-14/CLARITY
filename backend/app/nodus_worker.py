"""Remote worker entry point for CLARITY evaluation tasks (Issue #13).

Deploy this module as the Nodus Function named by ``NODUS_APP_NAME`` /
``NODUS_FUNCTION_NAME``. The Issue #12 adapter invokes it with the keyword
arguments it always sends::

    evaluate(operation=..., payload=..., task_id=..., correlation_id=...)

Supported operations
--------------------
``evaluate_candidate``
    Runs one candidate fit/evaluation through the Issue #6 ``EvaluationPipeline``
    and returns the validated result envelope from
    ``app.services.evaluation_task_runtime``.
``smoke_test``
    Echoes ``payload["echo_value"]`` so a smoke test can verify that a specific
    task's input reached a specific remote execution.

The worker is deliberately stateless: it opens no database connection, reads no
local files, requires no environment credentials beyond the platform's own
injection, and never receives evaluator-only ground truth. Unknown operations
raise, which the platform reports as a failed call rather than a fake result.
"""

from __future__ import annotations

from typing import Any, Optional

from app.services.evaluation_task_runtime import (
    OPERATION_EVALUATE_CANDIDATE,
    execute_evaluation_task_payload,
)

SMOKE_TEST_OPERATION = "smoke_test"
ECHO_VALUE_KEY = "echo_value"
SEQUENCE_KEY = "sequence"


def evaluate(
    *,
    operation: str,
    payload: dict,
    task_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> dict:
    """Execute one task and return a JSON-serializable dict.

    Arguments match the verified Issue #12 spawn contract. ``task_id`` and
    ``correlation_id`` are echoed in smoke-test responses only; evaluation
    results carry their own validated identifiers.
    """
    if not isinstance(operation, str) or not operation:
        raise ValueError("operation must be a non-empty string")
    if not isinstance(payload, dict):
        raise ValueError("payload must be a dict")

    if operation == OPERATION_EVALUATE_CANDIDATE:
        return execute_evaluation_task_payload(payload)

    if operation == SMOKE_TEST_OPERATION:
        if ECHO_VALUE_KEY not in payload:
            raise ValueError("smoke_test payload must contain 'echo_value'")
        response: dict[str, Any] = {
            ECHO_VALUE_KEY: payload[ECHO_VALUE_KEY],
            "task_id": task_id,
            "correlation_id": correlation_id,
        }
        if SEQUENCE_KEY in payload:
            response[SEQUENCE_KEY] = payload[SEQUENCE_KEY]
        return response

    raise ValueError(f"Unsupported operation: {operation!r}")


__all__ = ["evaluate", "ECHO_VALUE_KEY", "SEQUENCE_KEY", "SMOKE_TEST_OPERATION"]
