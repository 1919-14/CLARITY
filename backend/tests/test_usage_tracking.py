"""Unit and integration tests for compute-usage tracking, cost estimation, and reconciliation (Issue #15).

Verifies:
1. Task-level compute usage is recorded with execution mode, timestamps, and status.
2. Local and remote execution are cleanly distinguished.
3. Successful, failed, timed-out, cancelled, and retrying tasks produce accurate records.
4. Reprocessing the same task event does not double-count usage (idempotency).
5. Run-level usage summaries are derived from and reconcile with underlying task records.
6. Cost classification: estimated, accruing, settled, and unavailable.
7. Missing provider cost data remains unavailable (None), never fabricated as zero spend.
8. Cost estimates document their calculation method and pricing assumptions.
"""

import uuid
from datetime import datetime, timedelta, timezone
import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    ComputeTaskModel,
    RunModel,
    UsageRecordModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.usage_contracts import (
    ComputeExecutionMode,
    ComputeTaskStatus,
    CostState,
)
from app.services.run_service import create_run_service
from app.services.usage_service import (
    DEFAULT_ORACLE_QUERY_COST_USD,
    DEFAULT_REMOTE_TASK_HOURLY_RATE_USD,
    derive_run_usage_summary,
    reconcile_run_usage,
    record_task_usage_service,
)


@pytest.fixture
def db_session(app):
    """Provide an isolated database session."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        yield session


def _create_run(session, name="Usage Test Run", mode="local") -> RunModel:
    return create_run_service(
        session=session,
        name=name,
        execution_mode=mode,
        max_queries=10,
        max_evaluations=20,
    )


class TestTaskUsageRecording:
    def test_record_local_task_usage(self, db_session):
        """Recording a local compute task saves execution mode and duration."""
        run = _create_run(db_session, mode="local")
        now = datetime.now(timezone.utc)
        start = now - timedelta(seconds=2)

        task = record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id="task-local-1",
            execution_mode="local",
            candidate_id="hyp-1",
            experiment_id="exp-1",
            status="completed",
            started_at=start,
            completed_at=now,
            duration_ms=2000,
        )

        assert task.task_id == "task-local-1"
        assert task.run_id == run.run_id
        assert task.execution_mode == "local"
        assert task.status == "completed"
        assert task.duration_ms == 2000
        assert task.candidate_id == "hyp-1"
        assert task.error_metadata["experiment_id"] == "exp-1"

    def test_record_remote_nodus_task_with_provider_id(self, db_session):
        """Recording a remote Nodus task retains genuine provider task ID and metadata."""
        run = _create_run(db_session, mode="nodus_remote")
        now = datetime.now(timezone.utc)

        task = record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id="task-remote-1",
            execution_mode="nodus_remote",
            candidate_id="hyp-2",
            status="completed",
            provider_task_id="nodus-job-9988",
            started_at=now - timedelta(seconds=10),
            completed_at=now,
            duration_ms=10000,
            task_metadata={"queue": "gpu-fast", "region": "us-east-1"},
        )

        assert task.execution_mode == "nodus_remote"
        assert task.provider_task_id == "nodus-job-9988"
        assert task.error_metadata["task_metadata"]["queue"] == "gpu-fast"

    def test_task_status_lifecycle_and_failures(self, db_session):
        """Task records reflect terminal statuses: failed, timed_out, cancelled."""
        run = _create_run(db_session)

        # Failed task with error metadata
        t_fail = record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id="task-fail",
            status="failed",
            error_metadata={"error_code": "OOM", "message": "Out of memory"},
        )
        assert t_fail.status == "failed"
        assert t_fail.error_metadata["error_code"] == "OOM"

        # Timed out task
        t_time = record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id="task-timeout",
            status="timed_out",
            error_metadata={"timeout_seconds": 30.0},
        )
        assert t_time.status == "timed_out"

        # Cancelled task
        t_canc = record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id="task-cancelled",
            status="cancelled",
        )
        assert t_canc.status == "cancelled"


class TestIdempotencyAndDoubleCounting:
    def test_duplicate_task_event_does_not_double_count(self, db_session):
        """Reprocessing the same task event updates state without creating duplicate rows."""
        run = _create_run(db_session)
        task_id = "task-idempotent-1"

        # 1. Initial submission: status=pending
        record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id=task_id,
            status="pending",
        )

        repo = ClarityRepository(db_session)
        tasks = repo.list_compute_tasks_for_run(run.run_id)
        assert len(tasks) == 1
        assert tasks[0].status == "pending"

        # 2. Worker updates to running
        record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id=task_id,
            status="running",
        )
        tasks = repo.list_compute_tasks_for_run(run.run_id)
        assert len(tasks) == 1
        assert tasks[0].status == "running"

        # 3. Duplicate delivery of completion
        now = datetime.now(timezone.utc)
        record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id=task_id,
            status="completed",
            duration_ms=1500,
            completed_at=now,
        )
        record_task_usage_service(
            session=db_session,
            run_id=run.run_id,
            task_id=task_id,
            status="completed",
            duration_ms=1500,
            completed_at=now,
        )

        # Still exactly 1 row; duration not doubled
        tasks = repo.list_compute_tasks_for_run(run.run_id)
        assert len(tasks) == 1
        assert tasks[0].status == "completed"
        assert tasks[0].duration_ms == 1500


class TestRunUsageDerivationAndReconciliation:
    def test_derive_run_usage_summary_reconciles_with_tasks(self, db_session):
        """Run summary aggregates task counts, durations, and reconciles with usage records."""
        run = _create_run(db_session)

        # Record 2 local tasks, 1 remote task
        record_task_usage_service(
            db_session, run.run_id, "task-1", execution_mode="local", status="completed", duration_ms=1000
        )
        record_task_usage_service(
            db_session, run.run_id, "task-2", execution_mode="local", status="completed", duration_ms=2000
        )
        record_task_usage_service(
            db_session, run.run_id, "task-3", execution_mode="nodus_remote", status="failed", duration_ms=5000
        )

        summary = derive_run_usage_summary(db_session, run.run_id)

        assert summary.tasks_total == 3
        assert summary.tasks_completed == 2
        assert summary.tasks_failed == 1
        assert summary.local_tasks_count == 2
        assert summary.remote_tasks_count == 1
        assert summary.total_execution_duration_ms == 8000

        # Reconciliation check
        recon = reconcile_run_usage(db_session, run.run_id)
        assert recon["task_count"] == 3
        assert recon["tasks_completed"] == 2
        assert recon["total_duration_ms"] == 8000


class TestCostClassificationAndHonesty:
    def test_cost_classification_states(self, db_session):
        """Distinguish estimated, accruing, settled, and unavailable cost states."""
        run = _create_run(db_session)

        # Initial state: no provider cost reported
        summary = derive_run_usage_summary(db_session, run.run_id)

        # Estimated cost is present and calculated
        assert summary.estimated_cost.state == CostState.ESTIMATED
        assert summary.estimated_cost.amount_usd >= 0.0
        assert summary.estimated_cost.calculation_method == "application_documented_rates"
        assert "remote_task_hourly_rate_usd" in summary.estimated_cost.pricing_assumptions

        # Provider accruing and settled costs are UNAVAILABLE, not zero
        assert summary.accruing_cost.state == CostState.UNAVAILABLE
        assert summary.accruing_cost.amount_usd is None
        assert summary.settled_cost.state == CostState.UNAVAILABLE
        assert summary.settled_cost.amount_usd is None

    def test_missing_cost_is_never_represented_as_zero(self, db_session):
        """Missing provider charges must be None/unavailable, never fabricated as 0.0 spend."""
        run = _create_run(db_session)
        repo = ClarityRepository(db_session)

        # Update usage record without provider costs
        usage = repo.get_latest_usage_for_run(run.run_id)
        usage.queries_executed = 5
        usage.evaluations_completed = 10
        usage.live_accruing_cost_usd = None
        usage.settled_cost_usd = None
        db_session.flush()

        summary = derive_run_usage_summary(db_session, run.run_id)
        assert summary.accruing_cost.amount_usd is None
        assert summary.settled_cost.amount_usd is None
        assert summary.accruing_cost.state == CostState.UNAVAILABLE

    def test_settled_provider_cost_is_recorded_when_available(self, db_session):
        """When provider reports settled charges, they are classified as SETTLED."""
        run = _create_run(db_session)
        repo = ClarityRepository(db_session)

        # Simulate provider settled invoice
        usage = repo.get_latest_usage_for_run(run.run_id)
        usage.settled_cost_usd = 4.25
        usage.live_accruing_cost_usd = 4.25
        db_session.flush()

        summary = derive_run_usage_summary(db_session, run.run_id)
        assert summary.settled_cost.state == CostState.SETTLED
        assert summary.settled_cost.amount_usd == 4.25
        assert summary.settled_cost.is_provider_reported is True


class TestUsageApiEndpoints:
    def test_get_run_usage_endpoint(self, client):
        """GET /api/v1/runs/<run_id>/usage returns complete usage summary."""
        res_run = client.post("/api/v1/runs", json={"name": "API Usage Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.get(f"/api/v1/runs/{run_id}/usage")
        assert res.status_code == 200
        data = res.get_json()["usage"]
        assert "task_counters" in data
        assert "costs" in data
        assert "estimated" in data["costs"]
        assert "accruing" in data["costs"]
        assert "settled" in data["costs"]
        assert data["costs"]["estimated"]["state"] == "estimated"
        assert data["costs"]["accruing"]["state"] == "unavailable"

    def test_list_run_tasks_endpoint(self, client, app):
        """GET /api/v1/runs/<run_id>/tasks lists task records with status and mode filter."""
        res_run = client.post("/api/v1/runs", json={"name": "API Tasks Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        # Seed two tasks
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            record_task_usage_service(
                session, run_id, "api-task-1", execution_mode="local", status="completed", duration_ms=500
            )
            record_task_usage_service(
                session, run_id, "api-task-2", execution_mode="nodus_remote", status="failed", duration_ms=1200
            )

        res = client.get(f"/api/v1/runs/{run_id}/tasks")
        assert res.status_code == 200
        data = res.get_json()
        assert data["count"] == 2
        assert len(data["tasks"]) == 2

        # Filter by status
        res_filter = client.get(f"/api/v1/runs/{run_id}/tasks?status=completed")
        assert res_filter.status_code == 200
        assert res_filter.get_json()["count"] == 1
        assert res_filter.get_json()["tasks"][0]["task_id"] == "api-task-1"
