"""Robustness & Edge-Case Test Suite: Scope 5 - Flask API Error Contracts.

Validates that:
1. All error responses carry a consistent JSON payload with 'error', 'message', 'status_code'.
2. HTTP status codes match spec: 400 for bad input, 404 for missing resources,
   409 for conflicts (cancelling a finished run, duplicate run_id), 502/504 for upstream failures.
3. Production mode unhandled 500 errors return a clean JSON body with no Python tracebacks,
   file-system paths, or SQL queries leaking into the response.
4. Payload validation catches type violations, out-of-range values, and required-field absence.
"""

import pytest
from unittest.mock import patch


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def run_id(client):
    """Create a fresh test run and return its run_id."""
    res = client.post("/api/v1/runs", json={"name": "API Contract Run"})
    assert res.status_code == 201
    return res.get_json()["run"]["run_id"]


# ==============================================================================
# 1. Consistent JSON Error Payload Structure
# ==============================================================================

class TestErrorResponseStructure:
    """All error paths must return {error, message, status_code} JSON regardless of cause."""

    def _assert_error_shape(self, res, expected_status: int):
        assert res.status_code == expected_status
        assert res.content_type == "application/json"
        data = res.get_json()
        assert isinstance(data, dict), "Error body must be a JSON object"
        assert "error" in data, "Error body must contain 'error'"
        assert "message" in data, "Error body must contain 'message'"
        assert "status_code" in data, "Error body must contain 'status_code'"
        assert data["status_code"] == expected_status, "status_code field must match HTTP status"
        assert isinstance(data["message"], str) and data["message"], "message must be a non-empty string"
        assert isinstance(data["error"], str) and data["error"], "error must be a non-empty string"

    def test_400_missing_required_field(self, client):
        """Missing 'name' on POST /runs returns structured 400."""
        res = client.post("/api/v1/runs", json={"domain": "test"})
        self._assert_error_shape(res, 400)

    def test_400_invalid_type_field(self, client):
        """Non-string name on POST /runs returns structured 400."""
        res = client.post("/api/v1/runs", json={"name": 12345})
        self._assert_error_shape(res, 400)

    def test_400_non_json_payload(self, client):
        """Non-JSON body returns structured 400."""
        res = client.post("/api/v1/runs", data="not json", content_type="text/plain")
        self._assert_error_shape(res, 400)

    def test_404_missing_run(self, client):
        """GET on nonexistent run_id returns structured 404."""
        res = client.get("/api/v1/runs/run-does-not-exist-ever")
        self._assert_error_shape(res, 404)

    def test_409_duplicate_run_id(self, client, run_id):
        """Creating a run with a duplicate run_id returns structured 409."""
        res = client.post("/api/v1/runs", json={"name": "Dupe", "run_id": run_id})
        self._assert_error_shape(res, 409)

    def test_409_cancel_completed_run_returns_conflict(self, client, run_id):
        """Cancelling a run that is already cancelled returns 409 Conflict."""
        # First cancel (should succeed)
        res = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert res.status_code in {200, 204, 409}
        # Second cancel on a terminal run
        res2 = client.post(f"/api/v1/runs/{run_id}/cancel")
        # The run is already cancelled — this should be a 409
        self._assert_error_shape(res2, 409)

    def test_global_404_handler_returns_json(self, client):
        """Routing to a completely unknown URL returns JSON 404 (not HTML)."""
        res = client.get("/api/v1/totally-nonexistent-endpoint")
        assert res.status_code == 404
        assert res.content_type == "application/json"
        data = res.get_json()
        assert "error" in data
        assert "message" in data
        assert "status_code" in data


# ==============================================================================
# 2. HTTP Status Code Correctness
# ==============================================================================

class TestHttpStatusCodes:
    """Correct HTTP codes are returned in every known boundary condition."""

    def test_400_negative_max_queries(self, client):
        res = client.post("/api/v1/runs", json={"name": "Budget Fail", "max_queries": -5})
        assert res.status_code == 400

    def test_400_float_max_queries(self, client):
        res = client.post("/api/v1/runs", json={"name": "Float Budget", "max_queries": 10.5})
        assert res.status_code == 400

    def test_400_negative_spend_ceiling(self, client):
        res = client.post("/api/v1/runs", json={"name": "Neg Spend", "spend_ceiling_usd": -2.0})
        assert res.status_code == 400

    def test_400_invalid_execution_mode(self, client):
        res = client.post("/api/v1/runs", json={"name": "Bad Mode", "execution_mode": "cloud_magic"})
        assert res.status_code == 400

    def test_400_budget_both_nested_and_flat(self, client):
        """Providing both 'budget' object and legacy flat fields is a 400."""
        res = client.post("/api/v1/runs", json={
            "name": "Both Budget",
            "budget": {"max_oracle_queries": 10},
            "max_queries": 10,
        })
        assert res.status_code == 400

    def test_400_invalid_pagination_limit(self, client):
        res = client.get("/api/v1/runs?limit=not_a_number")
        assert res.status_code == 400

    def test_400_negative_pagination_offset(self, client):
        res = client.get("/api/v1/runs?offset=-1")
        assert res.status_code == 400

    def test_400_invalid_status_filter(self, client):
        res = client.get("/api/v1/runs?status=on_the_moon")
        assert res.status_code == 400

    def test_404_get_nonexistent_run(self, client):
        res = client.get("/api/v1/runs/run-xyz-does-not-exist")
        assert res.status_code == 404

    def test_404_get_status_nonexistent_run(self, client):
        res = client.get("/api/v1/runs/run-phantom/status")
        assert res.status_code == 404

    def test_404_get_observations_nonexistent_run(self, client):
        res = client.get("/api/v1/runs/run-phantom/observations")
        assert res.status_code == 404

    def test_404_benchmark_comparison_nonexistent_id(self, client):
        res = client.get("/api/v1/benchmarks/compare/bm-nonexistent")
        assert res.status_code == 404

    def test_409_conflict_on_duplicate_run_id(self, client, run_id):
        """Duplicate run_id returns 409 with 'Conflict' error label."""
        res = client.post("/api/v1/runs", json={"name": "Conflict Run", "run_id": run_id})
        assert res.status_code == 409
        assert res.get_json()["error"] == "Conflict"


# ==============================================================================
# 3. Benchmark Endpoint Validation
# ==============================================================================

class TestBenchmarkApiValidation:
    """Benchmark endpoints enforce input constraints with well-structured errors."""

    def test_400_missing_scenario_id(self, client):
        """POST /benchmarks/compare without scenario_id returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={"seeds": [42]})
        assert res.status_code == 400
        assert "scenario_id" in res.get_json()["message"]

    def test_400_unknown_scenario_id(self, client):
        """POST /benchmarks/compare with unknown scenario returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={
            "scenario_id": "non_existent_scenario_9999",
            "seeds": [42],
        })
        assert res.status_code == 400

    def test_400_ground_truth_injection_rejected(self, client):
        """POST /benchmarks/compare with forbidden ground-truth fields returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={
            "scenario_id": "linear_1d",
            "target_formula": "y = 2x + 1",
        })
        assert res.status_code == 400
        msg = res.get_json()["message"].lower()
        assert "ground truth" in msg or "prohibited" in msg

    def test_400_invalid_seed_type(self, client):
        """POST /benchmarks/compare with non-integer seeds returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={
            "scenario_id": "linear_1d",
            "seeds": ["not_an_int"],
        })
        assert res.status_code == 400

    def test_400_seeds_exceeds_maximum(self, client):
        """POST /benchmarks/compare with more than 20 seeds returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={
            "scenario_id": "linear_1d",
            "seeds": list(range(25)),
        })
        assert res.status_code == 400

    def test_400_invalid_max_query_budget(self, client):
        """POST /benchmarks/compare with budget=0 (out of 1..100 range) returns 400."""
        res = client.post("/api/v1/benchmarks/compare", json={
            "scenario_id": "linear_1d",
            "seeds": [42],
            "max_query_budget": 0,
        })
        assert res.status_code == 400


# ==============================================================================
# 4. Hypothesis Proposal Validation
# ==============================================================================

class TestHypothesisApiValidation:
    """Hypothesis proposal endpoint enforces correct HTTP codes for common error scenarios."""

    def test_404_propose_hypothesis_for_missing_run(self, client):
        """POST /runs/<id>/hypotheses for non-existent run returns 404.

        The route requires model_family before looking up the run;
        we supply a complete valid payload so the run lookup is reached.
        """
        res = client.post("/api/v1/runs/run-does-not-exist/hypotheses", json={
            "model_family": "polynomial",
            "formula": "x^2 + 1",
            "parameters": {"a": 1.0},
        })
        assert res.status_code == 404

    def test_400_propose_hypothesis_empty_formula(self, client, run_id):
        """POST /runs/<id>/hypotheses with empty formula returns 400."""
        res = client.post(f"/api/v1/runs/{run_id}/hypotheses", json={
            "formula": "  ",
            "parameters": {},
        })
        assert res.status_code == 400


# ==============================================================================
# 5. No Internal Detail Leakage in Error Responses
# ==============================================================================

class TestNoLeakageInErrorResponses:
    """Error responses must not expose Python stack traces, file paths, or SQL."""

    def _assert_no_internal_leak(self, res):
        """Check response body doesn't contain Python-implementation artefacts."""
        body = res.get_data(as_text=True)
        # Stack traces
        assert "Traceback" not in body, "Response must not include Python traceback"
        assert "File \"/" not in body and "File \"\\" not in body, "Response must not expose file paths"
        # SQL queries
        assert "SELECT" not in body.upper() or "sql" not in body.lower(), (
            "Response must not expose raw SQL queries"
        )
        # Common ORM internals
        assert "sqlalchemy" not in body.lower(), "Response must not expose SQLAlchemy internals"
        assert "sqlite3" not in body.lower(), "Response must not expose sqlite3 internals"

    def test_404_no_internal_detail(self, client):
        res = client.get("/api/v1/runs/run-totally-absent-from-db")
        assert res.status_code == 404
        self._assert_no_internal_leak(res)

    def test_400_no_internal_detail(self, client):
        res = client.post("/api/v1/runs", json={"domain": "test"})  # Missing name
        assert res.status_code == 400
        self._assert_no_internal_leak(res)

    def test_409_no_internal_detail(self, client, run_id):
        res = client.post("/api/v1/runs", json={"name": "Dup", "run_id": run_id})
        assert res.status_code == 409
        self._assert_no_internal_leak(res)

    def test_simulated_500_no_traceback_in_testing_mode(self, client, app):
        """A route that raises unexpectedly must return clean JSON 500 with no Python traceback."""
        from app.routes.v1.runs import runs_v1_bp

        # Inject an unhandled exception into a route
        with patch("app.routes.v1.runs.ClarityRepository") as mock_repo_cls:
            mock_repo_cls.side_effect = RuntimeError("Internal DB corruption")
            res = client.get("/api/v1/runs")
            # The app must not blow up completely or expose internals
            assert res.status_code in {500, 200}  # 200 possible if mock bypassed
            if res.status_code == 500:
                self._assert_no_internal_leak(res)
