"""Repository layer providing clean CRUD interfaces for CLARITY entities.

Encapsulates SQL operations and transactions, decoupling API routes and discovery
services from database details.
"""

from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func
from .models import (
    RunModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    CandidateExperimentModel,
    EvaluationResultModel,
    ComputeTaskModel,
    UsageRecordModel,
    AuditEventModel,
    BenchmarkResultModel,
    ReportModel,
    RunBudgetUsageModel,
    BudgetReservationModel,
)
from ...domain.sanitization import sanitize_metadata
from ...domain.usage_contracts import AuditEventConflictError, TaskRunConflictError


class ClarityRepository:
    """Consolidated repository for managing CLARITY relational entities."""

    def __init__(self, session: Session):
        self.session = session

    # =========================================================================
    # Run Operations
    # =========================================================================

    def create_run(self, run: RunModel) -> RunModel:
        self.session.add(run)
        self.session.flush()
        return run

    def get_run(self, run_id: str) -> Optional[RunModel]:
        return self.session.get(RunModel, run_id)

    def list_runs(
        self, limit: int = 50, offset: int = 0, status_filter: Optional[str] = None
    ) -> List[RunModel]:
        stmt = select(RunModel).order_by(desc(RunModel.created_at))
        if status_filter:
            stmt = stmt.where(RunModel.status == status_filter)
        stmt = stmt.limit(limit).offset(offset)
        return list(self.session.scalars(stmt).all())

    def update_run_status(
        self, run_id: str, status: str, stop_reason: Optional[str] = None
    ) -> Optional[RunModel]:
        run = self.get_run(run_id)
        if run:
            run.status = status
            if stop_reason is not None:
                run.stop_reason = stop_reason
            self.session.flush()
        return run

    # =========================================================================
    # Hypothesis and Revision Operations (Preserving Scientific History)
    # =========================================================================

    def create_hypothesis_with_initial_revision(
        self,
        hypothesis: HypothesisModel,
        initial_revision: HypothesisRevisionModel,
    ) -> HypothesisModel:
        """Create a hypothesis and its initial immutable revision in a single atomic operation."""
        self.session.add(hypothesis)
        initial_revision.hypothesis_id = hypothesis.hypothesis_id
        initial_revision.revision_number = 1
        self.session.add(initial_revision)
        self.session.flush()
        return hypothesis

    def add_hypothesis_revision(
        self,
        hypothesis_id: str,
        revision: HypothesisRevisionModel,
    ) -> HypothesisRevisionModel:
        """Add a new revision to an existing hypothesis while preserving prior revisions."""
        hyp = self.session.get(HypothesisModel, hypothesis_id)
        if not hyp:
            raise ValueError(f"Hypothesis '{hypothesis_id}' not found.")

        hyp.current_revision += 1
        revision.hypothesis_id = hypothesis_id
        revision.revision_number = hyp.current_revision
        self.session.add(revision)
        self.session.flush()
        return revision

    def get_hypothesis(self, hypothesis_id: str) -> Optional[HypothesisModel]:
        return self.session.get(HypothesisModel, hypothesis_id)

    def list_hypotheses_for_run(
        self,
        run_id: str,
        include_excluded: bool = True,
    ) -> List[HypothesisModel]:
        stmt = select(HypothesisModel).where(HypothesisModel.run_id == run_id)
        if not include_excluded:
            stmt = stmt.where(HypothesisModel.is_excluded.is_(False))
        return list(self.session.scalars(stmt).all())

    def update_hypothesis_flags(
        self,
        hypothesis_id: str,
        is_pinned: Optional[bool] = None,
        is_excluded: Optional[bool] = None,
    ) -> Optional[HypothesisModel]:
        hyp = self.session.get(HypothesisModel, hypothesis_id)
        if hyp:
            if is_pinned is not None:
                hyp.is_pinned = is_pinned
            if is_excluded is not None:
                hyp.is_excluded = is_excluded
            self.session.flush()
        return hyp

    # =========================================================================
    # Observation Operations
    # =========================================================================

    def add_observation(self, observation: ObservationModel) -> ObservationModel:
        self.session.add(observation)
        self.session.flush()
        return observation

    def get_observation(self, observation_id: str) -> Optional[ObservationModel]:
        return self.session.get(ObservationModel, observation_id)

    def list_observations_for_run(
        self,
        run_id: str,
        only_training: bool = False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[ObservationModel]:
        stmt = (
            select(ObservationModel)
            .where(ObservationModel.run_id == run_id)
            .order_by(ObservationModel.sequence_number)
        )
        if only_training:
            stmt = stmt.where(ObservationModel.is_held_out.is_(False))
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def count_observations_for_run(
        self,
        run_id: str,
        only_training: bool = False,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(ObservationModel)
            .where(ObservationModel.run_id == run_id)
        )
        if only_training:
            stmt = stmt.where(ObservationModel.is_held_out.is_(False))
        return self.session.scalar(stmt) or 0

    # =========================================================================
    # Evaluation Operations
    # =========================================================================

    def record_evaluation(self, evaluation: EvaluationResultModel) -> EvaluationResultModel:
        # Enforce that evaluation and its referenced revision belong to the exact same run
        if evaluation.revision_id:
            rev = self.session.get(HypothesisRevisionModel, evaluation.revision_id)
            if rev is not None:
                hyp = self.session.get(HypothesisModel, rev.hypothesis_id)
                if hyp is not None and hyp.run_id != evaluation.run_id:
                    raise ValueError(
                        f"Cross-run evaluation invariant violated: EvaluationResult run_id ('{evaluation.run_id}') "
                        f"does not match Hypothesis run_id ('{hyp.run_id}') for revision '{evaluation.revision_id}'."
                    )
        self.session.add(evaluation)
        self.session.flush()
        return evaluation

    def list_evaluations_for_revision(
        self, revision_id: str
    ) -> List[EvaluationResultModel]:
        stmt = (
            select(EvaluationResultModel)
            .where(EvaluationResultModel.revision_id == revision_id)
            .order_by(desc(EvaluationResultModel.evaluated_at))
        )
        return list(self.session.scalars(stmt).all())

    # =========================================================================
    # Compute Tasks Operations (Issue #15 Task-Level Tracking)
    # =========================================================================

    def record_compute_task(self, task: ComputeTaskModel) -> ComputeTaskModel:
        """Idempotently insert or update a compute task record. Reassignment across runs is rejected."""
        existing = self.session.get(ComputeTaskModel, task.task_id)
        if existing:
            if existing.run_id != task.run_id:
                raise TaskRunConflictError(
                    f"Task '{task.task_id}' already belongs to run '{existing.run_id}' and cannot be reassigned to run '{task.run_id}'."
                )
            existing.status = task.status
            if task.started_at is not None:
                existing.started_at = task.started_at
            if task.completed_at is not None:
                existing.completed_at = task.completed_at
            if task.duration_ms is not None:
                existing.duration_ms = task.duration_ms
            if task.provider_task_id is not None:
                existing.provider_task_id = task.provider_task_id
            if task.execution_mode is not None:
                existing.execution_mode = task.execution_mode
            if task.candidate_id is not None:
                existing.candidate_id = task.candidate_id
            if task.error_metadata is not None:
                existing.error_metadata = sanitize_metadata(task.error_metadata)
            self.session.flush()
            return existing
        if task.error_metadata is not None:
            task.error_metadata = sanitize_metadata(task.error_metadata)
        self.session.add(task)
        self.session.flush()
        return task

    def get_compute_task(self, task_id: str) -> Optional[ComputeTaskModel]:
        return self.session.get(ComputeTaskModel, task_id)

    def list_compute_tasks_for_run(
        self,
        run_id: str,
        status: Optional[str] = None,
        execution_mode: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[ComputeTaskModel]:
        stmt = (
            select(ComputeTaskModel)
            .where(ComputeTaskModel.run_id == run_id)
            .order_by(ComputeTaskModel.created_at.asc(), ComputeTaskModel.task_id.asc())
        )
        if status is not None:
            stmt = stmt.where(ComputeTaskModel.status == status)
        if execution_mode is not None:
            stmt = stmt.where(ComputeTaskModel.execution_mode == execution_mode)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def count_compute_tasks_for_run(
        self,
        run_id: str,
        status: Optional[str] = None,
        execution_mode: Optional[str] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(ComputeTaskModel)
            .where(ComputeTaskModel.run_id == run_id)
        )
        if status is not None:
            stmt = stmt.where(ComputeTaskModel.status == status)
        if execution_mode is not None:
            stmt = stmt.where(ComputeTaskModel.execution_mode == execution_mode)
        return self.session.scalar(stmt) or 0

    def get_task_metrics_for_run(self, run_id: str) -> Dict[str, Any]:
        """Aggregate compute task counters, execution modes, and durations using SQL aggregates."""
        stmt = (
            select(
                ComputeTaskModel.status,
                ComputeTaskModel.execution_mode,
                func.count(ComputeTaskModel.task_id),
                func.coalesce(func.sum(ComputeTaskModel.duration_ms), 0),
                func.coalesce(func.max(ComputeTaskModel.duration_ms), 0),
                func.count(ComputeTaskModel.candidate_id),
            )
            .where(ComputeTaskModel.run_id == run_id)
            .group_by(ComputeTaskModel.status, ComputeTaskModel.execution_mode)
        )
        rows = self.session.execute(stmt).all()

        total_tasks = 0
        tasks_submitted = 0
        tasks_completed = 0
        tasks_failed = 0
        tasks_timed_out = 0
        tasks_cancelled = 0
        tasks_retried = 0
        local_tasks = 0
        remote_tasks = 0
        total_duration_ms = 0
        remote_duration_ms = 0
        max_task_duration_ms = 0
        evaluation_tasks_count = 0
        evaluation_tasks_completed = 0

        for status, mode, count, sum_dur, max_dur, cand_count in rows:
            count = int(count or 0)
            sum_dur = int(sum_dur or 0)
            max_dur = int(max_dur or 0)
            cand_count = int(cand_count or 0)

            total_tasks += count
            if status in ("pending", "running", "completed", "failed", "timed_out", "cancelled", "retrying"):
                tasks_submitted += count
            if status == "completed":
                tasks_completed += count
                evaluation_tasks_completed += cand_count
            elif status == "failed":
                tasks_failed += count
            elif status == "timed_out":
                tasks_timed_out += count
            elif status == "cancelled":
                tasks_cancelled += count
            elif status == "retrying":
                tasks_retried += count

            if mode in ("local", "mock"):
                local_tasks += count
            elif mode == "nodus_remote":
                remote_tasks += count
                remote_duration_ms += sum_dur

            total_duration_ms += sum_dur
            if max_dur > max_task_duration_ms:
                max_task_duration_ms = max_dur
            evaluation_tasks_count += cand_count

        return {
            "tasks_total": total_tasks,
            "tasks_submitted": tasks_submitted,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "tasks_timed_out": tasks_timed_out,
            "tasks_cancelled": tasks_cancelled,
            "tasks_retried": tasks_retried,
            "local_tasks_count": local_tasks,
            "remote_tasks_count": remote_tasks,
            "total_execution_duration_ms": total_duration_ms,
            "remote_duration_ms": remote_duration_ms,
            "max_task_duration_ms": max_task_duration_ms,
            "evaluation_tasks_count": evaluation_tasks_count,
            "evaluation_tasks_completed": evaluation_tasks_completed,
        }

    # =========================================================================
    # Audit Events (Append-Only)
    # =========================================================================

    def record_audit_event(self, event: AuditEventModel) -> AuditEventModel:
        """Append an audit event. Existing events with the same event_id are validated for identical content."""
        existing = self.session.get(AuditEventModel, event.event_id)
        if existing:
            conflicts = []
            if existing.run_id != event.run_id:
                conflicts.append(f"run_id mismatch ('{existing.run_id}' != '{event.run_id}')")
            if existing.event_type != event.event_type:
                conflicts.append(f"event_type mismatch ('{existing.event_type}' != '{event.event_type}')")
            if existing.actor != event.actor:
                conflicts.append(f"actor mismatch ('{existing.actor}' != '{event.actor}')")
            if (existing.metadata_json or {}) != (event.metadata_json or {}):
                conflicts.append("metadata payload mismatch")
            if conflicts:
                raise AuditEventConflictError(
                    f"Conflicting audit event reuse for event_id '{event.event_id}': {', '.join(conflicts)}."
                )
            return existing
        self.session.add(event)
        self.session.flush()
        return event

    def get_audit_event(self, event_id: str) -> Optional[AuditEventModel]:
        return self.session.get(AuditEventModel, event_id)

    def list_audit_events_for_run(
        self,
        run_id: str,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ) -> List[AuditEventModel]:
        stmt = (
            select(AuditEventModel)
            .where(AuditEventModel.run_id == run_id)
            .order_by(AuditEventModel.timestamp.asc(), AuditEventModel.event_id.asc())
        )
        if event_type is not None:
            stmt = stmt.where(AuditEventModel.event_type == event_type)
        if offset is not None:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt).all())

    def count_audit_events_for_run(
        self,
        run_id: str,
        event_type: Optional[str] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(AuditEventModel)
            .where(AuditEventModel.run_id == run_id)
        )
        if event_type is not None:
            stmt = stmt.where(AuditEventModel.event_type == event_type)
        return self.session.scalar(stmt) or 0

    # =========================================================================
    # Usage & Budget Operations
    # =========================================================================

    def record_usage(self, usage: UsageRecordModel) -> UsageRecordModel:
        self.session.add(usage)
        self.session.flush()
        return usage

    def get_latest_usage_for_run(self, run_id: str) -> Optional[UsageRecordModel]:
        stmt = (
            select(UsageRecordModel)
            .where(UsageRecordModel.run_id == run_id)
            .order_by(desc(UsageRecordModel.recorded_at))
            .limit(1)
        )
        return self.session.scalars(stmt).first()

    # ------------------------------------------------------------------
    # Per-run budget counters and reservations (Issue #14)
    # ------------------------------------------------------------------

    def get_budget_usage(self, run_id: str) -> Optional[RunBudgetUsageModel]:
        return self.session.get(RunBudgetUsageModel, run_id)

    def create_budget_usage(self, usage: RunBudgetUsageModel) -> RunBudgetUsageModel:
        self.session.add(usage)
        self.session.flush()
        return usage

    def get_budget_reservation(
        self,
        run_id: str,
        resource: str,
        operation_id: str,
    ) -> Optional[BudgetReservationModel]:
        stmt = select(BudgetReservationModel).where(
            BudgetReservationModel.run_id == run_id,
            BudgetReservationModel.resource == resource,
            BudgetReservationModel.operation_id == operation_id,
        )
        return self.session.scalars(stmt).first()

    def add_budget_reservation(
        self, reservation: BudgetReservationModel
    ) -> BudgetReservationModel:
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def list_budget_reservations(
        self,
        run_id: str,
        resource: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[BudgetReservationModel]:
        stmt = select(BudgetReservationModel).where(BudgetReservationModel.run_id == run_id)
        if resource is not None:
            stmt = stmt.where(BudgetReservationModel.resource == resource)
        if state is not None:
            stmt = stmt.where(BudgetReservationModel.state == state)
        stmt = stmt.order_by(BudgetReservationModel.created_at)
        return list(self.session.scalars(stmt).all())

    # =========================================================================
    # Benchmark & Report Operations
    # =========================================================================

    def record_benchmark_result(self, result: BenchmarkResultModel) -> BenchmarkResultModel:
        self.session.add(result)
        self.session.flush()
        return result

    def get_benchmark_results_for_run(self, run_id: str) -> List[BenchmarkResultModel]:
        stmt = (
            select(BenchmarkResultModel)
            .where(BenchmarkResultModel.run_id == run_id)
            .order_by(desc(BenchmarkResultModel.executed_at))
        )
        return list(self.session.scalars(stmt).all())

    def save_report(self, report: ReportModel) -> ReportModel:
        self.session.add(report)
        self.session.flush()
        return report

    def get_latest_report_for_run(self, run_id: str) -> Optional[ReportModel]:
        stmt = (
            select(ReportModel)
            .where(ReportModel.run_id == run_id)
            .order_by(desc(ReportModel.generated_at))
            .limit(1)
        )
        return self.session.scalars(stmt).first()
