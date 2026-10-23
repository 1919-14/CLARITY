"""SQLAlchemy relational entity models for CLARITY.

Persists:
- Discovery runs (lifecycle status, limits, execution mode, stop reason)
- Hypotheses & Hypothesis Revisions (scientific provenance, formula, complexity, pinning)
- Observations (input, output, status, sequences, noise)
- Candidate Experiments (proposed inputs, selection rationale, observation link)
- Hypothesis Evaluations (metrics, sample counts, held-out validation, exact revision & dataset reference)
- Compute Tasks (Nodus task tracking, execution status, timings, errors)
- Usage & Budgets (queries executed, evaluations scheduled, cost states, budget exhaustion)
- Per-run budget usage counters and resource reservations (Issue #14)
- Audit Events (researcher actions: pin, exclude, restore, re-evaluate)
- Benchmark Results (Hit@1/3/5, MRR, held-out errors, baseline comparison)
- Reports (top candidate rankings, summary artifact references)
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Session, relationship
from sqlalchemy import event as sqlalchemy_event
from .database import Base
from ...domain.history_contracts import AppendOnlyViolationError


def utc_now() -> datetime:
    """Return timezone-aware current UTC datetime."""
    return datetime.now(timezone.utc)


def generate_version_token() -> str:
    """Generate a unique version token for optimistic locking."""
    return f"vt-{uuid.uuid4().hex[:12]}"



class RunModel(Base):
    """Discovery run record tracking run lifecycle, limits, and terminal status."""

    __tablename__ = "runs"

    run_id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    domain = Column(String(64), nullable=False, default="default")
    status = Column(String(32), nullable=False, default="pending")  # pending, running, completed, budget_exhausted, cancelled, failed
    execution_mode = Column(String(32), nullable=False, default="local")  # local, nodus_remote
    sampling_strategy = Column(String(64), nullable=False, default="active_disagreement")
    stop_reason = Column(String(255), nullable=True)
    
    # Configured run-level limits. Legacy names are retained for API
    # compatibility; ``budget_config_json`` is the canonical validated snapshot.
    max_queries = Column(Integer, nullable=True)
    max_evaluations = Column(Integer, nullable=True)
    max_runtime_seconds = Column(Integer, nullable=True)
    spend_ceiling_usd = Column(Float, nullable=True)
    max_concurrent_remote_tasks = Column(Integer, nullable=True)
    max_generated_hypotheses = Column(Integer, nullable=True)
    budget_config_json = Column(JSON, nullable=True)
    deadline_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships preserving history with foreign-key RESTRICT protections
    hypotheses = relationship("HypothesisModel", back_populates="run", passive_deletes=True)
    observations = relationship("ObservationModel", back_populates="run", passive_deletes=True)
    experiments = relationship("CandidateExperimentModel", back_populates="run", passive_deletes=True)
    tasks = relationship("ComputeTaskModel", back_populates="run", passive_deletes=True)
    usage_records = relationship("UsageRecordModel", back_populates="run", passive_deletes=True)
    budget_usage = relationship(
        "RunBudgetUsageModel",
        back_populates="run",
        uselist=False,
        passive_deletes=True,
    )
    budget_reservations = relationship(
        "BudgetReservationModel", back_populates="run", passive_deletes=True
    )
    audit_events = relationship("AuditEventModel", back_populates="run", passive_deletes=True)
    reports = relationship("ReportModel", back_populates="run", passive_deletes=True)
    benchmark_results = relationship("BenchmarkResultModel", back_populates="run", passive_deletes=True)

    __table_args__ = (
        Index("idx_runs_status", "status"),
        Index("idx_runs_created_at", "created_at"),
    )


class HypothesisModel(Base):
    """Stable candidate hypothesis entity belonging to a discovery run."""

    __tablename__ = "hypotheses"

    hypothesis_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    model_family = Column(String(64), nullable=False)
    current_revision = Column(Integer, nullable=False, default=1)
    version = Column(Integer, nullable=False, default=1)
    version_token = Column(String(64), nullable=False, default=generate_version_token)
    
    # Scientific & Researcher UI flags
    is_pinned = Column(Boolean, nullable=False, default=False)
    is_excluded = Column(Boolean, nullable=False, default=False)

    
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="hypotheses")
    revisions = relationship("HypothesisRevisionModel", back_populates="hypothesis", passive_deletes=True, order_by="HypothesisRevisionModel.revision_number")

    __table_args__ = (
        Index("idx_hypotheses_run_state", "run_id", "is_excluded", "is_pinned"),
    )


class HypothesisRevisionModel(Base):
    """Immutable hypothesis revision capturing mathematical/symbolic definitions over time.

    Revisions are the scientific record: once written they are never updated.
    ``previous_revision_id`` chains each revision to its predecessor so a
    hypothesis's definition history can be reconstructed exactly (Issue #17).
    ``request_key`` carries the client-supplied idempotency key for a revision
    request; the unique constraint on ``(hypothesis_id, request_key)`` makes a
    replayed edit return the already-committed revision instead of writing a
    duplicate. ``request_fingerprint`` is the canonical fingerprint of every
    semantically relevant request field (formula, parameters, complexity, model
    family, creation source), so replaying a key with a different payload is
    detected even when formula/parameters/complexity happen to match.
    """

    __tablename__ = "hypothesis_revisions"

    revision_id = Column(String(64), primary_key=True)
    hypothesis_id = Column(String(64), ForeignKey("hypotheses.hypothesis_id", ondelete="RESTRICT"), nullable=False)
    revision_number = Column(Integer, nullable=False)
    formula = Column(Text, nullable=False)
    parameters = Column(JSON, nullable=False)  # Weights/coefficients dictionary
    complexity = Column(Integer, nullable=False, default=1)
    creation_source = Column(String(64), nullable=False, default="generator")  # generator, researcher, refit, restoration
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Researcher history provenance (Issue #17)
    #
    # ``previous_revision_id`` is an application-maintained reference to the
    # preceding revision of the same hypothesis. It is deliberately not a
    # self-referential foreign key: SQLite cannot drop a self-referencing table
    # in FK-enforced test teardown, and the chain is only ever written by
    # ``revise_hypothesis_service`` together with the unique revision number and
    # request-key constraints. See docs/researcher-history.md for the guarantee.
    previous_revision_id = Column(String(64), nullable=True)
    change_reason = Column(Text, nullable=True)
    actor = Column(String(64), nullable=True)  # researcher, automated_agent, system
    actor_id = Column(String(128), nullable=True)
    request_key = Column(String(128), nullable=True)
    request_fingerprint = Column(String(96), nullable=True)

    hypothesis = relationship("HypothesisModel", back_populates="revisions")
    evaluations = relationship(
        "EvaluationResultModel",
        back_populates="revision",
        passive_deletes=True,
        foreign_keys="EvaluationResultModel.revision_id",
    )

    __table_args__ = (
        UniqueConstraint("hypothesis_id", "revision_number", name="uq_hypothesis_revision_number"),
        UniqueConstraint("hypothesis_id", "request_key", name="uq_hypothesis_revision_request_key"),
        Index("idx_revisions_hyp_rev", "hypothesis_id", "revision_number"),
    )


class ObservationModel(Base):
    """Observable record returned by an oracle query."""

    __tablename__ = "observations"

    observation_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    input_id = Column(String(64), nullable=False)
    input_values = Column(JSON, nullable=False)
    output_values = Column(JSON, nullable=True)  # Null if query failed or was invalid
    status = Column(String(32), nullable=False, default="success")  # success, invalid_input, oracle_failure, budget_exceeded
    sequence_number = Column(Integer, nullable=False)  # 1-indexed sequence order within the run
    is_held_out = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(64), nullable=True)
    error_message = Column(Text, nullable=True)
    noise_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="observations")
    experiment = relationship("CandidateExperimentModel", back_populates="observation", uselist=False)

    __table_args__ = (
        Index("idx_observations_run_seq", "run_id", "sequence_number"),
        Index("idx_observations_run_heldout", "run_id", "is_held_out"),
    )


class CandidateExperimentModel(Base):
    """Proposed candidate experiment input evaluated by active learning policies."""

    __tablename__ = "candidate_experiments"

    experiment_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    proposed_input = Column(JSON, nullable=False)
    selection_rationale = Column(JSON, nullable=True)  # Disagreement score, candidate predictions, strategy
    execution_status = Column(String(32), nullable=False, default="proposed")  # proposed, scheduled, executed, rejected
    observation_id = Column(String(64), ForeignKey("observations.observation_id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="experiments")
    observation = relationship("ObservationModel", back_populates="experiment")

    __table_args__ = (
        Index("idx_experiments_run_status", "run_id", "execution_status"),
    )


class EvaluationResultModel(Base):
    """Evaluated fitness and predictive performance of a hypothesis revision against an observation set."""

    __tablename__ = "evaluation_results"

    evaluation_id = Column(String(64), primary_key=True)
    revision_id = Column(String(64), ForeignKey("hypothesis_revisions.revision_id", ondelete="RESTRICT"), nullable=False)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(32), nullable=False, default="completed")  # completed, failed, pending
    sample_count = Column(Integer, nullable=False, default=0)
    # Number of observations available at evaluation time. Retained for API
    # compatibility; a count is NOT a dataset identity, because observations can
    # change independently while the count stays equal.
    dataset_version = Column(Integer, nullable=False, default=0)
    # Exact immutable scientific input (Issue #17): deterministic fingerprint of
    # the canonical content of the observations that were used for this
    # evaluation, plus the manifest those inputs were fingerprinted from.
    dataset_snapshot_id = Column(String(96), nullable=True)
    dataset_snapshot = Column(JSON, nullable=True)
    
    # Structured predictive metrics
    metrics = Column(JSON, nullable=False)  # {"mae": 0.045, "rmse": 0.052, ...}
    held_out_metrics = Column(JSON, nullable=True)  # {"mae": 0.082, "rmse": 0.091, ...}
    error_message = Column(Text, nullable=True)
    evaluated_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    revision = relationship("HypothesisRevisionModel", back_populates="evaluations")

    __table_args__ = (
        Index("idx_evaluations_rev_run", "revision_id", "run_id"),
        Index("idx_evaluations_run_evaluated", "run_id", "evaluated_at"),
        Index("idx_evaluations_dataset_snapshot", "dataset_snapshot_id"),
    )


class ComputeTaskModel(Base):
    """Individual compute execution units scheduled locally or via Nodus."""

    __tablename__ = "compute_tasks"

    task_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    candidate_id = Column(String(64), nullable=True)
    execution_mode = Column(String(32), nullable=False, default="local")  # local, nodus_remote, mock
    provider_task_id = Column(String(128), nullable=True)  # Nodus task identifier
    status = Column(String(32), nullable=False, default="pending")  # pending, running, completed, failed, timed_out
    
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)
    error_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="tasks")

    __table_args__ = (
        Index("idx_tasks_run_status", "run_id", "status"),
        Index("idx_tasks_provider_task_id", "provider_task_id"),
    )


class UsageRecordModel(Base):
    """Tracks query counts, evaluations, and cost progression for a run."""

    __tablename__ = "usage_records"

    usage_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    queries_executed = Column(Integer, nullable=False, default=0)
    evaluations_scheduled = Column(Integer, nullable=False, default=0)
    evaluations_completed = Column(Integer, nullable=False, default=0)
    
    # Explicit cost separation (context.md §5)
    estimated_cost_usd = Column(Float, nullable=True)
    live_accruing_cost_usd = Column(Float, nullable=True)
    settled_cost_usd = Column(Float, nullable=True)
    
    is_budget_exhausted = Column(Boolean, nullable=False, default=False)
    exhaustion_reason = Column(String(255), nullable=True)
    recorded_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="usage_records")

    __table_args__ = (
        Index("idx_usage_run_recorded", "run_id", "recorded_at"),
    )


class RunBudgetUsageModel(Base):
    """Authoritative per-run budget counters (Issue #14).

    One row per run. Admission is a single conditional ``UPDATE`` against these
    counters, so the invariant ``admitted <= configured limit`` holds across
    concurrent processes, not only inside one Python process.

    ``*_reserved`` counts admitted work. A reservation that is provably never
    dispatched is released (the counter decreases and ``*_released``
    increments); work whose outcome is ambiguous stays reserved and is charged
    conservatively.
    """

    __tablename__ = "run_budget_usage"

    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True)

    queries_reserved = Column(Integer, nullable=False, default=0)
    queries_completed = Column(Integer, nullable=False, default=0)
    queries_failed = Column(Integer, nullable=False, default=0)
    queries_released = Column(Integer, nullable=False, default=0)

    evaluations_reserved = Column(Integer, nullable=False, default=0)
    evaluations_completed = Column(Integer, nullable=False, default=0)
    evaluations_failed = Column(Integer, nullable=False, default=0)
    evaluations_released = Column(Integer, nullable=False, default=0)

    hypotheses_generated = Column(Integer, nullable=False, default=0)
    hypotheses_released = Column(Integer, nullable=False, default=0)

    active_remote_tasks = Column(Integer, nullable=False, default=0)

    # Explicit cost separation. NULL means unavailable/not yet known; it is
    # never coerced to 0.0.
    estimated_cost_usd = Column(Float, nullable=True)
    accruing_cost_usd = Column(Float, nullable=True)
    settled_cost_usd = Column(Float, nullable=True)
    cost_data_state = Column(String(32), nullable=False, default="not_started")

    is_exhausted = Column(Boolean, nullable=False, default=False)
    stop_reason = Column(String(64), nullable=True)
    exhausted_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="budget_usage")

    __table_args__ = (
        Index("idx_budget_usage_exhausted", "is_exhausted"),
    )


class BudgetReservationModel(Base):
    """Durable admission ledger for one logical resource operation.

    ``operation_id`` makes reservation idempotent: duplicate worker delivery of
    the same logical operation cannot consume capacity twice. A unique
    constraint on ``(run_id, resource, operation_id)`` is the idempotency key.

    States:
      reserved  — admitted, outcome not yet recorded
      committed — outcome recorded (completed or failed)
      released  — provably never dispatched; capacity returned
      ambiguous — dispatched but the outcome is unknown; capacity stays charged
    """

    __tablename__ = "budget_reservations"

    reservation_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False)
    resource = Column(String(32), nullable=False)  # oracle_query, candidate_evaluation, remote_task, hypothesis
    operation_id = Column(String(128), nullable=False)
    state = Column(String(16), nullable=False, default="reserved")
    quantity = Column(Integer, nullable=False, default=1)
    details_json = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)

    run = relationship("RunModel", back_populates="budget_reservations")

    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "resource",
            "operation_id",
            name="uq_budget_reservation_operation",
        ),
        Index("idx_budget_reservation_run_state", "run_id", "resource", "state"),
    )


class AuditEventModel(Base):
    """Immutable, append-only audit record for researcher and system actions.

    ``actor`` names the actor category (``researcher``, ``automated_agent``,
    ``system``, ``worker``, ...); ``actor_id`` carries the authenticated identity
    when one is available. ``before_json`` / ``after_json`` hold the relevant
    state snapshots, ``reason`` the human-supplied explanation, and
    ``corrects_event_id`` links a correction to the event it supersedes without
    rewriting it (Issue #17).
    """

    __tablename__ = "audit_events"

    event_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    event_type = Column(String(64), nullable=False)  # RUN_STARTED, HYPOTHESIS_PINNED, HYPOTHESIS_EXCLUDED, etc.
    actor = Column(String(64), nullable=False, default="researcher")  # researcher, automated_agent, system, worker
    metadata_json = Column(JSON, nullable=True)
    timestamp = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    # Researcher history provenance (Issue #17)
    hypothesis_id = Column(String(64), nullable=True)
    revision_id = Column(String(64), nullable=True)
    actor_id = Column(String(128), nullable=True)
    correlation_id = Column(String(128), nullable=True)
    before_json = Column(JSON, nullable=True)
    after_json = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    corrects_event_id = Column(String(64), nullable=True)

    run = relationship("RunModel", back_populates="audit_events")

    __table_args__ = (
        Index("idx_audit_run_timestamp", "run_id", "timestamp"),
        Index("idx_audit_event_type", "event_type"),
        Index("idx_audit_hypothesis_timestamp", "hypothesis_id", "timestamp"),
        Index("idx_audit_correlation", "correlation_id"),
    )


class BenchmarkResultModel(Base):
    """Ground-truth evaluation benchmarks comparing discovery runs against targets."""

    __tablename__ = "benchmark_results"

    benchmark_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    benchmark_name = Column(String(128), nullable=False)
    target_label = Column(String(128), nullable=False)
    
    # Ground-truth accuracy metrics
    hit_at_1 = Column(Boolean, nullable=False, default=False)
    hit_at_3 = Column(Boolean, nullable=False, default=False)
    hit_at_5 = Column(Boolean, nullable=False, default=False)
    reciprocal_rank = Column(Float, nullable=False, default=0.0)
    best_candidate_id = Column(String(64), nullable=True)
    queries_to_target = Column(Integer, nullable=True)
    
    held_out_mae = Column(Float, nullable=True)
    held_out_rmse = Column(Float, nullable=True)
    baseline_type = Column(String(64), nullable=True)  # random_sampling, passive_uniform
    efficiency_gain = Column(String(32), nullable=True)  # e.g., "3.2x"
    execution_metadata = Column(JSON, nullable=True)
    executed_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="benchmark_results")

    __table_args__ = (
        Index("idx_benchmarks_run", "run_id"),
        Index("idx_benchmarks_name_executed", "benchmark_name", "executed_at"),
    )


class ReportModel(Base):
    """Generated summary report containing scientific findings and candidate rankings."""

    __tablename__ = "reports"

    report_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("runs.run_id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(32), nullable=False, default="generated")  # pending, generated, failed
    ranked_candidates = Column(JSON, nullable=False)  # Top 3-5 candidates with metrics and evidence
    stop_reason = Column(String(255), nullable=True)
    limitations = Column(Text, nullable=True)
    artifact_uri = Column(String(255), nullable=True)
    generated_at = Column(DateTime(timezone=True), default=utc_now, nullable=False)

    run = relationship("RunModel", back_populates="reports")

    __table_args__ = (
        Index("idx_reports_run_generated", "run_id", "generated_at"),
    )


# ---------------------------------------------------------------------------
# Append-only enforcement (Issue #17)
#
# Historical records are immutable through normal application operations: the
# ORM refuses to UPDATE or DELETE an audit event or a hypothesis revision.
# Corrections append a new record instead. This is an ORM/session guard, not a
# database permission grant; raw SQL executed outside the ORM is not
# intercepted, and that limitation is documented in docs/researcher-history.md.
# ---------------------------------------------------------------------------

_IMMUTABLE_HISTORY_MODELS = (AuditEventModel, HypothesisRevisionModel)


@sqlalchemy_event.listens_for(Session, "before_flush")
def _reject_history_mutation(session: Session, flush_context, instances) -> None:
    """Fail a flush that would modify or delete an append-only record."""
    for obj in session.deleted:
        if isinstance(obj, _IMMUTABLE_HISTORY_MODELS):
            raise AppendOnlyViolationError(
                f"{type(obj).__name__} records are append-only and cannot be deleted."
            )
    for obj in session.dirty:
        if isinstance(obj, _IMMUTABLE_HISTORY_MODELS) and session.is_modified(
            obj, include_collections=False
        ):
            raise AppendOnlyViolationError(
                f"{type(obj).__name__} records are append-only and cannot be modified; "
                "append a new record that references the original instead."
            )
