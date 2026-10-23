"""Compute-usage tracking, cost classification, and run reconciliation service (Issue #15).

Invariants:
-----------
1. Task-level granularity: all local and remote task executions are individually recorded.
2. Honest cost classification:
   - Estimated: application-side calculation with documented method and pricing assumptions.
   - Accruing: live provider-reported charges when available.
   - Settled: finalized provider-reported charges when available.
   - Unavailable: null when no data is available; never coerced or fabricated as zero.
3. Idempotent accounting: re-ingesting a task status or event updates the existing task record
   and does not double-count usage or tasks.
4. Run reconciliation: run-level counters can be derived from and verified against task records.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    ComputeTaskModel,
    RunBudgetUsageModel,
    RunModel,
    UsageRecordModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.usage_contracts import (
    AUDIT_TASK_CANCELLED,
    AUDIT_TASK_COMPLETED,
    AUDIT_TASK_FAILED,
    AUDIT_TASK_RETRIED,
    AUDIT_TASK_STARTED,
    AUDIT_TASK_SUBMITTED,
    AUDIT_TASK_TIMED_OUT,
    ComputeExecutionMode,
    ComputeTaskStatus,
    CostEstimateDetail,
    CostState,
    RunUsageSummary,
    TaskUsageRecord,
)
from .audit_service import record_audit_event_service

# Documented pricing assumptions for application estimates (Issue #15)
DEFAULT_REMOTE_TASK_HOURLY_RATE_USD = 0.50
DEFAULT_LOCAL_TASK_HOURLY_RATE_USD = 0.00
DEFAULT_ORACLE_QUERY_COST_USD = 0.01
DEFAULT_EVALUATION_COST_USD = 0.005


def record_task_usage_service(
    session: Session,
    run_id: str,
    task_id: str,
    execution_mode: str = "local",
    candidate_id: Optional[str] = None,
    experiment_id: Optional[str] = None,
    status: str = "pending",
    provider_task_id: Optional[str] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    duration_ms: Optional[int] = None,
    error_metadata: Optional[Mapping[str, Any]] = None,
    task_metadata: Optional[Mapping[str, Any]] = None,
    record_audit: bool = True,
) -> ComputeTaskModel:
    """Record or update an individual compute task execution unit.

    Idempotent: updates existing task by task_id rather than inserting duplicates,
    preventing double-counting on retries or duplicate event deliveries.
    """
    repo = ClarityRepository(session)
    existing = repo.get_compute_task(task_id)

    combined_metadata = dict(error_metadata or {})
    if experiment_id:
        combined_metadata["experiment_id"] = experiment_id
    if task_metadata:
        combined_metadata["task_metadata"] = dict(task_metadata)

    # Compute duration if start/complete timestamps are present
    calc_duration = duration_ms
    if calc_duration is None and started_at and completed_at:
        calc_duration = int((completed_at - started_at).total_seconds() * 1000)

    is_new = existing is None
    prev_status = existing.status if existing else None

    task = ComputeTaskModel(
        task_id=task_id,
        run_id=run_id,
        candidate_id=candidate_id,
        execution_mode=execution_mode,
        provider_task_id=provider_task_id,
        status=status,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=calc_duration,
        error_metadata=combined_metadata if combined_metadata else None,
    )
    saved_task = repo.record_compute_task(task)

    # Record appropriate audit event for task state changes
    if record_audit:
        event_map = {
            "pending": AUDIT_TASK_SUBMITTED,
            "running": AUDIT_TASK_STARTED,
            "completed": AUDIT_TASK_COMPLETED,
            "failed": AUDIT_TASK_FAILED,
            "timed_out": AUDIT_TASK_TIMED_OUT,
            "cancelled": AUDIT_TASK_CANCELLED,
            "retrying": AUDIT_TASK_RETRIED,
        }
        event_type = event_map.get(status)
        if event_type and (is_new or prev_status != status):
            audit_meta = {
                "task_id": task_id,
                "execution_mode": execution_mode,
                "status": status,
                "candidate_id": candidate_id,
                "experiment_id": experiment_id,
                "duration_ms": calc_duration,
            }
            if provider_task_id:
                audit_meta["provider_task_id"] = provider_task_id
            if error_metadata:
                audit_meta["error_metadata"] = dict(error_metadata)
            record_audit_event_service(
                session=session,
                run_id=run_id,
                event_type=event_type,
                actor="nodus" if execution_mode == "nodus_remote" else "orchestrator",
                metadata=audit_meta,
            )

    session.flush()
    return saved_task


def calculate_cost_estimate(
    local_tasks: int,
    remote_tasks: int,
    remote_duration_ms: int,
    queries_count: int,
    evaluations_count: int,
) -> CostEstimateDetail:
    """Calculate application-side estimated compute cost based on documented pricing assumptions."""
    remote_hours = remote_duration_ms / (1000.0 * 3600.0)
    remote_compute_cost = remote_hours * DEFAULT_REMOTE_TASK_HOURLY_RATE_USD
    queries_cost = queries_count * DEFAULT_ORACLE_QUERY_COST_USD
    evaluations_cost = evaluations_count * DEFAULT_EVALUATION_COST_USD

    total_est = round(remote_compute_cost + queries_cost + evaluations_cost, 4)

    return CostEstimateDetail(
        state=CostState.ESTIMATED,
        amount_usd=total_est,
        currency="USD",
        calculation_method="application_documented_rates",
        pricing_assumptions={
            "remote_task_hourly_rate_usd": DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
            "local_task_hourly_rate_usd": DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
            "oracle_query_cost_usd": DEFAULT_ORACLE_QUERY_COST_USD,
            "candidate_evaluation_cost_usd": DEFAULT_EVALUATION_COST_USD,
            "remote_execution_hours": round(remote_hours, 4),
        },
        source="application_estimate",
        is_provider_reported=False,
    )


def derive_run_usage_summary(
    session: Session,
    run_id: str,
) -> RunUsageSummary:
    """Derive comprehensive run-level compute usage, task counters, and cost classification."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found.")

    tasks = repo.list_compute_tasks_for_run(run_id)
    tasks_total = len(tasks)
    tasks_submitted = len([t for t in tasks if t.status in ("pending", "running", "completed", "failed", "timed_out", "cancelled", "retrying")])
    tasks_completed = len([t for t in tasks if t.status == "completed"])
    tasks_failed = len([t for t in tasks if t.status == "failed"])
    tasks_timed_out = len([t for t in tasks if t.status == "timed_out"])
    tasks_cancelled = len([t for t in tasks if t.status == "cancelled"])
    tasks_retried = len([t for t in tasks if t.status == "retrying"])

    local_tasks = len([t for t in tasks if t.execution_mode in ("local", "mock")])
    remote_tasks = len([t for t in tasks if t.execution_mode == "nodus_remote"])

    total_duration_ms = sum((t.duration_ms or 0) for t in tasks)
    remote_duration_ms = sum((t.duration_ms or 0) for t in tasks if t.execution_mode == "nodus_remote")

    # Fetch latest usage record if present
    latest_usage = repo.get_latest_usage_for_run(run_id)
    budget_usage = repo.get_budget_usage(run_id)

    queries_count = (
        latest_usage.queries_executed
        if latest_usage
        else (budget_usage.queries_committed if budget_usage else 0)
    )
    evals_scheduled = (
        latest_usage.evaluations_scheduled
        if latest_usage
        else (budget_usage.evaluations_reserved if budget_usage else 0)
    )
    evals_completed = (
        latest_usage.evaluations_completed
        if latest_usage
        else (budget_usage.evaluations_committed if budget_usage else 0)
    )

    is_budget_exhausted = (
        latest_usage.is_budget_exhausted
        if latest_usage
        else (budget_usage.exhaustion_reason is not None if budget_usage else False)
    )
    exhaustion_reason = (
        latest_usage.exhaustion_reason
        if latest_usage
        else (budget_usage.exhaustion_reason if budget_usage else None)
    )

    # 1. Estimated Cost: Calculated from documented assumptions
    estimated_cost = calculate_cost_estimate(
        local_tasks=local_tasks,
        remote_tasks=remote_tasks,
        remote_duration_ms=remote_duration_ms,
        queries_count=queries_count,
        evaluations_count=evals_completed,
    )

    # 2. Accruing Cost: Provider-reported in-flight cost, or unavailable
    provider_accruing = latest_usage.live_accruing_cost_usd if latest_usage else None
    if provider_accruing is not None:
        accruing_cost = CostEstimateDetail(
            state=CostState.ACCRUING,
            amount_usd=float(provider_accruing),
            currency="USD",
            calculation_method="provider_live_reported",
            source="nodus_provider",
            is_provider_reported=True,
        )
    else:
        accruing_cost = CostEstimateDetail(
            state=CostState.UNAVAILABLE,
            amount_usd=None,
            currency="USD",
            calculation_method="provider_billing_callback_unavailable",
            source="none",
            is_provider_reported=False,
        )

    # 3. Settled Cost: Final provider invoice cost, or unavailable
    provider_settled = latest_usage.settled_cost_usd if latest_usage else None
    if provider_settled is not None:
        settled_cost = CostEstimateDetail(
            state=CostState.SETTLED,
            amount_usd=float(provider_settled),
            currency="USD",
            calculation_method="provider_invoice_settlement",
            source="nodus_provider",
            is_provider_reported=True,
        )
    else:
        settled_cost = CostEstimateDetail(
            state=CostState.UNAVAILABLE,
            amount_usd=None,
            currency="USD",
            calculation_method="provider_invoice_unavailable",
            source="none",
            is_provider_reported=False,
        )

    # Reconciliation status
    reconciliation_status = "reconciled"
    if tasks_total > 0 and evals_completed > 0 and tasks_completed < evals_completed:
        # A discrepancy exists if completed tasks count does not account for evaluations
        reconciliation_status = "discrepancy_detected"

    return RunUsageSummary(
        run_id=run_id,
        queries_executed=queries_count,
        evaluations_scheduled=evals_scheduled,
        evaluations_completed=evals_completed,
        tasks_total=tasks_total,
        tasks_submitted=tasks_submitted,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        tasks_timed_out=tasks_timed_out,
        tasks_cancelled=tasks_cancelled,
        tasks_retried=tasks_retried,
        local_tasks_count=local_tasks,
        remote_tasks_count=remote_tasks,
        total_execution_duration_ms=total_duration_ms,
        estimated_cost=estimated_cost,
        accruing_cost=accruing_cost,
        settled_cost=settled_cost,
        reconciliation_status=reconciliation_status,
        is_budget_exhausted=is_budget_exhausted,
        exhaustion_reason=exhaustion_reason,
        recorded_at=latest_usage.recorded_at if latest_usage else datetime.now(timezone.utc),
    )


def reconcile_run_usage(
    session: Session,
    run_id: str,
) -> Dict[str, Any]:
    """Reconcile run-level usage records with underlying task-level execution records."""
    summary = derive_run_usage_summary(session, run_id)
    repo = ClarityRepository(session)
    tasks = repo.list_compute_tasks_for_run(run_id)

    task_completed_count = len([t for t in tasks if t.status == "completed"])
    task_duration_sum = sum((t.duration_ms or 0) for t in tasks)

    return {
        "run_id": run_id,
        "is_reconciled": summary.reconciliation_status == "reconciled",
        "task_count": summary.tasks_total,
        "tasks_completed": task_completed_count,
        "total_duration_ms": task_duration_sum,
        "queries_executed": summary.queries_executed,
        "evaluations_completed": summary.evaluations_completed,
        "reconciliation_status": summary.reconciliation_status,
    }
