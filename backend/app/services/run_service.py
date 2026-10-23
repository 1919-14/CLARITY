"""Shared service layer for discovery run operations.

Encapsulates run creation, usage tracking initialization, status transitions,
and audit trails, keeping REST API route handlers thin.
"""

from datetime import datetime, timezone
import uuid
from typing import Any, Dict, Optional
from sqlalchemy.orm import Session

from ..adapters.persistence.models import AuditEventModel, RunModel, UsageRecordModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import RunBudgetConfig
from .budget_service import initialize_run_budget


class RunServiceError(Exception):
    """Base exception for run service operations."""


class RunNotFoundError(RunServiceError):
    """Raised when a run cannot be found."""


class RunConflictError(RunServiceError):
    """Raised when an operation conflicts with the current run state (e.g. terminal state)."""


TERMINAL_RUN_STATUSES = frozenset(
    {"completed", "budget_exhausted", "stopped_by_budget", "cancelled", "failed"}
)


def create_run_service(
    session: Session,
    name: str,
    domain: str = "1d_polynomial",
    execution_mode: str = "local",
    sampling_strategy: str = "active_disagreement",
    max_queries: Optional[int] = None,
    max_evaluations: Optional[int] = None,
    max_runtime_seconds: Optional[int] = None,
    spend_ceiling_usd: Optional[float] = None,
    run_id: Optional[str] = None,
    budget: Optional[RunBudgetConfig] = None,
) -> RunModel:
    """Create a new discovery run, its validated budget, zero usage, and audit trail.

    ``budget`` is the canonical Issue #14 configuration. When omitted, the
    legacy flat arguments are resolved into a validated budget with documented
    defaults, so every newly created run has an explicit budget.
    """
    repo = ClarityRepository(session)
    resolved_run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"

    if repo.get_run(resolved_run_id):
        raise RunConflictError(f"Run with ID '{resolved_run_id}' already exists.")

    if budget is None:
        legacy_payload = {}
        if max_queries is not None:
            legacy_payload["max_oracle_queries"] = max_queries
        if max_evaluations is not None:
            legacy_payload["max_candidate_evaluations"] = max_evaluations
        if max_runtime_seconds is not None:
            legacy_payload["max_runtime_seconds"] = max_runtime_seconds
        if spend_ceiling_usd is not None:
            legacy_payload["max_compute_spend_usd"] = spend_ceiling_usd
        budget = RunBudgetConfig.from_mapping(legacy_payload)

    run = RunModel(
        run_id=resolved_run_id,
        name=name,
        domain=domain,
        status="pending",
        execution_mode=execution_mode,
        sampling_strategy=sampling_strategy,
        max_queries=budget.max_oracle_queries,
        max_evaluations=budget.max_candidate_evaluations,
        max_runtime_seconds=budget.max_runtime_seconds,
        spend_ceiling_usd=budget.max_compute_spend_usd,
        max_concurrent_remote_tasks=budget.max_concurrent_remote_tasks,
        max_generated_hypotheses=budget.max_generated_hypotheses,
        budget_config_json=budget.to_dict(),
    )
    repo.create_run(run)

    # Audit event for run creation
    audit = AuditEventModel(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        run_id=resolved_run_id,
        event_type="RUN_CREATED",
        actor="researcher",
        metadata_json={
            "name": name,
            "domain": domain,
            "execution_mode": execution_mode,
            "limits": {
                "max_queries": budget.max_oracle_queries,
                "max_evaluations": budget.max_candidate_evaluations,
                "max_runtime_seconds": budget.max_runtime_seconds,
                "spend_ceiling_usd": budget.max_compute_spend_usd,
                "max_concurrent_remote_tasks": budget.max_concurrent_remote_tasks,
                "max_generated_hypotheses": budget.max_generated_hypotheses,
            },
            "budget": budget.to_dict(),
        },
    )
    repo.record_audit_event(audit)

    initialize_run_budget(session, run, budget, actor="researcher")

    # Initial zero usage record
    usage = UsageRecordModel(
        usage_id=f"use-{uuid.uuid4().hex[:12]}",
        run_id=resolved_run_id,
        queries_executed=0,
        evaluations_scheduled=0,
        evaluations_completed=0,
        # Application-side estimate of zero is measured; accruing/settled are
        # unknown until the provider reports them, so they are null, not zero.
        estimated_cost_usd=0.0,
        live_accruing_cost_usd=None,
        settled_cost_usd=None,
        is_budget_exhausted=False,
    )
    repo.record_usage(usage)
    session.flush()
    return run


def cancel_run_service(
    session: Session,
    run_id: str,
    stop_reason: str = "Cancelled by researcher",
) -> RunModel:
    """Cancel an active run, guarding against modifying terminal runs and logging audit event."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise RunNotFoundError(f"Run '{run_id}' not found.")

    if run.status in TERMINAL_RUN_STATUSES:
        raise RunConflictError(
            f"Run '{run_id}' is already in a terminal state ('{run.status}') and cannot be cancelled."
        )

    previous_status = run.status
    run.status = "cancelled"
    run.stop_reason = stop_reason
    run.completed_at = datetime.now(timezone.utc)

    audit = AuditEventModel(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        event_type="RUN_CANCELLED",
        actor="researcher",
        metadata_json={
            "run_id": run_id,
            "stop_reason": stop_reason,
            "previous_status": previous_status,
        },
    )
    repo.record_audit_event(audit)
    session.flush()
    return run
