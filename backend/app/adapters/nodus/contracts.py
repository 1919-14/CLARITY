"""Contracts for the Nodus Compute execution adapter.

Defines the stable task and normalized result structures, execution modes,
and strict JSON payload validation to ensure computational workloads remain
decoupled from SDK internals and evaluator ground truth.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ExecutionMode(str, Enum):
    """Execution modes supported by the Nodus adapter."""

    REMOTE = "nodus_remote"
    LOCAL = "local"
    MOCK = "mock"

    @classmethod
    def from_str(cls, value: str) -> ExecutionMode:
        """Parse execution mode string case-insensitively."""
        normalized = value.strip().lower()
        for mode in cls:
            if mode.value == normalized:
                return mode
        raise ValueError(
            f"Invalid execution mode: {value!r}. Must be one of: "
            f"{', '.join(m.value for m in cls)}"
        )


# Prohibited payload keys that could leak ground-truth evaluator state or secrets
PROHIBITED_PAYLOAD_KEYS = frozenset(
    {
        "hidden_rule",
        "ground_truth",
        "evaluator_labels",
        "oracle_secret",
        "secret_key",
        "api_key",
        "token",
        "password",
    }
)


def validate_json_payload(payload: Any) -> None:
    """Validate that a payload is strictly JSON-serializable and non-sensitive.

    Raises:
        TypeError: If the payload contains non-serializable objects (callables,
            open file handles, live database sessions, etc.).
        ValueError: If the payload contains prohibited secret-bearing keys or
            cyclic references.
    """
    if not isinstance(payload, dict):
        raise TypeError(f"Task payload must be a dict, got {type(payload).__name__}")

    def _check_serializable(obj: Any, path: str = "payload") -> None:
        if obj is None or isinstance(obj, (bool, int, float, str)):
            return
        if isinstance(obj, list):
            for i, item in enumerate(obj):
                _check_serializable(item, f"{path}[{i}]")
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str):
                    raise TypeError(f"Payload keys must be strings, got {type(k).__name__} at {path}")
                k_lower = k.lower()
                for prohibited in PROHIBITED_PAYLOAD_KEYS:
                    if prohibited in k_lower:
                        raise ValueError(f"Payload contains prohibited key {k!r} at {path}")
                _check_serializable(v, f"{path}.{k}")
            return
        raise TypeError(
            f"Unsupported non-serializable type {type(obj).__name__} at {path}. "
            "Payload must contain only JSON-compatible types."
        )

    _check_serializable(payload)

    # Secondary check: verify actual json.dumps succeeds without error
    try:
        json.dumps(payload)
    except (TypeError, OverflowError, ValueError) as exc:
        raise ValueError(f"Payload failed JSON serialization check: {exc}") from exc


@dataclass(frozen=True)
class NodusTask:
    """Task specification submitted to the Nodus execution adapter.

    Attributes:
        task_id: Unique, stable task identifier.
        operation: Name of computation operation (e.g. 'evaluate_candidate', 'smoke_test').
        payload: Validated JSON-compatible payload dictionary.
        correlation_id: Optional correlation/run identifier.
        timeout_seconds: Per-task execution timeout in seconds.
        max_retries: Maximum permitted retries for transient errors.
        metadata: Optional task context metadata.
    """

    task_id: str
    operation: str
    payload: dict[str, Any]
    correlation_id: str | None = None
    timeout_seconds: float = 30.0
    max_retries: int = 2
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id must be a non-empty string")
        if not self.operation or not isinstance(self.operation, str):
            raise ValueError("operation must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        validate_json_payload(self.payload)

    def to_dict(self) -> dict[str, Any]:
        """Serialize task to dictionary representation."""
        return {
            "task_id": self.task_id,
            "operation": self.operation,
            "payload": self.payload,
            "correlation_id": self.correlation_id,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class NodusResult:
    """Normalized result returned from task execution.

    Attributes:
        task_id: Matching task identifier.
        execution_mode: Explicit mode under which execution occurred.
        status: Terminal or current status ('succeeded', 'failed', 'timed_out', 'cancelled').
        result_data: Computed result dictionary when successful.
        error: Sanitized error dictionary when failed.
        provider_task_id: Genuine provider task ID when available (never fabricated).
        submitted_at: Submission timestamp.
        completed_at: Completion timestamp.
        duration_seconds: Measured execution duration in seconds.
        provider_metadata: Genuine provider metadata when available (never fabricated).
    """

    task_id: str
    execution_mode: str
    status: str
    result_data: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    provider_task_id: str | None = None
    submitted_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.task_id or not isinstance(self.task_id, str):
            raise ValueError("task_id must be a non-empty string")
        if self.status not in ("succeeded", "failed", "timed_out", "cancelled"):
            raise ValueError(f"Invalid result status: {self.status!r}")

    @property
    def is_success(self) -> bool:
        """Return True if execution succeeded."""
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        """Serialize result to dictionary representation."""
        return {
            "task_id": self.task_id,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "result_data": self.result_data,
            "error": self.error,
            "provider_task_id": self.provider_task_id,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_seconds": self.duration_seconds,
            "provider_metadata": dict(self.provider_metadata),
        }
