"""Tests for Flask API endpoints for discovery runs, hypotheses, observations, and metrics (Issue #19).

Verifies:
1. Run creation (POST /api/runs) with validation of name, limits, and domain.
2. Run listing (GET /api/runs) with pagination (limit and offset).
3. Run retrieval (GET /api/runs/<run_id>) including 404 handling.
4. Run cancellation (POST /api/runs/<run_id>/cancel) and terminal conflict prevention (409).
5. Hypothesis listing (GET /api/runs/<run_id>/hypotheses) with revision history and latest evaluation.
6. Researcher hypothesis proposal (POST /api/runs/<run_id>/hypotheses) and audit trail logging.
7. Non-destructive researcher interaction (PATCH /api/runs/<run_id>/hypotheses/<hyp_id>) for pinning/exclusion.
8. Observation listing (GET /api/runs/<run_id>/observations) with training vs held-out filtering.
9. Metrics, usage, and budget progression (GET /api/runs/<run_id>/metrics).
10. Scientific report retrieval (GET /api/runs/<run_id>/report) for pending and generated states.
11. Benchmark result retrieval (GET /api/runs/<run_id>/benchmarks).
12. Security: ensuring no hidden ground-truth or internal secrets leak into any response.
"""

import pytest
from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    BenchmarkResultModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from app.adapters.persistence.repository import ClarityRepository


# ==============================================================================
# 1. Run Creation & Validation Tests
# ==============================================================================

def test_create_run_success(client):
    """Verify POST /api/runs creates a run and returns 201 with serialized run data."""
    payload = {
        "name": "1D Polynomial Exploration",
        "domain": "1d_polynomial",
        "execution_mode": "local",
        "sampling_strategy": "active_disagreement",
        "max_queries": 25,
        "max_evaluations": 100,
        "max_runtime_seconds": 300,
        "spend_ceiling_usd": 15.5,
    }
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 201

    data = response.get_json()
    assert "run" in data
    run = data["run"]
    assert run["name"] == "1D Polynomial Exploration"
    assert run["domain"] == "1d_polynomial"
    assert run["status"] == "pending"
    assert run["max_queries"] == 25
    assert run["spend_ceiling_usd"] == 15.5
    assert run["run_id"].startswith("run-")
    assert run["created_at"] is not None


def test_create_run_validation_errors(client):
    """Verify POST /api/runs rejects invalid payloads with 400 Bad Request."""
    # 1. Missing name
    res = client.post("/api/runs", json={"domain": "test"})
    assert res.status_code == 400
    assert "name" in res.get_json()["message"]

    # 2. Empty name
    res = client.post("/api/runs", json={"name": "   "})
    assert res.status_code == 400

    # 3. Non-JSON request
    res = client.post("/api/runs", data="not json", content_type="text/plain")
    assert res.status_code == 400

    # 4. Negative numeric limits
    res = client.post("/api/runs", json={"name": "Test", "max_queries": -5})
    assert res.status_code == 400

    res = client.post("/api/runs", json={"name": "Test", "spend_ceiling_usd": -1.0})
    assert res.status_code == 400

    # 5. Invalid execution mode
    res = client.post("/api/runs", json={"name": "Test", "execution_mode": "invalid_mode"})
    assert res.status_code == 400


def test_create_run_duplicate_id_rejected(client):
    """Verify duplicate run_id produces 409 Conflict."""
    payload = {"run_id": "run-fixed-id", "name": "Fixed Run"}
    res1 = client.post("/api/runs", json=payload)
    assert res1.status_code == 201

    res2 = client.post("/api/runs", json=payload)
    assert res2.status_code == 409


# ==============================================================================
# 2. Run Retrieval & Listing Tests
# ==============================================================================

def test_get_run_success_and_not_found(client):
    """Verify GET /api/runs/<run_id> returns 200 or 404."""
    create_res = client.post("/api/runs", json={"name": "Target Run"})
    run_id = create_res.get_json()["run"]["run_id"]

    # Retrieve existing
    res = client.get(f"/api/runs/{run_id}")
    assert res.status_code == 200
    assert res.get_json()["run"]["run_id"] == run_id

    # Non-existent run
    res_404 = client.get("/api/runs/non-existent-run-id")
    assert res_404.status_code == 404
    assert res_404.get_json()["error"] == "Not Found"


def test_list_runs_pagination(client):
    """Verify GET /api/runs supports limit and offset pagination."""
    for i in range(5):
        client.post("/api/runs", json={"name": f"Run {i}"})

    # Page 1
    res = client.get("/api/runs?limit=2&offset=0")
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["runs"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0

    # Page 2
    res2 = client.get("/api/runs?limit=2&offset=2")
    assert res2.status_code == 200
    assert len(res2.get_json()["runs"]) == 2

    # Invalid pagination params
    bad_res = client.get("/api/runs?limit=-1")
    assert bad_res.status_code == 400


# ==============================================================================
# 3. Run Cancellation Tests
# ==============================================================================

def test_cancel_run(client):
    """Verify POST /api/runs/<run_id>/cancel updates status and logs stop_reason."""
    create_res = client.post("/api/runs", json={"name": "Run to Cancel"})
    run_id = create_res.get_json()["run"]["run_id"]

    cancel_res = client.post(f"/api/runs/{run_id}/cancel", json={"reason": "Manual operator stop"})
    assert cancel_res.status_code == 200
    data = cancel_res.get_json()
    assert data["run"]["status"] == "cancelled"
    assert data["run"]["stop_reason"] == "Manual operator stop"

    # Attempting to cancel already cancelled run returns 409
    res_repeat = client.post(f"/api/runs/{run_id}/cancel")
    assert res_repeat.status_code == 409


# ==============================================================================
# 4. Hypothesis Proposal & Listing Tests
# ==============================================================================

def test_propose_and_list_hypotheses(client, app):
    """Verify proposing a hypothesis (POST) and retrieving it with revisions (GET)."""
    run_res = client.post("/api/runs", json={"name": "Hypothesis Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    # 1. Propose hypothesis
    hyp_payload = {
        "model_family": "1d_polynomial",
        "formula": "y = 0.5x^2 - 1.2x + 3.0",
        "parameters": {"c2": 0.5, "c1": -1.2, "c0": 3.0},
        "complexity": 3,
    }
    prop_res = client.post(f"/api/runs/{run_id}/hypotheses", json=hyp_payload)
    assert prop_res.status_code == 201

    hyp_data = prop_res.get_json()["hypothesis"]
    assert hyp_data["model_family"] == "1d_polynomial"
    assert hyp_data["current_revision"] == 1
    assert hyp_data["is_pinned"] is False
    assert hyp_data["is_excluded"] is False
    assert hyp_data["latest_revision"]["formula"] == "y = 0.5x^2 - 1.2x + 3.0"
    assert hyp_data["latest_revision"]["creation_source"] == "researcher"

    # 2. Add an evaluation record in DB to test evaluation embedding
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        eval_record = EvaluationResultModel(
            evaluation_id="eval-test-01",
            revision_id=hyp_data["latest_revision"]["revision_id"],
            run_id=run_id,
            status="completed",
            sample_count=10,
            dataset_version=10,
            metrics={"mae": 0.042, "rmse": 0.055},
            held_out_metrics={"mae": 0.081, "rmse": 0.093},
        )
        repo.record_evaluation(eval_record)

    # 3. List hypotheses
    list_res = client.get(f"/api/runs/{run_id}/hypotheses")
    assert list_res.status_code == 200
    hyps = list_res.get_json()["hypotheses"]
    assert len(hyps) == 1
    assert hyps[0]["latest_evaluation"] is not None
    assert hyps[0]["latest_evaluation"]["metrics"]["mae"] == 0.042
    assert hyps[0]["latest_evaluation"]["held_out_metrics"]["mae"] == 0.081


def test_propose_hypothesis_validation_errors(client):
    """Verify validation errors when proposing a hypothesis."""
    run_res = client.post("/api/runs", json={"name": "Validation Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    # Missing formula
    res = client.post(f"/api/runs/{run_id}/hypotheses", json={"model_family": "poly"})
    assert res.status_code == 400

    # Missing model_family
    res = client.post(f"/api/runs/{run_id}/hypotheses", json={"formula": "y = x"})
    assert res.status_code == 400

    # Run not found
    res = client.post("/api/runs/non-existent-run/hypotheses", json={"formula": "y = x", "model_family": "poly"})
    assert res.status_code == 404


# ==============================================================================
# 5. Non-Destructive Researcher Flag Updates (Pin / Exclude)
# ==============================================================================

def test_patch_hypothesis_flags(client):
    """Verify PATCH /api/runs/<run_id>/hypotheses/<hyp_id> updates is_pinned and is_excluded."""
    run_res = client.post("/api/runs", json={"name": "Flag Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    hyp_res = client.post(
        f"/api/runs/{run_id}/hypotheses",
        json={"model_family": "poly", "formula": "y = 2x"},
    )
    hyp_id = hyp_res.get_json()["hypothesis"]["hypothesis_id"]

    # Pin hypothesis
    patch1 = client.patch(f"/api/runs/{run_id}/hypotheses/{hyp_id}", json={"is_pinned": True})
    assert patch1.status_code == 200
    assert patch1.get_json()["hypothesis"]["is_pinned"] is True

    # Exclude hypothesis
    patch2 = client.patch(f"/api/runs/{run_id}/hypotheses/{hyp_id}", json={"is_excluded": True})
    assert patch2.status_code == 200
    assert patch2.get_json()["hypothesis"]["is_excluded"] is True

    # Filter with include_excluded=false
    list_active = client.get(f"/api/runs/{run_id}/hypotheses?include_excluded=false")
    assert len(list_active.get_json()["hypotheses"]) == 0

    # Filter with include_excluded=true
    list_all = client.get(f"/api/runs/{run_id}/hypotheses?include_excluded=true")
    assert len(list_all.get_json()["hypotheses"]) == 1

    # Empty PATCH payload
    bad_patch = client.patch(f"/api/runs/{run_id}/hypotheses/{hyp_id}", json={})
    assert bad_patch.status_code == 400


# ==============================================================================
# 6. Observation Retrieval Tests
# ==============================================================================

def test_get_observations_and_filter_training(client, app):
    """Verify GET /api/runs/<run_id>/observations with training vs held-out filtering."""
    run_res = client.post("/api/runs", json={"name": "Obs Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    # Seed observations in DB
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        obs_train = ObservationModel(
            observation_id="obs-train-01",
            run_id=run_id,
            input_id="inp-01",
            input_values={"x": 1.0},
            output_values={"y": 2.0},
            status="success",
            sequence_number=1,
            is_held_out=False,
        )
        obs_held = ObservationModel(
            observation_id="obs-held-01",
            run_id=run_id,
            input_id="inp-02",
            input_values={"x": 5.0},
            output_values={"y": 10.0},
            status="success",
            sequence_number=2,
            is_held_out=True,
        )
        repo.add_observation(obs_train)
        repo.add_observation(obs_held)

    # 1. Retrieve all observations
    res_all = client.get(f"/api/runs/{run_id}/observations")
    assert res_all.status_code == 200
    assert len(res_all.get_json()["observations"]) == 2

    # 2. Retrieve only training observations
    res_train = client.get(f"/api/runs/{run_id}/observations?only_training=true")
    assert res_train.status_code == 200
    train_list = res_train.get_json()["observations"]
    assert len(train_list) == 1
    assert train_list[0]["observation_id"] == "obs-train-01"


# ==============================================================================
# 7. Metrics & Budget Retrieval Tests
# ==============================================================================

def test_get_metrics_endpoint(client, app):
    """Verify GET /api/runs/<run_id>/metrics returns usage and budget progression."""
    run_res = client.post(
        "/api/runs",
        json={"name": "Metrics Run", "max_queries": 20, "spend_ceiling_usd": 10.0},
    )
    run_id = run_res.get_json()["run"]["run_id"]

    # Update usage record in DB
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        usage = UsageRecordModel(
            usage_id="use-metrics-01",
            run_id=run_id,
            queries_executed=12,
            evaluations_scheduled=30,
            evaluations_completed=28,
            estimated_cost_usd=0.084,
            live_accruing_cost_usd=0.079,
            settled_cost_usd=None,
            is_budget_exhausted=False,
        )
        repo.record_usage(usage)

    res = client.get(f"/api/runs/{run_id}/metrics")
    assert res.status_code == 200
    data = res.get_json()

    assert data["run_id"] == run_id
    assert data["limits"]["max_queries"] == 20
    assert data["limits"]["spend_ceiling_usd"] == 10.0
    assert data["usage"]["queries_executed"] == 12
    assert data["usage"]["evaluations_completed"] == 28
    assert data["usage"]["estimated_cost_usd"] == 0.084
    assert data["usage"]["settled_cost_usd"] is None


# ==============================================================================
# 8. Report & Benchmark Retrieval Tests
# ==============================================================================

def test_get_report_pending_and_generated(client, app):
    """Verify GET /api/runs/<run_id>/report returns pending status then report object."""
    run_res = client.post("/api/runs", json={"name": "Report Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    # 1. Report not yet generated
    res_pending = client.get(f"/api/runs/{run_id}/report")
    assert res_pending.status_code == 200
    assert res_pending.get_json()["report"] is None
    assert res_pending.get_json()["status"] == "pending"

    # 2. Save report in DB
    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        report = ReportModel(
            report_id="rep-001",
            run_id=run_id,
            status="generated",
            ranked_candidates=[{"hypothesis_id": "hyp-01", "rank": 1, "mae": 0.012}],
            stop_reason="target_accuracy_reached",
            limitations="Evaluated within [-10, 10] range.",
            artifact_uri="reports/run-001-summary.json",
        )
        repo.save_report(report)

    # 3. Retrieve generated report
    res_gen = client.get(f"/api/runs/{run_id}/report")
    assert res_gen.status_code == 200
    rep_data = res_gen.get_json()["report"]
    assert rep_data["report_id"] == "rep-001"
    assert rep_data["status"] == "generated"
    assert len(rep_data["ranked_candidates"]) == 1


def test_get_benchmarks_endpoint(client, app):
    """Verify GET /api/runs/<run_id>/benchmarks returns Hit@K and MRR metrics."""
    run_res = client.post("/api/runs", json={"name": "Benchmark Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    db_uri = app.config["SQLALCHEMY_DATABASE_URI"]
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        bench = BenchmarkResultModel(
            benchmark_id="bench-001",
            run_id=run_id,
            benchmark_name="polynomial_cubic_benchmark",
            target_label="target_cubic",
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            reciprocal_rank=1.0,
            best_candidate_id="hyp-target",
            queries_to_target=4,
            held_out_mae=0.012,
            held_out_rmse=0.018,
            efficiency_gain="3.2x",
        )
        repo.record_benchmark_result(bench)

    res = client.get(f"/api/runs/{run_id}/benchmarks")
    assert res.status_code == 200
    benchmarks = res.get_json()["benchmarks"]
    assert len(benchmarks) == 1
    assert benchmarks[0]["hit_at_1"] is True
    assert benchmarks[0]["reciprocal_rank"] == 1.0


# ==============================================================================
# 9. Security & Ground Truth Isolation Tests
# ==============================================================================

def test_ground_truth_leakage_prevented(client, app):
    """Verify that forbidden internal keys (e.g. ground_truth, coefficients_secret) cannot leak via API."""
    run_res = client.post("/api/runs", json={"name": "Leakage Check Run"})
    run_id = run_res.get_json()["run"]["run_id"]

    # Forbidden ground-truth keywords that must never appear in agent-facing responses
    forbidden_keys = [
        "ground_truth",
        "oracle_secret",
        "secret_coefficients",
        "true_function",
        "hidden_rule",
    ]

    endpoints_to_check = [
        f"/api/runs/{run_id}",
        f"/api/runs/{run_id}/hypotheses",
        f"/api/runs/{run_id}/observations",
        f"/api/runs/{run_id}/metrics",
        f"/api/runs/{run_id}/report",
        f"/api/runs/{run_id}/benchmarks",
    ]

    for ep in endpoints_to_check:
        res = client.get(ep)
        assert res.status_code == 200
        text = res.get_data(as_text=True).lower()
        for key in forbidden_keys:
            assert key not in text, f"Forbidden key '{key}' found in response from {ep}!"
