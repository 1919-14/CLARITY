"""API v1 tests for the Issue #14 per-run budget contract."""

import pytest


def _create(client, **payload):
    return client.post("/api/v1/runs", json=payload)


def _make_run(client, **payload):
    payload.setdefault("name", "Budget API Run")
    res = _create(client, **payload)
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["run"]


class TestCreateRunBudget:
    def test_nested_budget_is_validated_and_persisted(self, client):
        run = _make_run(
            client,
            budget={
                "max_oracle_queries": 5,
                "max_candidate_evaluations": 20,
                "max_concurrent_remote_tasks": 2,
                "max_runtime_seconds": 120,
                "max_generated_hypotheses": 7,
                "max_compute_spend_usd": 1.25,
            },
        )
        assert run["max_queries"] == 5
        assert run["max_evaluations"] == 20
        assert run["max_concurrent_remote_tasks"] == 2
        assert run["max_runtime_seconds"] == 120
        assert run["max_generated_hypotheses"] == 7
        assert run["spend_ceiling_usd"] == 1.25
        assert run["budget"]["max_oracle_queries"] == 5
        assert run["budget"]["schema_version"] == "1.0"

    def test_defaults_apply_when_budget_omitted(self, client):
        run = _make_run(client)
        assert run["max_queries"] == run["budget"]["max_oracle_queries"]
        assert run["budget"]["max_oracle_queries"] == 10
        assert run["budget"]["max_candidate_evaluations"] == 100
        assert run["budget"]["max_concurrent_remote_tasks"] == 4
        assert run["budget"]["max_runtime_seconds"] == 3600
        assert run["budget"]["max_generated_hypotheses"] is None
        assert run["budget"]["max_compute_spend_usd"] is None

    def test_zero_is_accepted_and_meaningful(self, client):
        run = _make_run(
            client,
            budget={
                "max_oracle_queries": 0,
                "max_candidate_evaluations": 0,
                "max_concurrent_remote_tasks": 0,
                "max_runtime_seconds": 0,
            },
        )
        assert run["budget"]["max_oracle_queries"] == 0
        assert run["budget"]["max_runtime_seconds"] == 0

    def test_legacy_flat_limits_still_work(self, client):
        run = _make_run(client, max_queries=12, max_evaluations=34, spend_ceiling_usd=2.5)
        assert run["max_queries"] == 12
        assert run["budget"]["max_oracle_queries"] == 12
        assert run["budget"]["max_candidate_evaluations"] == 34
        assert run["budget"]["max_compute_spend_usd"] == 2.5

    def test_negative_limit_rejected(self, client):
        res = _create(client, name="X", budget={"max_oracle_queries": -1})
        assert res.status_code == 400
        body = res.get_json()
        assert body["details"][0]["field"] == "max_oracle_queries"

    def test_boolean_limit_rejected(self, client):
        res = _create(client, name="X", budget={"max_runtime_seconds": True})
        assert res.status_code == 400
        assert res.get_json()["details"][0]["code"] == "INVALID_TYPE"

    def test_fractional_integer_limit_rejected(self, client):
        res = _create(client, name="X", budget={"max_candidate_evaluations": 2.5})
        assert res.status_code == 400

    def test_non_finite_money_rejected(self, client):
        res = _create(client, name="X", budget={"max_compute_spend_usd": float("inf")})
        assert res.status_code == 400
        assert res.get_json()["details"][0]["code"] == "NOT_FINITE"

    def test_unknown_budget_field_rejected(self, client):
        res = _create(client, name="X", budget={"max_queries_typo": 5})
        assert res.status_code == 400
        assert res.get_json()["details"][0]["code"] == "UNKNOWN_FIELD"

    def test_malformed_budget_rejected(self, client):
        res = _create(client, name="X", budget=["not", "an", "object"])
        assert res.status_code == 400

    def test_conflicting_nested_and_flat_payload_rejected(self, client):
        res = _create(
            client,
            name="X",
            budget={"max_oracle_queries": 5},
            max_queries=5,
        )
        assert res.status_code == 400
        assert res.get_json()["details"][0]["code"] == "CONFLICTING_SETTING"


class TestBudgetStatusEndpoint:
    def test_initial_budget_status(self, client):
        run = _make_run(
            client,
            budget={
                "max_oracle_queries": 5,
                "max_candidate_evaluations": 10,
                "max_concurrent_remote_tasks": 3,
                "max_runtime_seconds": 60,
                "max_generated_hypotheses": 4,
                "max_compute_spend_usd": 2.0,
            },
        )
        res = client.get(f"/api/v1/runs/{run['run_id']}/budget")
        assert res.status_code == 200
        body = res.get_json()
        assert body["run_id"] == run["run_id"]
        budget = body["budget"]
        assert budget["limits"]["max_oracle_queries"] == 5
        assert budget["usage"]["oracle_queries"] == {
            "limit": 5,
            "used": 0,
            "remaining": 5,
            "exhausted": False,
            "enforcement": "application",
            "disabled": False,
        }
        assert budget["usage"]["candidate_evaluations"]["remaining"] == 10
        assert budget["usage"]["generated_hypotheses"]["limit"] == 4
        assert budget["usage"]["max_concurrent_remote_tasks"] == 3
        assert budget["runtime"]["elapsed_seconds"] is None
        assert budget["runtime"]["remaining_seconds"] is None
        # Unavailable money is null, never 0.
        assert budget["cost"]["estimated_cost_usd"] is None
        assert budget["cost"]["settled_cost_usd"] is None
        assert budget["cost"]["accruing_cost_usd"] is None
        assert budget["cost"]["data_state"] == "not_started"
        assert budget["cost"]["provider_hard_cap_supported"] is False
        assert budget["is_exhausted"] is False
        assert budget["stop_reason"] is None
        assert "max_oracle_queries" in budget["zero_semantics"]

    def test_unknown_run_returns_404(self, client):
        res = client.get("/api/v1/runs/run-ghost/budget")
        assert res.status_code == 404

    def test_metrics_includes_budget_summary(self, client):
        run = _make_run(client, budget={"max_oracle_queries": 3})
        res = client.get(f"/api/v1/runs/{run['run_id']}/metrics")
        assert res.status_code == 200
        body = res.get_json()
        assert body["limits"]["max_queries"] == 3
        assert body["budget"]["limits"]["max_oracle_queries"] == 3
        assert body["budget"]["usage"]["oracle_queries"]["remaining"] == 3

    def test_budget_response_never_leaks_oracle_internals(self, client):
        run = _make_run(client)
        body = client.get(f"/api/v1/runs/{run['run_id']}/budget").get_data(as_text=True)
        assert "ground_truth" not in body
        assert "oracle_formula" not in body
        assert "hidden" not in body


class TestHypothesisBudgetEndpoint:
    def test_hypothesis_limit_persists_stop_reason_and_409(self, client):
        run = _make_run(client, budget={"max_generated_hypotheses": 1})
        run_id = run["run_id"]

        first = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "linear", "formula": "y = x"},
        )
        assert first.status_code == 201

        second = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "linear", "formula": "y = 2*x"},
        )
        assert second.status_code == 409
        assert second.get_json()["details"]["reason"] == "HYPOTHESIS_LIMIT_REACHED"

        run_after = client.get(f"/api/v1/runs/{run_id}").get_json()["run"]
        assert run_after["status"] == "budget_exhausted"
        assert run_after["stop_reason"] == "HYPOTHESIS_LIMIT_REACHED"

        budget = client.get(f"/api/v1/runs/{run_id}/budget").get_json()["budget"]
        assert budget["is_exhausted"] is True
        assert budget["stop_reason"] == "HYPOTHESIS_LIMIT_REACHED"
        assert budget["usage"]["generated_hypotheses"]["used"] == 1
        assert budget["usage"]["generated_hypotheses"]["remaining"] == 0

    def test_zero_hypothesis_limit_blocks_the_first_proposal(self, client):
        run = _make_run(client, budget={"max_generated_hypotheses": 0})
        res = client.post(
            f"/api/v1/runs/{run['run_id']}/hypotheses",
            json={"model_family": "linear", "formula": "y = x"},
        )
        assert res.status_code == 409
        budget = client.get(f"/api/v1/runs/{run['run_id']}/budget").get_json()["budget"]
        assert budget["stop_reason"] == "HYPOTHESIS_LIMIT_REACHED"
        assert budget["usage"]["generated_hypotheses"]["used"] == 0

    def test_unlimited_hypotheses_still_work(self, client):
        run = _make_run(client, budget={"max_generated_hypotheses": None})
        for index in range(3):
            res = client.post(
                f"/api/v1/runs/{run['run_id']}/hypotheses",
                json={"model_family": "linear", "formula": f"y = {index}*x"},
            )
            assert res.status_code == 201
        budget = client.get(f"/api/v1/runs/{run['run_id']}/budget").get_json()["budget"]
        assert budget["usage"]["generated_hypotheses"]["limit"] is None
        assert budget["usage"]["generated_hypotheses"]["used"] == 3
        assert budget["is_exhausted"] is False
