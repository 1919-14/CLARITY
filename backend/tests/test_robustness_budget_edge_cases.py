"""Edge-Case Budget Enforcement Robustness Test Suite (Issue #28 - Scope 3).

Exercises the budget manager and discovery engine with:
1. Exact-limit boundaries (run stopping precisely when the limit is reached).
2. Zero and negative budget allocations (proper validation and rejection).
3. Exhausted budget mid-computation (clean shutdown with partial results preserved).
4. Highly constrained budgets where no useful hypothesis can be confirmed (appropriate status returned).
5. Concurrent or rapid successive budget deductions to ensure consistency.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List

import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.adapters.persistence.database import DatabaseManager
from app.adapters.persistence.models import (
    ObservationModel,
    RunBudgetUsageModel,
    RunModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.budget_contracts import (
    BudgetAdmissionDenied,
    BudgetResource,
    BudgetStopReason,
    BudgetValidationError,
    RunBudgetConfig,
)
from app.domain.contracts import (
    ExperimentInput,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
)
from app.domain.discovery_contracts import (
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryTerminationReason,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.domain.ranking_contracts import RankingPolicyConfig
from app.evaluator.benchmark_cases import LINEAR_BENCHMARK, QUADRATIC_BENCHMARK
from app.evaluator.benchmark_runner import ActiveRandomBenchmarkRunner
from app.services.budget_service import RunBudgetManager
from app.services.run_service import create_run_service


@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'budget-edge-cases.db'}")
    manager.create_all_tables()
    yield manager
    manager.drop_all_tables()


# ==============================================================================
# 1. Exact-Limit Boundaries
# ==============================================================================

class TestExactLimitBoundaries:
    """Validate boundary behavior when exact resource limits are reached."""

    def test_exact_query_limit_boundary_stops_precisely(self, db):
        """When budget is N queries, exactly N queries succeed; N+1 is denied with QUERY_BUDGET_EXHAUSTED."""
        with db.session_scope() as session:
            create_run_service(
                session=session,
                name="Exact Limit Run",
                run_id="run-boundary-01",
                budget=RunBudgetConfig(max_oracle_queries=3),
            )

        budget_mgr = RunBudgetManager(db.session_scope, "run-boundary-01")

        # 1st, 2nd, 3rd reservation succeed
        t1 = budget_mgr.reserve_oracle_query("q1")
        t2 = budget_mgr.reserve_oracle_query("q2")
        t3 = budget_mgr.reserve_oracle_query("q3")

        assert t1
        assert t2
        assert t3

        # 4th reservation is strictly denied
        with pytest.raises(BudgetAdmissionDenied) as exc_info:
            budget_mgr.reserve_oracle_query("q4")

        assert exc_info.value.reason == BudgetStopReason.QUERY_BUDGET_EXHAUSTED
        assert exc_info.value.resource == BudgetResource.ORACLE_QUERY

        st = budget_mgr.status()
        assert st.oracle_queries.used == 3
        assert st.oracle_queries.remaining == 0

    def test_discovery_engine_stops_at_exact_query_budget(self):
        """Discovery engine with max_query_budget=3 stops after exactly 3 queries."""
        oracle = PolynomialLocalOracle(
            domain_name="1d_polynomial",
            min_x=-5.0,
            max_x=5.0,
            coefficients={"w0": 1.0, "w1": 2.0},
        )
        config = DiscoveryRunConfig(
            max_query_budget=3,
            max_iterations=10,
            domain_bounds=NumericalDomainBounds("x", -5.0, 5.0, "1d_polynomial"),
        )
        engine = DiscoveryEngine(oracle=oracle, config=config)
        result = engine.run(initial_observations=[])

        assert result.query_accounting.attempted_queries == 3
        assert oracle.query_count == 3
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED


# ==============================================================================
# 2. Zero and Negative Budget Allocations
# ==============================================================================

class TestZeroAndNegativeBudgetAllocations:
    """Validate validation and rejection of negative allocations and immediate zero exhaustion."""

    @pytest.mark.parametrize("invalid_field, invalid_val", [
        ("max_oracle_queries", -1),
        ("max_oracle_queries", -10),
        ("max_candidate_evaluations", -5),
        ("max_runtime_seconds", -60),
        ("max_compute_spend_usd", -0.01),
        ("max_concurrent_remote_tasks", -2),
    ])
    def test_negative_budget_allocations_rejected_by_config(
        self, invalid_field: str, invalid_val: Any
    ):
        """Negative numbers in budget config raise BudgetValidationError."""
        config_dict = {
            "max_oracle_queries": 10,
            "max_candidate_evaluations": 20,
            "max_runtime_seconds": 300,
            "max_compute_spend_usd": 5.0,
            "max_concurrent_remote_tasks": 4,
            invalid_field: invalid_val,
        }

        with pytest.raises(BudgetValidationError) as exc_info:
            RunBudgetConfig.from_mapping(config_dict)

        err_dict = exc_info.value.to_dict()
        assert err_dict["code"] == "INVALID_BUDGET"
        assert any(d["field"] == invalid_field for d in exc_info.value.details)

    def test_zero_oracle_queries_budget_denies_first_query_immediately(self, db):
        """Setting max_oracle_queries=0 allows no queries and denies admission immediately."""
        with db.session_scope() as session:
            create_run_service(
                session=session,
                name="Zero Budget Run",
                run_id="run-zero-budget",
                budget=RunBudgetConfig(max_oracle_queries=0),
            )

        budget_mgr = RunBudgetManager(db.session_scope, "run-zero-budget")

        with pytest.raises(BudgetAdmissionDenied) as exc_info:
            budget_mgr.reserve_oracle_query("first_query")

        assert exc_info.value.reason == BudgetStopReason.QUERY_BUDGET_EXHAUSTED
        assert exc_info.value.limit == 0

    def test_discovery_engine_with_zero_budget_exits_immediately(self):
        """DiscoveryEngine configured with max_query_budget=0 halts without calling oracle."""
        oracle = PolynomialLocalOracle(
            domain_name="1d_polynomial",
            min_x=-2.0,
            max_x=2.0,
            coefficients={"w0": 3.0},
        )
        config = DiscoveryRunConfig(
            max_query_budget=0,
            domain_bounds=NumericalDomainBounds("x", -2.0, 2.0, "1d_polynomial"),
        )
        engine = DiscoveryEngine(oracle=oracle, config=config)
        result = engine.run(initial_observations=[])

        assert result.query_accounting.attempted_queries == 0
        assert oracle.query_count == 0
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED


# ==============================================================================
# 3. Exhausted Budget Mid-Computation & Preservation of Partial Results
# ==============================================================================

class TestExhaustedBudgetMidComputation:
    """Validate clean shutdown and preservation of scientific evidence on mid-computation exhaustion."""

    def test_budget_exhausted_mid_run_preserves_completed_observations_and_evaluations(self):
        """When budget exhausts mid-computation, completed observations and ranked candidates are preserved."""
        oracle = PolynomialLocalOracle(
            domain_name="1d_polynomial",
            min_x=-4.0,
            max_x=4.0,
            coefficients={"w0": -1.0, "w1": 0.0, "w2": 0.5},
        )

        init_obs = Observation(
            input_id="init-1",
            observation_id="obs-init-1",
            input_values={"x": 0.0},
            output_values={"y": -1.0},
            status=ObservationStatus.SUCCESS,
        )

        # Budget allows only 2 new queries
        config = DiscoveryRunConfig(
            max_query_budget=2,
            max_iterations=10,
            domain_bounds=NumericalDomainBounds("x", -4.0, 4.0, "1d_polynomial"),
        )

        engine = DiscoveryEngine(oracle=oracle, config=config)
        result = engine.run(initial_observations=[init_obs])

        # 1. Clean stop with budget exhausted
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED

        # 2. All completed observations are preserved: 1 initial + 2 queried = 3
        assert len(result.observations) == 3
        assert result.query_accounting.successful_queries == 2

        # 3. Scientific ranking reflects measured evidence from the preserved observations
        assert result.ranking is not None
        assert len(result.hypotheses) > 0


# ==============================================================================
# 4. Highly Constrained Budgets
# ==============================================================================

class TestHighlyConstrainedBudgets:
    """Validate behavior under severely constrained budgets where confirmation is impossible."""

    def test_insufficient_budget_reports_honest_non_confirmation(self):
        """Under a 1-query budget on a quadratic system, discovery reports non-convergence honestly."""
        runner = ActiveRandomBenchmarkRunner()
        trial = runner.run_single_trial(
            QUADRATIC_BENCHMARK,
            strategy="random_baseline",
            seed=42,
            max_budget=1,
        )

        assert trial.queries_used <= 1
        assert trial.target_reached is False
        assert trial.termination_reason in (
            DiscoveryTerminationReason.BUDGET_EXHAUSTED.value,
            "budget_exhausted",
        )
        # MRR must not be fabricated as 1.0
        assert trial.mrr < 1.0


# ==============================================================================
# 5. Concurrent Budget Deductions Consistency
# ==============================================================================

class TestConcurrentBudgetDeductions:
    """Validate race condition immunity under rapid concurrent resource reservations."""

    def test_concurrent_reservations_never_exceed_configured_limit(self, db):
        """When 20 concurrent threads compete for 5 slots, exactly 5 succeed and 15 are denied."""
        with db.session_scope() as session:
            create_run_service(
                session=session,
                name="Concurrent Budget Test",
                run_id="run-concurrency-budget",
                budget=RunBudgetConfig(max_oracle_queries=5),
            )

        budget_mgr = RunBudgetManager(db.session_scope, "run-concurrency-budget")
        successes = []
        denials = []
        lock = threading.Lock()

        def try_reserve(idx: int):
            try:
                budget_mgr.reserve_oracle_query(f"thread-query-{idx}")
                with lock:
                    successes.append(idx)
            except BudgetAdmissionDenied:
                with lock:
                    denials.append(idx)

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(try_reserve, i) for i in range(20)]
            for f in as_completed(futures):
                f.result()

        assert len(successes) == 5
        assert len(denials) == 15

        # Verify the database state has exactly 5 queries used
        st = budget_mgr.status()
        assert st.oracle_queries.used == 5
        assert st.oracle_queries.remaining == 0
