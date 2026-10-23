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
from ..domain.sanitization import sanitize_metadata
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
    TaskRunConflictError,
    TaskUsageRecord,
)
from .audit_service import record_audit_event_service

# Documented pricing assumptions for application estimates (Issue #15)
# Rate: $0.05 / task minute ($3.00 / compute hour)
DEFAULT_REMOTE_TASK_MINUTE_RATE_USD = 0.05
DEFAULT_REMOTE_TASK_HOURLY_RATE_USD = 3.00
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
    if existing and existing.run_id != run_id:
        raise TaskRunConflictError(
            f"Task '{task_id}' already belongs to run '{existing.run_id}' and cannot be reassigned to run '{run_id}'."
        )

    sanitized_error = sanitize_metadata(dict(error_metadata)) if error_metadata else {}
    combined_metadata = dict(sanitized_error)
    if experiment_id:
        combined_metadata["experiment_id"] = experiment_id
    if task_metadata:
        combined_metadata["task_metadata"] = sanitize_metadata(dict(task_metadata))

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
                audit_meta["error_metadata"] = sanitize_metadata(dict(error_metadata))
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
    local_tasks: int = 0,
    remote_tasks: int = 0,
    remote_duration_ms: int = 0,
    queries_count: int = 0,
    evaluations_count: int = 0,
    remote_tasks_missing_duration: Optional[int] = None,
) -> CostEstimateDetail:
    """Calculate application-side estimated compute cost based on documented pricing assumptions.

    Documented Rates (Issue #15):
    - Sequential Local Execution: $0.00 / hour
    - Nodus Remote Execution: $0.05 / task minute ($3.00 / compute hour)
    - Oracle Query: $0.01 / query
    - Candidate Evaluation: $0.005 / evaluation
    """
    if remote_tasks_missing_duration is None:
        remote_tasks_missing_duration = remote_tasks if (remote_tasks > 0 and remote_duration_ms == 0) else 0

    remote_minutes = remote_duration_ms / (1000.0 * 60.0)
    remote_hours = remote_minutes / 60.0
    remote_compute_cost = remote_minutes * DEFAULT_REMOTE_TASK_MINUTE_RATE_USD
    queries_cost = queries_count * DEFAULT_ORACLE_QUERY_COST_USD
    evaluations_cost = evaluations_count * DEFAULT_EVALUATION_COST_USD

    is_complete = (remote_tasks == 0) or (remote_tasks_missing_duration == 0)

    # When remote tasks exist with completely unknown duration and no other measured spend,
    # mark as UNAVAILABLE (null spend) rather than serializing an unqualified $0.00 estimate.
    if remote_tasks > 0 and remote_tasks_missing_duration == remote_tasks and remote_duration_ms == 0 and queries_cost == 0.0 and evaluations_cost == 0.0:
        return CostEstimateDetail(
            state=CostState.UNAVAILABLE,
            amount_usd=None,
            currency="USD",
            calculation_method="remote_task_duration_unavailable",
            pricing_assumptions={
                "remote_task_minute_rate_usd": DEFAULT_REMOTE_TASK_MINUTE_RATE_USD,
                "remote_task_hourly_rate_usd": DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
                "local_task_hourly_rate_usd": DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
                "oracle_query_cost_usd": DEFAULT_ORACLE_QUERY_COST_USD,
                "candidate_evaluation_cost_usd": DEFAULT_EVALUATION_COST_USD,
                "remote_execution_minutes": 0.0,
                "remote_execution_hours": 0.0,
                "remote_tasks_missing_duration": remote_tasks_missing_duration,
                "is_complete": False,
                "completeness": "incomplete",
            },
            source="application_estimate",
            is_provider_reported=False,
            is_complete=False,
        )

    total_est = round(remote_compute_cost + queries_cost + evaluations_cost, 4)
    calc_method = "application_documented_rates" if is_complete else "application_documented_rates_partial"

    return CostEstimateDetail(
        state=CostState.ESTIMATED,
        amount_usd=total_est,
        currency="USD",
        calculation_method=calc_method,
        pricing_assumptions={
            "remote_task_minute_rate_usd": DEFAULT_REMOTE_TASK_MINUTE_RATE_USD,
            "remote_task_hourly_rate_usd": DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
            "local_task_hourly_rate_usd": DEFAULT_LOCAL_TASK_HOURLY_RATE_USD,
            "oracle_query_cost_usd": DEFAULT_ORACLE_QUERY_COST_USD,
            "candidate_evaluation_cost_usd": DEFAULT_EVALUATION_COST_USD,
            "remote_execution_minutes": round(remote_minutes, 4),
            "remote_execution_hours": round(remote_hours, 4),
            "remote_tasks_missing_duration": remote_tasks_missing_duration,
            "is_complete": is_complete,
            "completeness": "complete" if is_complete else "partial",
        },
        source="application_estimate",
        is_provider_reported=False,
        is_complete=is_complete,
    )


def _evaluate_run_discrepancies(
    run: RunModel,
    task_metrics: Dict[str, Any],
    queries_count: int,
    evals_completed: int,
    recorded_observations_count: int,
) -> Tuple[List[str], Optional[int]]:
    """Evaluate discrepancy dimensions across tasks, usage counters, observations, and run wall-clock duration."""
    discrepancies: List[str] = []

    tasks_total = task_metrics["tasks_total"]
    tasks_completed = task_metrics["tasks_completed"]

    # 1. Tasks completed vs evaluations completed
    if evals_completed > 0 and tasks_completed < evals_completed:
        discrepancies.append(
            f"Completed tasks count ({tasks_completed}) is less than recorded completed evaluations ({evals_completed})."
        )

    # 2. Candidate evaluation tasks vs evaluations completed (must not skip when eval_tasks_count is 0)
    eval_tasks_completed = task_metrics.get("evaluation_tasks_completed", 0)
    if evals_completed > 0 and eval_tasks_completed < evals_completed:
        discrepancies.append(
            f"Candidate evaluation tasks completed ({eval_tasks_completed}) is less than recorded completed evaluations ({evals_completed})."
        )

    # 3. Sum of terminal/retried tasks vs total tasks
    terminal_sum = (
        task_metrics["tasks_completed"]
        + task_metrics["tasks_failed"]
        + task_metrics["tasks_timed_out"]
        + task_metrics["tasks_cancelled"]
        + task_metrics["tasks_retried"]
    )
    if terminal_sum > tasks_total:
        discrepancies.append(
            f"Sum of terminal/retried tasks ({terminal_sum}) exceeds total recorded tasks ({tasks_total})."
        )

    # 4. Task duration vs run wall-clock duration
    run_wall_clock_ms = None
    if run.started_at and run.completed_at:
        t_start = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        t_comp = run.completed_at.replace(tzinfo=timezone.utc) if run.completed_at.tzinfo is None else run.completed_at
        diff_ms = int((t_comp - t_start).total_seconds() * 1000)
        if diff_ms >= 0:
            run_wall_clock_ms = diff_ms
            # In sequential local mode, total execution duration cannot exceed run duration (plus 1000ms grace for clock precision)
            if run.execution_mode in ("local", "mock"):
                if task_metrics["total_execution_duration_ms"] > run_wall_clock_ms + 1000:
                    discrepancies.append(
                        f"Total sequential task duration ({task_metrics['total_execution_duration_ms']}ms) "
                        f"exceeds run wall-clock duration ({run_wall_clock_ms}ms)."
                    )
            # In remote mode, tasks can run concurrently, but no individual task duration can exceed run duration
            elif run.execution_mode == "nodus_remote":
                if task_metrics["max_task_duration_ms"] > run_wall_clock_ms + 1000:
                    discrepancies.append(
                        f"Maximum individual remote task duration ({task_metrics['max_task_duration_ms']}ms) "
                        f"exceeds run wall-clock duration ({run_wall_clock_ms}ms)."
                    )

    # 5. Oracle queries executed vs recorded observations in database
    if recorded_observations_count > 0 and queries_count != recorded_observations_count:
        discrepancies.append(
            f"Usage queries executed ({queries_count}) does not match recorded observations count ({recorded_observations_count})."
        )

    return discrepancies, run_wall_clock_ms


def derive_run_usage_summary(
    session: Session,
    run_id: str,
) -> RunUsageSummary:
    """Derive comprehensive run-level compute usage, task counters, and cost classification."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found.")

    # Aggregate compute task metrics using SQL aggregate queries
    task_metrics = repo.get_task_metrics_for_run(run_id)

    # Fetch latest usage record if present
    latest_usage = repo.get_latest_usage_for_run(run_id)
    budget_usage = repo.get_budget_usage(run_id)
    recorded_observations_count = repo.count_observations_for_run(run_id)

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
        local_tasks=task_metrics["local_tasks_count"],
        remote_tasks=task_metrics["remote_tasks_count"],
        remote_duration_ms=task_metrics["remote_duration_ms"],
        queries_count=queries_count,
        evaluations_count=evals_completed,
        remote_tasks_missing_duration=task_metrics.get("remote_tasks_missing_duration", 0),
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

    # Evaluate reconciliation status and discrepancies
    discrepancies, _ = _evaluate_run_discrepancies(
        run=run,
        task_metrics=task_metrics,
        queries_count=queries_count,
        evals_completed=evals_completed,
        recorded_observations_count=recorded_observations_count,
    )
    reconciliation_status = "reconciled" if len(discrepancies) == 0 else "discrepancy_detected"

    return RunUsageSummary(
        run_id=run_id,
        queries_executed=queries_count,
        evaluations_scheduled=evals_scheduled,
        evaluations_completed=evals_completed,
        tasks_total=task_metrics["tasks_total"],
        tasks_submitted=task_metrics["tasks_submitted"],
        tasks_completed=task_metrics["tasks_completed"],
        tasks_failed=task_metrics["tasks_failed"],
        tasks_timed_out=task_metrics["tasks_timed_out"],
        tasks_cancelled=task_metrics["tasks_cancelled"],
        tasks_retried=task_metrics["tasks_retried"],
        local_tasks_count=task_metrics["local_tasks_count"],
        remote_tasks_count=task_metrics["remote_tasks_count"],
        total_execution_duration_ms=task_metrics["total_execution_duration_ms"],
        estimated_cost=estimated_cost,
        accruing_cost=accruing_cost,
        settled_cost=settled_cost,
        reconciliation_status=reconciliation_status,
        reconciliation_discrepancies=discrepancies,
        is_budget_exhausted=is_budget_exhausted,
        exhaustion_reason=exhaustion_reason,
        recorded_at=latest_usage.recorded_at if latest_usage else datetime.now(timezone.utc),
    )


def reconcile_run_usage(
    session: Session,
    run_id: str,
) -> Dict[str, Any]:
    """Reconcile run-level usage records with underlying task-level execution records."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run '{run_id}' not found.")

    task_metrics = repo.get_task_metrics_for_run(run_id)
    latest_usage = repo.get_latest_usage_for_run(run_id)
    budget_usage = repo.get_budget_usage(run_id)
    recorded_observations_count = repo.count_observations_for_run(run_id)

    queries_count = (
        latest_usage.queries_executed
        if latest_usage
        else (budget_usage.queries_committed if budget_usage else 0)
    )
    evals_completed = (
        latest_usage.evaluations_completed
        if latest_usage
        else (budget_usage.evaluations_committed if budget_usage else 0)
    )

    discrepancies, run_wall_clock_ms = _evaluate_run_discrepancies(
        run=run,
        task_metrics=task_metrics,
        queries_count=queries_count,
        evals_completed=evals_completed,
        recorded_observations_count=recorded_observations_count,
    )

    is_reconciled = len(discrepancies) == 0
    status_str = "reconciled" if is_reconciled else "discrepancy_detected"

    return {
        "run_id": run_id,
        "is_reconciled": is_reconciled,
        "reconciliation_status": status_str,
        "discrepancies": discrepancies,
        "task_count": task_metrics["tasks_total"],
        "tasks_submitted": task_metrics["tasks_submitted"],
        "tasks_completed": task_metrics["tasks_completed"],
        "tasks_failed": task_metrics["tasks_failed"],
        "tasks_timed_out": task_metrics["tasks_timed_out"],
        "tasks_cancelled": task_metrics["tasks_cancelled"],
        "tasks_retried": task_metrics["tasks_retried"],
        "total_duration_ms": task_metrics["total_execution_duration_ms"],
        "run_duration_ms": run_wall_clock_ms,
        "queries_executed": queries_count,
        "recorded_observations_count": recorded_observations_count,
        "evaluations_completed": evals_completed,
    }
