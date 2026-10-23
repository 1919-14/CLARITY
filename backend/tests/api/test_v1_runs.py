"""API v1 tests: Discovery run endpoints.

Covers:
  POST   /api/v1/runs                      — creation, validation, conflicts
  GET    /api/v1/runs                      — listing, pagination, status filter
  GET    /api/v1/runs/<id>                 — retrieval, 404
  POST   /api/v1/runs/<id>/cancel          — cancellation, terminal-state conflict
  GET    /api/v1/runs/<id>/metrics         — usage semantics, null vs 0
  GET    /api/v1/runs/<id>/report          — pending report, generated report
  GET    /api/v1/runs/<id>/benchmarks      — benchmark results
  GET    /api/v1/runs/<id>/observations    — observation listing with pagination
  GET    /api/v1/runs/<id>/hypotheses      — hypothesis listing per run
  POST   /api/v1/runs/<id>/hypotheses      — researcher hypothesis proposal
"""

import uuid
import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    BenchmarkResultModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from app.adapters.persistence.repository import ClarityRepository


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_run(client, name="Test Run", **kwargs):
    payload = {"name": name}
    payload.update(kwargs)
    res = client.post("/api/v1/runs", json=payload)
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["run"]


# ---------------------------------------------------------------------------
# POST /api/v1/runs — creation
# ---------------------------------------------------------------------------

class TestCreateRun:
    def test_success_minimal(self, client):
        """Minimal payload creates run with 201 and expected defaults."""
        res = client.post("/api/v1/runs", json={"name": "Minimal Run"})
        assert res.status_code == 201
        data = res.get_json()
        assert "run" in data
        run = data["run"]
        assert run["name"] == "Minimal Run"
        assert run["status"] in {"pending", "queued"}  # 'queued' is the canonical lifecycle state
        assert run["run_id"].startswith("run-")
        assert run["created_at"] is not None

    def test_success_full_payload(self, client):
        """Full payload is correctly persisted."""
        res = client.post("/api/v1/runs", json={
            "name": "1D Polynomial Exploration",
            "domain": "1d_polynomial",
            "execution_mode": "local",
            "sampling_strategy": "active_disagreement",
            "max_queries": 50,
            "max_evaluations": 200,
            "max_runtime_seconds": 3600,
            "spend_ceiling_usd": 5.0,
        })
        assert res.status_code == 201
        run = res.get_json()["run"]
        assert run["domain"] == "1d_polynomial"
        assert run["max_queries"] == 50
        assert run["spend_ceiling_usd"] == 5.0

    def test_caller_supplied_run_id_accepted(self, client):
        """A caller-provided run_id is honoured."""
        res = client.post("/api/v1/runs", json={"name": "Fixed", "run_id": "run-fixed-abc"})
        assert res.status_code == 201
        assert res.get_json()["run"]["run_id"] == "run-fixed-abc"

    def test_non_json_payload_rejected(self, client):
        """Non-JSON content-type returns 400."""
        res = client.post("/api/v1/runs", data="not json", content_type="text/plain")
        assert res.status_code == 400

    def test_missing_name_rejected(self, client):
        """Missing 'name' field returns 400 mentioning 'name'."""
        res = client.post("/api/v1/runs", json={"domain": "test"})
        assert res.status_code == 400
        assert "name" in res.get_json()["message"]

    def test_empty_name_rejected(self, client):
        """Whitespace-only name is rejected."""
        res = client.post("/api/v1/runs", json={"name": "   "})
        assert res.status_code == 400

    def test_invalid_execution_mode_rejected(self, client):
        """Unknown execution_mode returns 400."""
        res = client.post("/api/v1/runs", json={"name": "X", "execution_mode": "cloud_magic"})
        assert res.status_code == 400

    def test_negative_max_queries_rejected(self, client):
        """Non-positive max_queries returns 400."""
        res = client.post("/api/v1/runs", json={"name": "X", "max_queries": -1})
        assert res.status_code == 400

    def test_float_max_queries_rejected(self, client):
        """Float max_queries (would silently truncate) is rejected."""
        res = client.post("/api/v1/runs", json={"name": "X", "max_queries": 10.5})
        assert res.status_code == 400

    def test_negative_spend_ceiling_rejected(self, client):
        """Negative spend_ceiling_usd returns 400."""
        res = client.post("/api/v1/runs", json={"name": "X", "spend_ceiling_usd": -1.0})
        assert res.status_code == 400

    def test_duplicate_run_id_returns_409(self, client):
        """Duplicate run_id returns 409 Conflict."""
        client.post("/api/v1/runs", json={"name": "First", "run_id": "run-dup"})
        res = client.post("/api/v1/runs", json={"name": "Second", "run_id": "run-dup"})
        assert res.status_code == 409
        data = res.get_json()
        assert data["error"] == "Conflict"

    def test_error_response_structure(self, client):
        """Error responses include 'error', 'message', and 'status_code'."""
        res = client.post("/api/v1/runs", json={"domain": "missing-name"})
        data = res.get_json()
        assert "error" in data
        assert "message" in data
        assert "status_code" in data
        assert data["status_code"] == 400

    def test_create_run_atomic_rollback_on_registration_failure(self, client, app):
        """When lifecycle manager initialization fails, DB row is rolled back and no orphan persists."""
        from unittest.mock import patch
        from app.domain.run_lifecycle import DuplicateExecutionError

        mgr = app.config["LIFECYCLE_MANAGER"]
        run_id = "run-fail-init-test"

        with patch.object(mgr, "initialize_run", side_effect=DuplicateExecutionError("Simulated registration error")):
            res = client.post("/api/v1/runs", json={"name": "Atomic Fail", "run_id": run_id})
            assert res.status_code == 500

        # Verify no orphan row exists in DB
        get_res = client.get(f"/api/v1/runs/{run_id}")
        assert get_res.status_code == 404
        assert run_id not in mgr._progress_registry

# ---------------------------------------------------------------------------
# GET /api/v1/runs — listing
# ---------------------------------------------------------------------------

class TestListRuns:
    def test_empty_list(self, client):
        """Returns empty list when no runs exist."""
        res = client.get("/api/v1/runs")
        assert res.status_code == 200
        data = res.get_json()
        assert data["runs"] == []
        assert data["count"] == 0

    def test_lists_created_runs(self, client):
        """Created runs appear in the listing."""
        _make_run(client, "Run A")
        _make_run(client, "Run B")
        res = client.get("/api/v1/runs")
        assert res.status_code == 200
        assert res.get_json()["count"] == 2

    def test_pagination_limit(self, client):
        """Limit restricts the number of runs returned."""
        for i in range(5):
            _make_run(client, f"Run {i}")
        res = client.get("/api/v1/runs?limit=3")
        data = res.get_json()
        assert data["count"] == 3
        assert data["limit"] == 3

    def test_pagination_offset(self, client):
        """Offset skips runs."""
        for i in range(4):
            _make_run(client, f"Run {i}")
        res = client.get("/api/v1/runs?limit=10&offset=3")
        assert res.get_json()["count"] == 1

    def test_limit_capped_at_200(self, client):
        """Limit cannot exceed 200 (capped silently)."""
        res = client.get("/api/v1/runs?limit=500")
        assert res.status_code == 200
        assert res.get_json()["limit"] == 200

    def test_invalid_pagination_params(self, client):
        """Non-integer pagination params return 400."""
        res = client.get("/api/v1/runs?limit=abc")
        assert res.status_code == 400

    def test_status_filter(self, client, app):
        """Status filter returns only matching runs."""
        run = _make_run(client, "Pending Run")
        # Cancel one run via the API
        client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        # Create another pending run
        _make_run(client, "Another Pending")
        res = client.get("/api/v1/runs?status=cancelled")
        data = res.get_json()
        assert data["count"] == 1
        assert data["runs"][0]["status"] == "cancelled"

    def test_invalid_status_filter_rejected(self, client):
        """Unknown status filter value returns 400."""
        res = client.get("/api/v1/runs?status=flying")
        assert res.status_code == 400

    def test_response_includes_pagination_metadata(self, client):
        """Response includes limit, offset, count, and status_filter."""
        res = client.get("/api/v1/runs?limit=10&offset=0&status=pending")
        data = res.get_json()
        assert "limit" in data
        assert "offset" in data
        assert "count" in data
        assert "status_filter" in data


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>
# ---------------------------------------------------------------------------

class TestGetRun:
    def test_returns_existing_run(self, client):
        """Returns 200 with run data for a valid run_id."""
        run = _make_run(client, "Detail Run")
        res = client.get(f"/api/v1/runs/{run['run_id']}")
        assert res.status_code == 200
        assert res.get_json()["run"]["run_id"] == run["run_id"]

    def test_unknown_run_returns_404(self, client):
        """Unknown run_id returns 404 with documented error structure."""
        res = client.get("/api/v1/runs/run-does-not-exist")
        assert res.status_code == 404
        data = res.get_json()
        assert data["error"] == "Not Found"
        assert "status_code" in data


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>/status — lifecycle progress snapshot
# ---------------------------------------------------------------------------

class TestRunStatus:
    def test_returns_status_for_created_run(self, client):
        """GET /status returns 200 with a progress snapshot for a created run."""
        run = _make_run(client, "Status Run")
        res = client.get(f"/api/v1/runs/{run['run_id']}/status")
        assert res.status_code == 200
        data = res.get_json()
        assert "status" in data
        progress = data["status"]
        assert progress["run_id"] == run["run_id"]
        assert progress["status"] in {"queued", "pending"}

    def test_status_has_required_keys(self, client):
        """Progress snapshot contains all documented fields."""
        run = _make_run(client, "Key Check Run")
        res = client.get(f"/api/v1/runs/{run['run_id']}/status")
        progress = res.get_json()["status"]
        for key in ("run_id", "status", "phase", "queries_used", "queries_remaining",
                    "evaluations_completed", "elapsed_seconds", "started_at",
                    "completed_at", "current_activity", "stop_reason",
                    "failure_details", "last_updated_at"):
            assert key in progress, f"Missing key: {key}"

    def test_status_has_no_fabricated_percentage(self, client):
        """Progress snapshot must not contain a percentage/percent key."""
        run = _make_run(client, "No Percent Run")
        res = client.get(f"/api/v1/runs/{run['run_id']}/status")
        progress = res.get_json()["status"]
        for key in progress:
            assert "percent" not in key.lower()

    def test_status_unknown_run_returns_404(self, client):
        """Unknown run_id returns 404."""
        res = client.get("/api/v1/runs/run-ghost/status")
        assert res.status_code == 404



# ---------------------------------------------------------------------------
# POST /api/v1/runs/<run_id>/cancel
# ---------------------------------------------------------------------------

class TestCancelRun:
    def test_cancels_pending_run(self, client):
        """Cancelling a pending run returns 200 with 'cancelled' status."""
        run = _make_run(client, "Cancellable")
        res = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        assert res.status_code == 200
        data = res.get_json()
        assert data["run"]["status"] == "cancelled"
        assert "message" in data

    def test_cancel_with_reason(self, client):
        """Custom cancellation reason is persisted."""
        run = _make_run(client, "Reason Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/cancel",
            json={"reason": "Budget exceeded manually"},
        )
        assert res.status_code == 200
        assert "Budget exceeded" in res.get_json()["run"]["stop_reason"]

    def test_cancel_already_cancelled_returns_409(self, client):
        """Re-cancelling a cancelled run returns 409 Conflict."""
        run = _make_run(client, "Already Done")
        client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        res = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        assert res.status_code == 409

    def test_cancel_unknown_run_returns_404(self, client):
        """Cancelling a non-existent run returns 404."""
        res = client.post("/api/v1/runs/run-ghost/cancel")
        assert res.status_code == 404

    def test_cancel_running_run_returns_202_cancelling(self, client):
        """Cancelling an active running run transitions to 'cancelling' and returns 202 Accepted."""
        run = _make_run(client, "Active Running")
        # Start run first
        start_res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert start_res.status_code == 200
        assert start_res.get_json()["run"]["status"] == "running"

        # Request cancellation
        cancel_res = client.post(f"/api/v1/runs/{run['run_id']}/cancel")
        assert cancel_res.status_code == 202
        data = cancel_res.get_json()
        assert data["run"]["status"] == "cancelling"
        assert data["status"]["status"] == "cancelling"
        assert "in progress" in data["message"].lower()

    def test_cancel_records_actual_prior_status_in_audit_event(self, client, app):
        """Audit event records the true previous status (not 'cancelled') in metadata_json."""
        from app.adapters.persistence.database import get_db_manager
        from app.adapters.persistence.repository import ClarityRepository

        run = _make_run(client, "Audit Status Check")
        # Initial run status is "queued" / "pending"
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/cancel",
            json={"reason": "User stop"},
        )
        assert res.status_code == 200

        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            events = repo.list_audit_events_for_run(run["run_id"])
            cancel_events = [e for e in events if e.event_type == "RUN_CANCELLED"]
            assert len(cancel_events) == 1
            meta = cancel_events[0].metadata_json
            assert meta.get("previous_status") in {"pending", "queued"}
            assert meta.get("stop_reason") == "User stop"


# ---------------------------------------------------------------------------
# POST /api/v1/runs/<run_id>/start
# ---------------------------------------------------------------------------

class TestStartRun:
    def test_start_queued_run_returns_200(self, client):
        """Starting a queued run returns 200 with status running."""
        run = _make_run(client, "Start Test")
        res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert res.status_code == 200
        data = res.get_json()
        assert data["run"]["status"] == "running"
        assert data["status"]["status"] == "running"

    def test_start_already_running_returns_409(self, client):
        """Starting an already active run returns 409 Conflict."""
        run = _make_run(client, "Already Running")
        client.post(f"/api/v1/runs/{run['run_id']}/start")
        res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert res.status_code == 409

    def test_start_unknown_run_returns_404(self, client):
        """Starting an unknown run returns 404."""
        res = client.post("/api/v1/runs/run-ghost/start")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>/metrics
# ---------------------------------------------------------------------------

class TestRunMetrics:
    def test_returns_metrics_with_initial_usage(self, client):
        """Metrics returns structured limits and usage after creation."""
        run = _make_run(client, "Metrics Run", max_queries=20, spend_ceiling_usd=1.0)
        res = client.get(f"/api/v1/runs/{run['run_id']}/metrics")
        assert res.status_code == 200
        data = res.get_json()
        assert data["run_id"] == run["run_id"]
        assert "limits" in data
        assert "usage" in data
        assert data["limits"]["max_queries"] == 20
        assert data["limits"]["spend_ceiling_usd"] == 1.0

    def test_metrics_distinguishes_measured_zero_from_missing(self, client):
        """Usage shows 0 (not None) for counts when a usage record exists."""
        run = _make_run(client, "Zero Metrics")
        res = client.get(f"/api/v1/runs/{run['run_id']}/metrics")
        usage = res.get_json()["usage"]
        # Measured zero is 0, not None
        assert usage["queries_executed"] == 0
        assert usage["evaluations_scheduled"] == 0
        # settled_cost_usd is None until provider confirms
        assert usage["settled_cost_usd"] is None

    def test_metrics_unknown_run_returns_404(self, client):
        """Unknown run_id returns 404."""
        res = client.get("/api/v1/runs/run-xyz/metrics")
        assert res.status_code == 404

    def test_metrics_no_oracle_secrets(self, client):
        """Metrics response never contains oracle formula or ground truth."""
        run = _make_run(client, "Secrets Check")
        res = client.get(f"/api/v1/runs/{run['run_id']}/metrics")
        body = res.get_data(as_text=True)
        assert "ground_truth" not in body
        assert "oracle_formula" not in body
        assert "hidden" not in body


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>/report
# ---------------------------------------------------------------------------

class TestRunReport:
    def test_pending_report_returns_null_with_status(self, client):
        """When no report exists, returns report: null with status 'pending'."""
        run = _make_run(client, "No Report")
        res = client.get(f"/api/v1/runs/{run['run_id']}/report")
        assert res.status_code == 200
        data = res.get_json()
        assert data["report"] is None
        assert data["status"] == "pending"
        assert "message" in data

    def test_generated_report_returned(self, client, app):
        """When a report exists, it is returned fully serialized."""
        run = _make_run(client, "With Report")
        run_id = run["run_id"]
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.save_report(ReportModel(
                report_id=f"rpt-{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                status="generated",
                ranked_candidates=[{"rank": 1, "hypothesis_id": "hyp-abc"}],
                stop_reason="budget_exhausted",
                limitations="Limited observations.",
            ))

        res = client.get(f"/api/v1/runs/{run_id}/report")
        assert res.status_code == 200
        data = res.get_json()
        assert data["report"]["status"] == "generated"
        assert len(data["report"]["ranked_candidates"]) == 1

    def test_report_unknown_run_returns_404(self, client):
        """Unknown run returns 404."""
        res = client.get("/api/v1/runs/run-nope/report")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>/benchmarks
# ---------------------------------------------------------------------------

class TestRunBenchmarks:
    def test_empty_benchmarks(self, client):
        """Returns empty list when no benchmarks exist."""
        run = _make_run(client, "No Benchmarks")
        res = client.get(f"/api/v1/runs/{run['run_id']}/benchmarks")
        assert res.status_code == 200
        data = res.get_json()
        assert data["benchmarks"] == []
        assert data["count"] == 0

    def test_benchmark_returned(self, client, app):
        """Recorded benchmark appears in the response."""
        run = _make_run(client, "Benchmark Run")
        run_id = run["run_id"]
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.record_benchmark_result(BenchmarkResultModel(
                benchmark_id=f"bm-{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                benchmark_name="active_vs_random",
                target_label="linear_1d",
                hit_at_1=True,
                hit_at_3=True,
                hit_at_5=True,
                reciprocal_rank=1.0,
            ))

        res = client.get(f"/api/v1/runs/{run_id}/benchmarks")
        data = res.get_json()
        assert data["count"] == 1
        bm = data["benchmarks"][0]
        assert bm["benchmark_name"] == "active_vs_random"
        assert bm["hit_at_1"] is True

    def test_benchmarks_unknown_run_returns_404(self, client):
        res = client.get("/api/v1/runs/run-ghost/benchmarks")
        assert res.status_code == 404

    def test_post_benchmark_rejects_injected_target_formula_or_parameters(self, client):
        """Requirement 4: Disallow client-injected target_formula and target_parameters via API."""
        run = _make_run(client, "Inject Test Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/benchmarks",
            json={
                "benchmark_case": "polynomial_linear_benchmark",
                "target_formula": "y = 999.0",
                "target_parameters": {"degree": 0},
            },
        )
        assert res.status_code == 400
        data = res.get_json()
        assert "Client-supplied ground truth" in data["message"]

    def test_post_benchmark_rejects_injected_target_label(self, client):
        """Requirement 4: Disallow client-injected target_label."""
        run = _make_run(client, "Inject Label Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/benchmarks",
            json={"target_label": "fabricated_target"},
        )
        assert res.status_code == 400
        data = res.get_json()
        assert "Client-supplied ground truth" in data["message"]

    def test_post_benchmark_rejects_injected_coefficients(self, client):
        """Requirement 4: Disallow client-injected coefficients."""
        run = _make_run(client, "Inject Coeffs Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/benchmarks",
            json={
                "benchmark_case": "polynomial_linear_benchmark",
                "coefficients": {"c0": 1.0, "c1": 2.0},
            },
        )
        assert res.status_code == 400
        data = res.get_json()
        assert "Client-supplied ground truth" in data["message"]

    def test_post_benchmark_requires_registered_benchmark_case(self, client):
        """Requirement 4: Missing or invalid benchmark_case returns 400."""
        run = _make_run(client, "No Case Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/benchmarks",
            json={},
        )
        assert res.status_code == 400
        assert "Field 'benchmark_case' is required" in res.get_json()["message"]

    def test_post_benchmark_rejects_unsafe_metadata(self, client):
        """Requirement 5: API rejects metadata containing secret seed or forbidden keys."""
        run = _make_run(client, "Unsafe Meta Run")
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/benchmarks",
            json={
                "benchmark_case": "polynomial_linear_benchmark",
                "metadata": {"audit": {"secret_token": "leak"}},
            },
        )
        assert res.status_code == 400
        assert "Unsafe metadata" in res.get_json()["message"]


# ---------------------------------------------------------------------------
# GET /api/v1/runs/<run_id>/observations
# ---------------------------------------------------------------------------

class TestRunObservations:
    def _seed_observation(self, app, run_id, seq, held_out=False):
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.add_observation(ObservationModel(
                observation_id=f"obs-{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                input_id=f"inp-{seq}",
                input_values={"x": seq},
                output_values={"y": seq * 2},
                status="success",
                sequence_number=seq,
                is_held_out=held_out,
            ))

    def test_empty_observations(self, client):
        """No observations returns empty list."""
        run = _make_run(client, "No Obs")
        res = client.get(f"/api/v1/runs/{run['run_id']}/observations")
        assert res.status_code == 200
        assert res.get_json()["count"] == 0

    def test_observations_returned(self, client, app):
        """Seeded observations are returned."""
        run = _make_run(client, "Obs Run")
        run_id = run["run_id"]
        self._seed_observation(app, run_id, 1)
        self._seed_observation(app, run_id, 2)
        res = client.get(f"/api/v1/runs/{run_id}/observations")
        data = res.get_json()
        assert data["count"] == 2

    def test_only_training_filter(self, client, app):
        """only_training=true excludes held-out observations."""
        run = _make_run(client, "Filter Obs")
        run_id = run["run_id"]
        self._seed_observation(app, run_id, 1, held_out=False)
        self._seed_observation(app, run_id, 2, held_out=True)
        res = client.get(f"/api/v1/runs/{run_id}/observations?only_training=true")
        assert res.get_json()["count"] == 1
        assert res.get_json()["observations"][0]["is_held_out"] is False

    def test_pagination(self, client, app):
        """Observations respect limit and offset."""
        run = _make_run(client, "Paged Obs")
        run_id = run["run_id"]
        for i in range(5):
            self._seed_observation(app, run_id, i + 1)
        res = client.get(f"/api/v1/runs/{run_id}/observations?limit=2&offset=2")
        data = res.get_json()
        assert data["count"] == 2
        assert data["total"] == 5

    def test_observation_pagination_is_database_bounded(self, client, app):
        """Prove that observation pagination pushes limit and offset into the database query and does not load full table into memory."""
        from unittest.mock import patch
        from sqlalchemy import event
        from app.adapters.persistence.database import get_db_manager
        from app.adapters.persistence.repository import ClarityRepository

        run = _make_run(client, "DB Bounded Obs")
        run_id = run["run_id"]
        for i in range(10):
            self._seed_observation(app, run_id, i + 1)

        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)

        executed_statements = []

        def capture_sql(conn, cursor, statement, parameters, context, executemany):
            executed_statements.append((statement, parameters))

        event.listen(db.engine, "before_cursor_execute", capture_sql)
        orig_list = ClarityRepository.list_observations_for_run
        orig_count = ClarityRepository.count_observations_for_run

        list_calls = []
        count_calls = []

        def spy_list(self_repo, r_id, only_training=False, limit=None, offset=None):
            list_calls.append({"run_id": r_id, "only_training": only_training, "limit": limit, "offset": offset})
            return orig_list(self_repo, r_id, only_training=only_training, limit=limit, offset=offset)

        def spy_count(self_repo, r_id, only_training=False):
            count_calls.append({"run_id": r_id, "only_training": only_training})
            return orig_count(self_repo, r_id, only_training=only_training)

        with patch.object(ClarityRepository, "list_observations_for_run", spy_list), \
             patch.object(ClarityRepository, "count_observations_for_run", spy_count):
            try:
                res = client.get(f"/api/v1/runs/{run_id}/observations?limit=3&offset=4")
            finally:
                event.remove(db.engine, "before_cursor_execute", capture_sql)

        assert res.status_code == 200
        data = res.get_json()
        assert data["count"] == 3
        assert data["total"] == 10
        assert data["limit"] == 3
        assert data["offset"] == 4

        # Verify repository was invoked with bounded limit & offset (not unbounded loading)
        assert len(list_calls) == 1
        assert list_calls[0] == {"run_id": run_id, "only_training": False, "limit": 3, "offset": 4}
        assert len(count_calls) == 1
        assert count_calls[0] == {"run_id": run_id, "only_training": False}

        # Verify SQL statements sent to SQLite contained LIMIT and OFFSET
        obs_sqls = [stmt for stmt, params in executed_statements if "observations" in stmt.lower() and "select" in stmt.lower()]
        assert any("count(" in stmt.lower() for stmt in obs_sqls), f"Missing count query in: {obs_sqls}"
        paged_sqls = [stmt for stmt in obs_sqls if "limit" in stmt.lower() and "offset" in stmt.lower()]
        assert len(paged_sqls) >= 1, f"Expected bounded SQL with LIMIT and OFFSET, found: {obs_sqls}"
    def test_no_oracle_internals_in_observations(self, client, app):
        """Observation response never contains oracle formula or hidden ground truth."""
        run = _make_run(client, "Secret Check")
        self._seed_observation(app, run["run_id"], 1)
        res = client.get(f"/api/v1/runs/{run['run_id']}/observations")
        body = res.get_data(as_text=True)
        assert "oracle_formula" not in body
        assert "ground_truth" not in body
        assert "hidden" not in body

    def test_observations_unknown_run_returns_404(self, client):
        res = client.get("/api/v1/runs/run-x/observations")
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Hypothesis endpoints via runs
# ---------------------------------------------------------------------------

class TestRunHypotheses:
    def test_propose_hypothesis_success(self, client):
        """POST hypothesis returns 201 with serialized hypothesis and audit trail."""
        run = _make_run(client, "Hyp Run")
        res = client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json={
            "model_family": "linear",
            "formula": "y = a*x + b",
            "parameters": {"a": 2.0, "b": 0.5},
            "complexity": 2,
        })
        assert res.status_code == 201
        data = res.get_json()
        hyp = data["hypothesis"]
        assert hyp["model_family"] == "linear"
        assert hyp["latest_revision"]["formula"] == "y = a*x + b"
        assert hyp["is_pinned"] is False
        assert hyp["is_excluded"] is False

    def test_propose_hypothesis_missing_formula_rejected(self, client):
        """Missing formula returns 400."""
        run = _make_run(client, "Hyp Validate")
        res = client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json={
            "model_family": "linear",
        })
        assert res.status_code == 400
        assert "formula" in res.get_json()["message"]

    def test_propose_hypothesis_non_dict_parameters_rejected(self, client):
        """Non-dict parameters returns 400."""
        run = _make_run(client, "Hyp Params")
        res = client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json={
            "model_family": "linear",
            "formula": "y = x",
            "parameters": [1, 2],
        })
        assert res.status_code == 400

    def test_propose_hypothesis_invalid_complexity_rejected(self, client):
        """Complexity < 1 returns 400."""
        run = _make_run(client, "Hyp Complexity")
        res = client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json={
            "model_family": "linear",
            "formula": "y = x",
            "complexity": 0,
        })
        assert res.status_code == 400

    def test_duplicate_hypothesis_id_returns_409(self, client):
        """Duplicate hypothesis_id returns 409."""
        run = _make_run(client, "Dup Hyp")
        payload = {
            "model_family": "linear",
            "formula": "y = x",
            "hypothesis_id": "hyp-dup-001",
        }
        client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json=payload)
        res = client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json=payload)
        assert res.status_code == 409

    def test_propose_hypothesis_unknown_run_returns_404(self, client):
        """Proposing to an unknown run returns 404."""
        res = client.post("/api/v1/runs/run-none/hypotheses", json={
            "model_family": "linear", "formula": "y = x"
        })
        assert res.status_code == 404

    def test_list_hypotheses_for_run(self, client):
        """GET hypotheses returns list of hypotheses for a run."""
        run = _make_run(client, "List Hyp")
        client.post(f"/api/v1/runs/{run['run_id']}/hypotheses", json={
            "model_family": "quadratic", "formula": "y = x^2"
        })
        res = client.get(f"/api/v1/runs/{run['run_id']}/hypotheses")
        assert res.status_code == 200
        data = res.get_json()
        assert data["count"] == 1
        assert data["hypotheses"][0]["model_family"] == "quadratic"

    def test_list_hypotheses_exclude_filter(self, client):
        """include_excluded=false hides excluded hypotheses."""
        run = _make_run(client, "Exclude Filter")
        run_id = run["run_id"]
        res = client.post(f"/api/v1/runs/{run_id}/hypotheses", json={
            "model_family": "linear", "formula": "y = x"
        })
        hyp_id = res.get_json()["hypothesis"]["hypothesis_id"]

        # Exclude via hypotheses resource endpoint
        client.patch(f"/api/v1/hypotheses/{hyp_id}", json={"is_excluded": True})

        res_all = client.get(f"/api/v1/runs/{run_id}/hypotheses?include_excluded=true")
        res_filtered = client.get(f"/api/v1/runs/{run_id}/hypotheses?include_excluded=false")
        assert res_all.get_json()["count"] == 1
        assert res_filtered.get_json()["count"] == 0
