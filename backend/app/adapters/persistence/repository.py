"""Repository layer providing clean CRUD interfaces for CLARITY entities.

Encapsulates SQL operations and transactions, decoupling API routes and discovery
services from database details.
"""

from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
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
)


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

    def list_runs(self, limit: int = 50, offset: int = 0) -> List[RunModel]:
        stmt = select(RunModel).order_by(desc(RunModel.created_at)).limit(limit).offset(offset)
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

    def list_observations_for_run(
        self,
        run_id: str,
        only_training: bool = False,
    ) -> List[ObservationModel]:
        stmt = (
            select(ObservationModel)
            .where(ObservationModel.run_id == run_id)
            .order_by(ObservationModel.sequence_number)
        )
        if only_training:
            stmt = stmt.where(ObservationModel.is_held_out.is_(False))
        return list(self.session.scalars(stmt).all())

    # =========================================================================
    # Evaluation Operations
    # =========================================================================

    def record_evaluation(self, evaluation: EvaluationResultModel) -> EvaluationResultModel:
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
    # Audit Events
    # =========================================================================

    def record_audit_event(self, event: AuditEventModel) -> AuditEventModel:
        self.session.add(event)
        self.session.flush()
        return event

    def list_audit_events_for_run(self, run_id: str) -> List[AuditEventModel]:
        stmt = (
            select(AuditEventModel)
            .where(AuditEventModel.run_id == run_id)
            .order_by(AuditEventModel.timestamp)
        )
        return list(self.session.scalars(stmt).all())

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
