"""Domain contracts for compute-usage tracking, cost classification, and audit events (Issue #15).

Design Rules:
-------------
1. Distinguish measured provider data, application-side estimates, and unavailable data.
2. Never represent missing cost data as zero spend.
3. Keep task-level records so run-level totals can be derived and independently reconciled.
4. Support all four cost states: estimated, accruing, settled, and unavailable.
5. Provide append-only audit trail contracts with structured metadata and actor provenance.
6. Strictly redact sensitive data (tokens, API keys, credentials) from audit payloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence


class CostState(str, Enum):
    """Cost data availability and settlement classification.

    - ESTIMATED: Calculated from documented pricing or an explicitly documented estimation method.
    - ACCRUING: Provider-reported usage or charges that are in-flight and not yet final.
    - SETTLED: Finalized provider-reported cost when available.
    - UNAVAILABLE: Insufficient information to calculate or obtain a reliable figure (never serialized as zero).
    """

    ESTIMATED = "estimated"
    ACCRUING = "accruing"
    SETTLED = "settled"
    UNAVAILABLE = "unavailable"


class ComputeExecutionMode(str, Enum):
    """Execution environment mode for compute tasks."""

    LOCAL = "local"
    NODUS_REMOTE = "nodus_remote"
    MOCK = "mock"


class ComputeTaskStatus(str, Enum):
    """Lifecycle status of an individual compute execution task."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    RETRYING = "retrying"

    @property
    def is_terminal(self) -> bool:
        return self in (
            ComputeTaskStatus.COMPLETED,
            ComputeTaskStatus.FAILED,
            ComputeTaskStatus.TIMED_OUT,
            ComputeTaskStatus.CANCELLED,
        )


@dataclass(frozen=True)
class CostEstimateDetail:
    """Detailed breakdown of a cost estimate or provider charge."""

    state: CostState
    amount_usd: Optional[float]
    currency: str = "USD"
    calculation_method: str = "none"
    pricing_assumptions: Dict[str, Any] = field(default_factory=dict)
    source: str = "application"
    is_provider_reported: bool = False
    is_complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "amount_usd": self.amount_usd,
            "currency": self.currency,
            "calculation_method": self.calculation_method,
            "pricing_assumptions": dict(self.pricing_assumptions),
            "source": self.source,
            "is_provider_reported": self.is_provider_reported,
            "is_complete": self.is_complete,
        }


@dataclass(frozen=True)
class TaskUsageRecord:
    """Task-level compute usage record."""

    task_id: str
    run_id: str
    candidate_id: Optional[str] = None
    experiment_id: Optional[str] = None
    execution_mode: ComputeExecutionMode = ComputeExecutionMode.LOCAL
    status: ComputeTaskStatus = ComputeTaskStatus.PENDING
    provider_task_id: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    cost: Optional[CostEstimateDetail] = None
    error_metadata: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "candidate_id": self.candidate_id,
            "experiment_id": self.experiment_id,
            "execution_mode": self.execution_mode.value,
            "status": self.status.value,
            "provider_task_id": self.provider_task_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "cost": self.cost.to_dict() if self.cost else None,
            "error_metadata": self.error_metadata,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True)
class RunUsageSummary:
    """Comprehensive run-level compute usage summary reconciled with task records."""

    run_id: str
    queries_executed: int
    evaluations_scheduled: int
    evaluations_completed: int
    tasks_total: int
    tasks_submitted: int
    tasks_completed: int
    tasks_failed: int
    tasks_timed_out: int
    tasks_cancelled: int
    tasks_retried: int
    local_tasks_count: int
    remote_tasks_count: int
    total_execution_duration_ms: int
    estimated_cost: CostEstimateDetail
    accruing_cost: CostEstimateDetail
    settled_cost: CostEstimateDetail
    reconciliation_status: str  # "reconciled" or "discrepancy_detected"
    reconciliation_discrepancies: List[str] = field(default_factory=list)
    is_budget_exhausted: bool = False
    exhaustion_reason: Optional[str] = None
    recorded_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "queries_executed": self.queries_executed,
            "evaluations_scheduled": self.evaluations_scheduled,
            "evaluations_completed": self.evaluations_completed,
            "task_counters": {
                "total": self.tasks_total,
                "submitted": self.tasks_submitted,
                "completed": self.tasks_completed,
                "failed": self.tasks_failed,
                "timed_out": self.tasks_timed_out,
                "cancelled": self.tasks_cancelled,
                "retried": self.tasks_retried,
            },
            "execution_modes": {
                "local": self.local_tasks_count,
                "remote": self.remote_tasks_count,
            },
            "total_execution_duration_ms": self.total_execution_duration_ms,
            "costs": {
                "estimated": self.estimated_cost.to_dict(),
                "accruing": self.accruing_cost.to_dict(),
                "settled": self.settled_cost.to_dict(),
            },
            "reconciliation": {
                "is_reconciled": self.reconciliation_status == "reconciled",
                "status": self.reconciliation_status,
                "discrepancies": list(self.reconciliation_discrepancies),
            },
            "reconciliation_status": self.reconciliation_status,
            "is_budget_exhausted": self.is_budget_exhausted,
            "exhaustion_reason": self.exhaustion_reason,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
        }


# Standard audit event types
AUDIT_RUN_CREATED = "RUN_CREATED"
AUDIT_RUN_CONFIGURED = "RUN_CONFIGURED"
AUDIT_RUN_STARTED = "RUN_STARTED"
AUDIT_RUN_COMPLETED = "RUN_COMPLETED"
AUDIT_RUN_FAILED = "RUN_FAILED"
AUDIT_RUN_CANCELLED = "RUN_CANCELLED"
AUDIT_RUN_STOPPED_BY_BUDGET = "RUN_STOPPED_BY_BUDGET"

AUDIT_BUDGET_CONFIGURED = "BUDGET_CONFIGURED"
AUDIT_BUDGET_WARNING = "BUDGET_WARNING"
AUDIT_BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"

AUDIT_TASK_SUBMITTED = "TASK_SUBMITTED"
AUDIT_TASK_STARTED = "TASK_STARTED"
AUDIT_TASK_COMPLETED = "TASK_COMPLETED"
AUDIT_TASK_FAILED = "TASK_FAILED"
AUDIT_TASK_TIMED_OUT = "TASK_TIMED_OUT"
AUDIT_TASK_RETRIED = "TASK_RETRIED"
AUDIT_TASK_CANCELLED = "TASK_CANCELLED"

AUDIT_ORACLE_QUERY_EXECUTED = "ORACLE_QUERY_EXECUTED"
AUDIT_OBSERVATION_RECORDED = "OBSERVATION_RECORDED"

AUDIT_HYPOTHESIS_PROPOSED = "HYPOTHESIS_PROPOSED"
AUDIT_HYPOTHESIS_EVALUATED = "HYPOTHESIS_EVALUATED"
AUDIT_HYPOTHESIS_RANKED = "HYPOTHESIS_RANKED"
AUDIT_HYPOTHESIS_PINNED = "HYPOTHESIS_PINNED"
AUDIT_HYPOTHESIS_UNPINNED = "HYPOTHESIS_UNPINNED"
AUDIT_HYPOTHESIS_EXCLUDED = "HYPOTHESIS_EXCLUDED"
AUDIT_HYPOTHESIS_RESTORED = "HYPOTHESIS_RESTORED"
AUDIT_HYPOTHESIS_REEVALUATED = "HYPOTHESIS_REEVALUATED"

AUDIT_REPORT_GENERATED = "REPORT_GENERATED"


@dataclass(frozen=True)
class AuditEventRecord:
    """Immutable audit trail record."""

    event_id: str
    run_id: str
    event_type: str
    actor: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "event_type": self.event_type,
            "actor": self.actor,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "metadata": dict(self.metadata),
        }


class AuditEventConflictError(ValueError):
    """Raised when an audit event with an existing event_id has conflicting fields (cross-run or conflicting payload)."""


class TaskRunConflictError(ValueError):
    """Raised when a task ID belongs to another run or is reassigned across runs."""

