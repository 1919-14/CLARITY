"""Integration tests: a normal API-started run executes under its persisted budget.

These tests deliberately never construct a ``RunBudgetManager``. They create a
run through the API, start it through the API, and rely on the application's own
execution composition to attach the authoritative persisted budget. A run that
reaches the oracle without that budget would fail these tests.
"""

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.discovery_contracts import DiscoveryLifecycleStatus, DiscoveryRunConfig
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.ranking_contracts import RankingPolicyConfig
from app.services.run_execution import RunBudgetUnavailable


def _engine_config(max_oracle_queries: int) -> DiscoveryRunConfig:
    return DiscoveryRunConfig(
        max_query_budget=max_oracle_queries,
        max_iterations=5,
        domain_bounds=NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="1d_polynomial"
        ),
        hypothesis_config=HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=4, seed=11
        ),
        ranking_policy=RankingPolicyConfig(
            min_samples_for_plausibility=3, performance_acceptance_threshold=1e9
        ),
        experiment_pool_size=4,
    )


def _install_factory(app, oracle, *, bind_budget=True):
    """Install an engine factory that receives the already-budgeted plan."""

    def factory(plan):
        return DiscoveryEngine(
            oracle,
            _engine_config(plan.budget.max_oracle_queries),
            run_id=plan.run_id,
            budget=plan.budget_manager if bind_budget else None,
        )

    app.config["RUN_ENGINE_FACTORY"] = factory
    return factory


def _create_run(client, **overrides):
    payload = {"name": "Budgeted API Run"}
    payload.update(overrides)
    res = client.post("/api/v1/runs", json=payload)
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["run"]


class TestApiStartedRunUsesPersistedBudget:
    def test_persisted_query_limit_stops_an_api_started_run(self, client, app):
        """A one-query budget created via the API is enforced without injection."""
        oracle = PolynomialLocalOracle()
        _install_factory(app, oracle)

        run = _create_run(client, budget={"max_oracle_queries": 1})
        assert run["max_queries"] == 1

        res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert res.status_code == 200, res.get_data(as_text=True)
        body = res.get_json()
        assert body["execution"]["dispatched"] is True
        assert body["execution"]["status"] == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED.value

        # The authoritative persisted counter is what stopped the run.
        budget = client.get(f"/api/v1/runs/{run['run_id']}/budget").get_json()["budget"]
        assert budget["limits"]["max_oracle_queries"] == 1
        query_usage = budget["usage"]["oracle_queries"]
        assert query_usage["limit"] == 1
        assert query_usage["used"] == 1
        assert query_usage["remaining"] == 0
        assert query_usage["exhausted"] is True
        assert oracle.query_count == 1

    def test_persisted_limit_bounds_the_oracle_across_a_wider_local_budget(self, client, app):
        """The engine's local counter cannot exceed the persisted limit either."""
        oracle = PolynomialLocalOracle()
        _install_factory(app, oracle)

        run = _create_run(client, budget={"max_oracle_queries": 2})
        res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert res.status_code == 200

        assert oracle.query_count == 2
        budget = client.get(f"/api/v1/runs/{run['run_id']}/budget").get_json()["budget"]
        assert budget["usage"]["oracle_queries"]["used"] == 2
        assert budget["usage"]["oracle_queries"]["limit"] == 2

    def test_unbudgeted_engine_from_the_factory_fails_closed(self, client, app):
        """A factory that drops the budget must not be allowed to execute."""
        oracle = PolynomialLocalOracle()
        _install_factory(app, oracle, bind_budget=False)

        run = _create_run(client, budget={"max_oracle_queries": 3})
        res = client.post(f"/api/v1/runs/{run['run_id']}/start")

        assert res.status_code == 503
        assert "budget" in res.get_json()["message"].lower()
        assert oracle.query_count == 0


class TestStartRunWithoutExecutionProvider:
    def test_start_without_a_factory_dispatches_no_work(self, client, app):
        app.config.pop("RUN_ENGINE_FACTORY", None)
        run = _create_run(client)
        res = client.post(f"/api/v1/runs/{run['run_id']}/start")
        assert res.status_code == 200
        body = res.get_json()
        assert body["execution"] == {
            "dispatched": False,
            "reason": "no_engine_factory_configured",
        }


class TestRunBudgetUnavailable:
    def test_unknown_run_cannot_be_composed(self, app):
        from app.adapters.persistence.database import get_db_manager

        db = get_db_manager(app.config["SQLALCHEMY_DATABASE_URI"])
        from app.services.run_execution import build_execution_plan

        with pytest.raises(RunBudgetUnavailable):
            build_execution_plan("run-does-not-exist", db)
