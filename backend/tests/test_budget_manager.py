"""Issue #14 budget manager tests.

Covers configuration validation and zero semantics, exact query boundaries,
cross-thread admission, retry accounting, runtime deadlines, structured
exhaustion, preservation of completed work, cost-state honesty, hypothesis and
evaluation limits, concurrency slots, and stale-reservation recovery.

All tests use an isolated SQLite database; no paid provider call is made.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.persistence.database import DatabaseManager
from app.adapters.persistence.models import (
    AuditEventModel,
    ComputeTaskModel,
    ObservationModel,
    RunBudgetUsageModel,
    RunModel,
)
from app.domain.budget_contracts import (
    DEFAULT_MAX_CANDIDATE_EVALUATIONS,
    DEFAULT_MAX_CONCURRENT_REMOTE_TASKS,
    DEFAULT_MAX_ORACLE_QUERIES,
    DEFAULT_MAX_RUNTIME_SECONDS,
    BudgetAdmissionDenied,
    BudgetReservationReplay,
    BudgetResource,
    BudgetStopReason,
    BudgetValidationError,
    CostDataState,
    CostEnforcement,
    RunBudgetConfig,
)
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.contracts import (
    EvaluationRequest,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.discovery_contracts import (
    ERROR_DUPLICATE_BUDGET_RESERVATION,
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryTerminationReason,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from app.domain.ranking_contracts import RankingPolicyConfig
from app.services.budget_service import (
    RunBudgetManager,
    _reserve,
    _resolve,
    cancel_outstanding_run_tasks,
    detect_expired_runs,
    get_budget_status,
    mark_cost_unavailable,
    persist_exhaustion,
    record_compute_cost,
    recover_stale_reservations,
    reserve_oracle_query,
)
from app.services.hypothesis_service import (
    HypothesisBudgetExhaustedError,
    propose_hypothesis_service,
)
from app.services.parallel_evaluation import (
    ERROR_EVALUATION_BUDGET_EXHAUSTED,
    ParallelEvaluationConfig,
    ParallelEvaluationRunner,
    ParallelExecutionMode,
    TaskOutcome,
)
from app.services.run_service import create_run_service


# ==============================================================================
# Fixtures and helpers
# ==============================================================================

@pytest.fixture
def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'budget-tests.db'}")
    manager.create_all_tables()
    yield manager
    manager.drop_all_tables()


class Clock:
    def __init__(self, now=None):
        self.now = now or datetime(2026, 10, 23, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def make_run(db, *, run_id="run-budget-1", budget=None, execution_mode="local"):
    with db.session_scope() as session:
        run = create_run_service(
            session=session,
            name="Budget Test",
            execution_mode=execution_mode,
            run_id=run_id,
            budget=budget or RunBudgetConfig(),
        )
        return run.run_id


def make_manager(db, run_id, clock=None):
    return RunBudgetManager(db.session_scope, run_id, clock=clock or Clock())


def read_usage(db, run_id):
    with db.session_scope() as session:
        usage = session.get(RunBudgetUsageModel, run_id)
        assert usage is not None
        return {
            "queries_reserved": usage.queries_reserved,
            "queries_completed": usage.queries_completed,
            "queries_failed": usage.queries_failed,
            "queries_released": usage.queries_released,
            "evaluations_reserved": usage.evaluations_reserved,
            "evaluations_completed": usage.evaluations_completed,
            "evaluations_failed": usage.evaluations_failed,
            "hypotheses_generated": usage.hypotheses_generated,
            "active_remote_tasks": usage.active_remote_tasks,
            "is_exhausted": usage.is_exhausted,
            "stop_reason": usage.stop_reason,
            "estimated_cost_usd": usage.estimated_cost_usd,
            "accruing_cost_usd": usage.accruing_cost_usd,
            "settled_cost_usd": usage.settled_cost_usd,
            "cost_data_state": usage.cost_data_state,
        }


def read_run(db, run_id):
    with db.session_scope() as session:
        run = session.get(RunModel, run_id)
        return {
            "status": run.status,
            "stop_reason": run.stop_reason,
            "started_at": run.started_at,
            "deadline_at": run.deadline_at,
            "completed_at": run.completed_at,
        }


def audit_events(db, run_id, event_type=None):
    with db.session_scope() as session:
        rows = [
            event
            for event in session.query(AuditEventModel)
            .filter(AuditEventModel.run_id == run_id)
            .all()
        ]
        return [
            (event.event_type, event.metadata_json)
            for event in rows
            if event_type is None or event.event_type == event_type
        ]


# ==============================================================================
# 1. Configuration validation and zero semantics
# ==============================================================================

class TestBudgetConfiguration:
    def test_documented_defaults_are_explicit(self):
        config = RunBudgetConfig()
        assert config.max_oracle_queries == DEFAULT_MAX_ORACLE_QUERIES
        assert config.max_candidate_evaluations == DEFAULT_MAX_CANDIDATE_EVALUATIONS
        assert config.max_concurrent_remote_tasks == DEFAULT_MAX_CONCURRENT_REMOTE_TASKS
        assert config.max_runtime_seconds == DEFAULT_MAX_RUNTIME_SECONDS
        assert config.max_generated_hypotheses is None
        assert config.max_compute_spend_usd is None

    def test_zero_is_accepted_and_means_no_work_not_unlimited(self):
        config = RunBudgetConfig.from_mapping(
            {
                "max_oracle_queries": 0,
                "max_candidate_evaluations": 0,
                "max_concurrent_remote_tasks": 0,
                "max_runtime_seconds": 0,
                "max_generated_hypotheses": 0,
                "max_compute_spend_usd": 0,
            }
        )
        assert config.max_oracle_queries == 0
        assert config.max_generated_hypotheses == 0
        assert config.max_compute_spend_usd == 0.0
        assert config.permits_any_work is False

    def test_omitted_optional_limits_are_disabled_not_zero(self):
        config = RunBudgetConfig.from_mapping({"max_oracle_queries": 3})
        assert config.max_generated_hypotheses is None
        assert config.max_compute_spend_usd is None

    @pytest.mark.parametrize("value", [-1, True, 1.5, "5", None])
    def test_invalid_query_limit_rejected(self, value):
        # An explicit null is not a valid integer admission limit; omission is
        # the only way to accept a documented default.
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping({"max_oracle_queries": value})

    def test_boolean_rejected_for_integer_limit(self):
        with pytest.raises(BudgetValidationError) as exc:
            RunBudgetConfig.from_mapping({"max_runtime_seconds": True})
        assert exc.value.details[0]["field"] == "max_runtime_seconds"

    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_money_rejected(self, value):
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping({"max_compute_spend_usd": value})

    def test_negative_money_rejected(self):
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping({"max_compute_spend_usd": -0.01})

    def test_malformed_budget_rejected(self):
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping(["not", "an", "object"])

    def test_unknown_field_rejected(self):
        with pytest.raises(BudgetValidationError) as exc:
            RunBudgetConfig.from_mapping({"max_queries_typo": 5})
        assert exc.value.details[0]["code"] == "UNKNOWN_FIELD"

    def test_conflicting_canonical_and_legacy_alias_rejected(self):
        with pytest.raises(BudgetValidationError) as exc:
            RunBudgetConfig.from_mapping(
                {"max_oracle_queries": 3, "max_queries": 4},
                allow_legacy_aliases=True,
            )
        assert exc.value.details[0]["code"] == "CONFLICTING_SETTING"

    def test_out_of_bounds_rejected(self):
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping({"max_concurrent_remote_tasks": 33})
        with pytest.raises(BudgetValidationError):
            RunBudgetConfig.from_mapping({"max_runtime_seconds": 604_801})

    def test_compute_enforcement_is_honest(self):
        local = RunBudgetConfig(max_compute_spend_usd=1.0)
        remote = RunBudgetConfig(max_compute_spend_usd=1.0)
        assert local.cost_enforcement("local") is CostEnforcement.NOT_APPLICABLE
        assert remote.cost_enforcement("nodus_remote") is CostEnforcement.APPLICATION_ESTIMATE
        assert remote.cost_enforcement("nodus_remote") is not CostEnforcement.PROVIDER_HARD_CAP


# ==============================================================================
# 2. Oracle query accounting
# ==============================================================================

class TestOracleBudget:
    def test_five_queries_cannot_execute_a_sixth(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        manager = make_manager(db, run_id)

        tokens = [manager.reserve_oracle_query(f"q-{i}") for i in range(5)]
        for token in tokens:
            manager.record_oracle_query_outcome(token, "completed")

        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_oracle_query("q-6")

        assert exc.value.reason is BudgetStopReason.QUERY_BUDGET_EXHAUSTED
        assert exc.value.is_terminal is True

        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 5
        assert usage["queries_completed"] == 5
        assert usage["is_exhausted"] is True
        assert usage["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"

        run = read_run(db, run_id)
        assert run["status"] == "budget_exhausted"
        assert run["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"
        assert run["completed_at"] is not None

    def test_exact_limit_admits_exactly_five(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        manager = make_manager(db, run_id)
        for index in range(5):
            manager.reserve_oracle_query(f"exact-{index}")
        status = manager.status()
        assert status.oracle_queries.used == 5
        assert status.oracle_queries.remaining == 0
        assert status.oracle_queries.exhausted is True
        assert status.is_exhausted is False  # no sixth attempt was made
        assert status.stop_reason is None

    def test_sixth_attempt_is_rejected_before_dispatch(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        oracle = PolynomialLocalOracle()
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=1),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()

        assert oracle.query_count == 1
        assert result.query_accounting.attempted_queries == 1
        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        assert usage["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"

    def test_proposed_experiment_that_is_rejected_is_not_counted(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=3))
        oracle = RejectingOracle()
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=3, max_iterations=2),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()

        assert oracle.calls == 0
        assert result.query_accounting.rejected_queries >= 1
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 0
        assert usage["queries_completed"] == 0

    def test_failed_query_is_charged_and_recorded(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=2))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("failure")
        manager.record_oracle_query_outcome(token, "failed")
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        assert usage["queries_failed"] == 1
        assert usage["queries_completed"] == 0

    def test_release_returns_capacity_only_for_undispatched_work(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("never-dispatched")
        manager.release_oracle_query(token)

        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 0
        assert usage["queries_released"] == 1
        # The released slot can be used again; no dispatch ever happened.
        assert manager.reserve_oracle_query("replacement")

    def test_ambiguous_timeout_stays_charged(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("timed-out")
        manager.record_oracle_query_outcome(token, "ambiguous")
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        with pytest.raises(BudgetAdmissionDenied):
            manager.reserve_oracle_query("cannot-prove-it-did-not-run")

    def test_retry_attempts_cannot_bypass_accounting(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        oracle = TimeoutThenSuccessOracle()
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=5, max_iterations=2, max_query_retries=2),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()

        assert oracle.calls == 1  # the retry was denied before reaching the oracle
        assert result.query_accounting.attempted_queries == 1
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        assert usage["queries_failed"] == 1
        assert usage["queries_completed"] == 0
        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED

    def test_retry_that_reaches_the_oracle_is_charged_twice(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=2))
        oracle = TimeoutThenSuccessOracle()
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=2, max_iterations=2, max_query_retries=1),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()

        assert oracle.calls == 2
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 2
        assert usage["queries_failed"] == 1
        assert usage["queries_completed"] == 1
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED

    def test_duplicate_delivery_keeps_accounting_idempotent(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=2))
        with db.session_scope() as session:
            first, created_first = reserve_oracle_query(session, run_id, "same-operation")
        assert created_first is True

        with pytest.raises(BudgetReservationReplay) as exc:
            with db.session_scope() as session:
                reserve_oracle_query(session, run_id, "same-operation")
        assert exc.value.reservation_id == first
        assert exc.value.state == "reserved"
        assert read_usage(db, run_id)["queries_reserved"] == 1


# ==============================================================================
# 2b. Whole-quantity admission
# ==============================================================================

class TestAtomicQuantityAdmission:
    def test_batch_larger_than_limit_is_refused_whole(self, db):
        """A six-unit batch against a five-unit limit must not be admitted."""
        run_id = make_run(db, budget=RunBudgetConfig(max_candidate_evaluations=5))
        manager = make_manager(db, run_id)

        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_candidate_evaluations(6, "batch-too-big")
        assert exc.value.reason is BudgetStopReason.EVALUATION_LIMIT_REACHED
        assert read_usage(db, run_id)["evaluations_reserved"] == 0

    def test_batch_exactly_filling_the_limit_is_admitted(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_candidate_evaluations=5))
        manager = make_manager(db, run_id)
        token = manager.reserve_candidate_evaluations(5, "exact-batch")
        assert token
        assert read_usage(db, run_id)["evaluations_reserved"] == 5

    def test_batch_is_denied_when_only_one_unit_remains(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_candidate_evaluations=5))
        manager = make_manager(db, run_id)
        manager.reserve_candidate_evaluations(4, "first-batch")

        with pytest.raises(BudgetAdmissionDenied):
            manager.reserve_candidate_evaluations(2, "second-batch")
        # The refusal is all-or-none: the single remaining unit was not consumed
        # and the counter did not overshoot the limit.
        assert read_usage(db, run_id)["evaluations_reserved"] == 4

    def test_hypothesis_batch_cannot_overshoot_the_limit(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_generated_hypotheses=3))
        manager = make_manager(db, run_id)

        with pytest.raises(BudgetAdmissionDenied):
            manager.reserve_hypotheses(4, "oversized")
        assert read_usage(db, run_id)["hypotheses_generated"] == 0

    def test_hypothesis_batch_is_denied_when_one_unit_remains(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_generated_hypotheses=3))
        manager = make_manager(db, run_id)
        manager.reserve_hypotheses(2, "fits")

        with pytest.raises(BudgetAdmissionDenied):
            manager.reserve_hypotheses(2, "one-left")
        assert read_usage(db, run_id)["hypotheses_generated"] == 2

    def test_unlimited_hypotheses_still_admit_batches(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_generated_hypotheses=None))
        manager = make_manager(db, run_id)
        assert manager.reserve_hypotheses(7, "unlimited")
        assert read_usage(db, run_id)["hypotheses_generated"] == 7

    def test_multi_unit_query_reservation_is_refused_past_the_limit(self, db):
        """A query limit of one must not admit a two-unit reservation."""
        clock = Clock()
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        with pytest.raises(BudgetAdmissionDenied):
            with db.session_scope() as session:
                run, config, usage = _resolve(session, run_id)
                _reserve(
                    session,
                    run,
                    config,
                    usage,
                    BudgetResource.ORACLE_QUERY,
                    2,
                    "two-units",
                    clock(),
                )
        assert read_usage(db, run_id)["queries_reserved"] == 0


# ==============================================================================
# 2c. Reservation replay never authorizes a second execution
# ==============================================================================

class TestReservationReplay:
    def _replay(self, db, run_id, operation_id):
        with pytest.raises(BudgetReservationReplay):
            with db.session_scope() as session:
                reserve_oracle_query(session, run_id, operation_id)

    def test_replay_of_reserved_reservation_is_refused(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=3))
        manager = make_manager(db, run_id)
        manager.reserve_oracle_query("in-flight")

        self._replay(db, run_id, "in-flight")
        assert read_usage(db, run_id)["queries_reserved"] == 1

    def test_replay_of_committed_reservation_is_refused(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=3))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("done")
        manager.record_oracle_query_outcome(token, "completed")

        self._replay(db, run_id, "done")
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        assert usage["queries_completed"] == 1

    def test_replay_of_released_reservation_is_refused(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=3))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("returned")
        manager.release_oracle_query(token)

        self._replay(db, run_id, "returned")
        # Released capacity stays usable by a *new* operation id, but the
        # released operation id itself is never re-admitted.
        assert read_usage(db, run_id)["queries_reserved"] == 0
        assert manager.reserve_oracle_query("fresh-operation")

    def test_replay_of_ambiguous_reservation_is_refused(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=2))
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("unknown")
        manager.record_oracle_query_outcome(token, "ambiguous")

        self._replay(db, run_id, "unknown")
        assert read_usage(db, run_id)["queries_reserved"] == 1

    def test_replay_detail_is_structured_and_leaks_nothing(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        manager = make_manager(db, run_id)
        manager.reserve_oracle_query("op")

        with pytest.raises(BudgetReservationReplay) as exc:
            with db.session_scope() as session:
                reserve_oracle_query(session, run_id, "op")
        payload = exc.value.to_dict()
        assert payload["code"] == "DUPLICATE_RESERVATION"
        assert payload["admitted"] is False
        assert payload["resource"] == "oracle_query"
        assert set(payload) == {
            "code",
            "reservation_id",
            "resource",
            "state",
            "operation_id",
            "admitted",
        }

    def test_engine_does_not_query_again_on_replay(self, db):
        """A restarted run reusing the same operation id must not re-query."""
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        manager = make_manager(db, run_id)
        manager.reserve_oracle_query(f"{run_id}:query:1")

        oracle = PolynomialLocalOracle()
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=5, max_iterations=2),
            run_id=run_id,
            budget=manager,
        )
        result = engine.run()

        assert oracle.query_count == 0
        assert result.status == DiscoveryLifecycleStatus.FAILED
        codes = [error.code for error in result.errors]
        assert ERROR_DUPLICATE_BUDGET_RESERVATION in codes
        assert read_usage(db, run_id)["queries_reserved"] == 1


# ==============================================================================
# 3. Concurrency
# ==============================================================================

class TestBudgetConcurrency:
    def test_concurrent_workers_cannot_exceed_query_limit(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        barrier = threading.Barrier(10)

        def attempt(index):
            barrier.wait(timeout=10)
            manager = RunBudgetManager(db.session_scope, run_id)
            try:
                manager.reserve_oracle_query(f"concurrent-{index}")
                return "admitted"
            except BudgetAdmissionDenied:
                return "denied"

        with ThreadPoolExecutor(max_workers=10) as pool:
            outcomes = list(pool.map(attempt, range(10)))

        assert outcomes.count("admitted") == 5
        assert outcomes.count("denied") == 5
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 5
        assert usage["is_exhausted"] is True

    def test_concurrent_evaluations_cannot_exceed_limit(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_candidate_evaluations=3))
        barrier = threading.Barrier(8)

        def attempt(index):
            barrier.wait(timeout=10)
            manager = RunBudgetManager(db.session_scope, run_id)
            try:
                manager.reserve_candidate_evaluations(1, f"eval-{index}")
                return "admitted"
            except BudgetAdmissionDenied:
                return "denied"

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(8)))

        assert outcomes.count("admitted") == 3
        assert read_usage(db, run_id)["evaluations_reserved"] == 3

    def test_remote_slots_are_all_or_none_and_idempotent(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=2))
        manager = make_manager(db, run_id)

        first = manager.try_acquire_remote_slots(1, "slot-a")
        second = manager.try_acquire_remote_slots(1, "slot-b")
        assert first and second
        assert manager.try_acquire_remote_slots(1, "slot-c") is None  # backpressure

        manager.release_remote_slots(first)
        manager.release_remote_slots(first)  # duplicate release must not double-free
        assert read_usage(db, run_id)["active_remote_tasks"] == 1

        third = manager.try_acquire_remote_slots(1, "slot-c")
        assert third is not None
        assert read_usage(db, run_id)["active_remote_tasks"] == 2

    def test_batch_slot_request_is_atomic(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=3))
        manager = make_manager(db, run_id)
        manager.try_acquire_remote_slots(2, "batch-a")
        # Only one slot is free, so a two-slot request must be refused whole.
        assert manager.try_acquire_remote_slots(2, "batch-b") is None
        assert read_usage(db, run_id)["active_remote_tasks"] == 2

    def test_multiple_managers_share_the_persisted_limit(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        manager_a = RunBudgetManager(db.session_scope, run_id, clock=Clock())
        manager_b = RunBudgetManager(db.session_scope, run_id, clock=Clock())

        admitted = 0
        for index in range(3):
            manager_a.reserve_oracle_query(f"a-{index}")
            admitted += 1
        for index in range(3):
            try:
                manager_b.reserve_oracle_query(f"b-{index}")
                admitted += 1
            except BudgetAdmissionDenied:
                pass

        assert admitted == 5
        assert read_usage(db, run_id)["queries_reserved"] == 5

    def test_stale_remote_slot_is_recovered(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=1))
        clock = Clock()
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.try_acquire_remote_slots(1, "crashed-worker")
        assert read_usage(db, run_id)["active_remote_tasks"] == 1

        with db.session_scope() as session:
            result = recover_stale_reservations(
                session, run_id, now=clock() + timedelta(seconds=3600)
            )
        assert result["released"] == 1
        assert read_usage(db, run_id)["active_remote_tasks"] == 0
        assert manager.try_acquire_remote_slots(1, "recovered") is not None

    def test_stale_oracle_reservation_is_kept_charged_as_ambiguous(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        clock = Clock()
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.reserve_oracle_query("possibly-executed")

        with db.session_scope() as session:
            result = recover_stale_reservations(
                session, run_id, now=clock() + timedelta(seconds=3600)
            )
        assert result["ambiguous"] == 1
        assert read_usage(db, run_id)["queries_reserved"] == 1

    def test_stale_slot_with_a_live_remote_task_stays_charged(self, db):
        """A local TTL expiry must not free a slot whose provider task is live."""
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=1))
        clock = Clock()
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.try_acquire_remote_slots(1, "slow-worker")
        with db.session_scope() as session:
            session.add(
                ComputeTaskModel(
                    task_id="task-live",
                    run_id=run_id,
                    execution_mode="nodus_remote",
                    provider_task_id="prov-live",
                    status="running",
                )
            )

        with db.session_scope() as session:
            result = recover_stale_reservations(
                session, run_id, now=clock() + timedelta(seconds=3600)
            )

        assert result == {"released": 0, "ambiguous": 1}
        # The concurrency invariant holds: no new dispatch may start while the
        # provider task is still running.
        assert read_usage(db, run_id)["active_remote_tasks"] == 1
        assert manager.try_acquire_remote_slots(1, "second-dispatch") is None

    def test_stale_slot_is_released_once_the_remote_task_is_terminal(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=1))
        clock = Clock()
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.try_acquire_remote_slots(1, "finished-worker")
        with db.session_scope() as session:
            session.add(
                ComputeTaskModel(
                    task_id="task-done",
                    run_id=run_id,
                    execution_mode="nodus_remote",
                    provider_task_id="prov-done",
                    status="completed",
                )
            )

        with db.session_scope() as session:
            result = recover_stale_reservations(
                session, run_id, now=clock() + timedelta(seconds=3600)
            )

        assert result["released"] == 1
        assert read_usage(db, run_id)["active_remote_tasks"] == 0
        assert manager.try_acquire_remote_slots(1, "next-dispatch") is not None

    def test_only_the_surplus_slot_is_released(self, db):
        """With two slots and one live task, only the provably free slot frees."""
        run_id = make_run(db, budget=RunBudgetConfig(max_concurrent_remote_tasks=2))
        clock = Clock()
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.try_acquire_remote_slots(1, "slot-live")
        manager.try_acquire_remote_slots(1, "slot-orphan")
        with db.session_scope() as session:
            session.add(
                ComputeTaskModel(
                    task_id="task-live",
                    run_id=run_id,
                    execution_mode="nodus_remote",
                    provider_task_id="prov-live",
                    status="running",
                )
            )

        with db.session_scope() as session:
            result = recover_stale_reservations(
                session, run_id, now=clock() + timedelta(seconds=3600)
            )

        assert result == {"released": 1, "ambiguous": 1}
        assert read_usage(db, run_id)["active_remote_tasks"] == 1
        assert manager.try_acquire_remote_slots(1, "after-recovery") is not None
        assert manager.try_acquire_remote_slots(1, "over-limit") is None


# ==============================================================================
# 4. Runtime limits
# ==============================================================================

class TestRuntimeBudget:
    def test_work_is_admitted_before_the_deadline(self, db):
        clock = Clock()
        run_id = make_run(
            db, budget=RunBudgetConfig(max_runtime_seconds=10, max_oracle_queries=2)
        )
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.reserve_oracle_query("before")
        clock.advance(9)
        manager.reserve_oracle_query("still-before")

        status = manager.status()
        assert status.remaining_seconds == pytest.approx(1.0)
        assert status.elapsed_seconds == pytest.approx(9.0)

    def test_no_work_is_admitted_at_or_after_the_deadline(self, db):
        clock = Clock()
        run_id = make_run(
            db, budget=RunBudgetConfig(max_runtime_seconds=10, max_oracle_queries=5)
        )
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.reserve_oracle_query("before")
        clock.advance(10)

        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_oracle_query("at-deadline")
        assert exc.value.reason is BudgetStopReason.TIME_LIMIT_EXCEEDED
        assert read_usage(db, run_id)["queries_reserved"] == 1
        assert read_run(db, run_id)["status"] == "budget_exhausted"

    def test_zero_runtime_prevents_any_work(self, db):
        run_id = make_run(
            db,
            budget=RunBudgetConfig(max_runtime_seconds=0, max_oracle_queries=10),
        )
        manager = make_manager(db, run_id)
        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_oracle_query("never")
        assert exc.value.reason is BudgetStopReason.TIME_LIMIT_EXCEEDED
        assert read_usage(db, run_id)["queries_reserved"] == 0

    def test_expired_run_is_detected_after_restart(self, db):
        clock = Clock()
        run_id = make_run(
            db, budget=RunBudgetConfig(max_runtime_seconds=10, max_oracle_queries=3)
        )
        manager = RunBudgetManager(db.session_scope, run_id, clock=clock)
        manager.reserve_oracle_query("started")

        with db.session_scope() as session:
            expired = detect_expired_runs(session, now=clock() + timedelta(seconds=11))
        assert expired == [run_id]
        assert read_run(db, run_id)["stop_reason"] == "TIME_LIMIT_EXCEEDED"

    def test_engine_stops_scheduling_after_time_limit(self, db):
        clock = Clock()
        run_id = make_run(
            db, budget=RunBudgetConfig(max_runtime_seconds=1, max_oracle_queries=10)
        )
        oracle = AdvancingOracle(clock, step_seconds=1)
        engine = DiscoveryEngine(
            oracle,
            make_engine_config(max_query_budget=10, max_iterations=10),
            run_id=run_id,
            budget=RunBudgetManager(db.session_scope, run_id, clock=clock),
        )
        result = engine.run()

        assert oracle.calls == 1
        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
        assert read_usage(db, run_id)["stop_reason"] == "TIME_LIMIT_EXCEEDED"


# ==============================================================================
# 5. Exhaustion, preservation, and recovery
# ==============================================================================

class TestExhaustionLifecycle:
    def test_completed_observations_survive_exhaustion(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        with db.session_scope() as session:
            session.add(
                ObservationModel(
                    observation_id="obs-kept",
                    run_id=run_id,
                    input_id="inp-kept",
                    input_values={"x": 0.5},
                    output_values={"y": 1.5},
                    status="success",
                    sequence_number=1,
                )
            )
        manager = make_manager(db, run_id)
        token = manager.reserve_oracle_query("only")
        manager.record_oracle_query_outcome(token, "completed")
        with pytest.raises(BudgetAdmissionDenied):
            manager.reserve_oracle_query("over-limit")

        with db.session_scope() as session:
            kept = session.get(ObservationModel, "obs-kept")
            assert kept is not None
            assert kept.output_values == {"y": 1.5}

    def test_first_exhaustion_reason_wins(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        with db.session_scope() as session:
            assert persist_exhaustion(
                session, run_id, BudgetStopReason.QUERY_BUDGET_EXHAUSTED
            )
        with db.session_scope() as session:
            assert not persist_exhaustion(
                session, run_id, BudgetStopReason.TIME_LIMIT_EXCEEDED
            )

        usage = read_usage(db, run_id)
        assert usage["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"
        events = audit_events(db, run_id, "BUDGET_EXHAUSTED")
        assert len(events) == 1
        assert events[0][1]["reason"] == "QUERY_BUDGET_EXHAUSTED"

    def test_concurrent_exhaustion_signals_produce_one_outcome(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        reasons = [
            BudgetStopReason.QUERY_BUDGET_EXHAUSTED,
            BudgetStopReason.TIME_LIMIT_EXCEEDED,
            BudgetStopReason.EVALUATION_LIMIT_REACHED,
        ]
        barrier = threading.Barrier(len(reasons))

        def signal(reason):
            barrier.wait(timeout=10)
            RunBudgetManager(db.session_scope, run_id).mark_exhausted(reason)

        with ThreadPoolExecutor(max_workers=len(reasons)) as pool:
            list(pool.map(signal, reasons))

        events = audit_events(db, run_id, "BUDGET_EXHAUSTED")
        assert len(events) == 1
        assert read_usage(db, run_id)["stop_reason"] in {r.value for r in reasons}

    def test_cancellation_failure_does_not_erase_exhaustion_reason(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        with db.session_scope() as session:
            session.add(
                ComputeTaskModel(
                    task_id="task-1",
                    run_id=run_id,
                    execution_mode="nodus_remote",
                    provider_task_id="prov-1",
                    status="running",
                )
            )
        with db.session_scope() as session:
            persist_exhaustion(
                session, run_id, BudgetStopReason.QUERY_BUDGET_EXHAUSTED
            )

        # Exhaustion itself cancels outstanding application-side tasks.
        assert read_usage(db, run_id)["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"
        with db.session_scope() as session:
            task = session.get(ComputeTaskModel, "task-1")
            assert task.status == "cancelled"
            assert task.error_metadata["code"] == "CANCELLED_BY_BUDGET"
            assert task.error_metadata["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"

        cancel_events = audit_events(db, run_id, "RUN_TASKS_CANCELLED")
        assert len(cancel_events) == 1
        assert cancel_events[0][1]["provider_task_ids"] == ["prov-1"]
        assert cancel_events[0][1]["provider_cancellation_confirmed"] is False

        # Repeating the cancellation is idempotent and cannot erase the reason.
        with db.session_scope() as session:
            assert (
                cancel_outstanding_run_tasks(
                    session,
                    run_id,
                    stop_reason=BudgetStopReason.QUERY_BUDGET_EXHAUSTED,
                )
                == []
            )
        assert read_usage(db, run_id)["stop_reason"] == "QUERY_BUDGET_EXHAUSTED"

    def test_engine_local_limit_does_not_falsely_mark_persisted_budget_exhausted(self, db):
        """A stricter engine-local counter must not claim persisted exhaustion."""
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=5))
        engine = DiscoveryEngine(
            PolynomialLocalOracle(),
            make_engine_config(max_query_budget=1, max_iterations=5),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()

        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
        usage = read_usage(db, run_id)
        assert usage["queries_reserved"] == 1
        assert usage["is_exhausted"] is False
        assert usage["stop_reason"] is None
        assert make_manager(db, run_id).status().oracle_queries.remaining == 4

    def test_engine_does_not_report_resource_stop_as_scientific_success(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_oracle_queries=1))
        engine = DiscoveryEngine(
            PolynomialLocalOracle(),
            make_engine_config(max_query_budget=1, max_iterations=5),
            run_id=run_id,
            budget=make_manager(db, run_id),
        )
        result = engine.run()
        assert result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED
        assert result.termination_reason == DiscoveryTerminationReason.BUDGET_EXHAUSTED
        assert result.status != DiscoveryLifecycleStatus.TERMINATED_STOPPING_CRITERION


# ==============================================================================
# 6. Cost states and enforcement honesty
# ==============================================================================

class TestCostAccounting:
    def test_unavailable_cost_is_null_not_zero(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_compute_spend_usd=5.0))
        manager = make_manager(db, run_id)
        status = manager.status()

        # The initial legacy usage snapshot must not fabricate actual spend.
        with db.session_scope() as session:
            from app.adapters.persistence.repository import ClarityRepository

            latest = ClarityRepository(session).get_latest_usage_for_run(run_id)
            assert latest.settled_cost_usd is None
            assert latest.live_accruing_cost_usd is None

        payload = status.to_dict()
        assert payload["cost"]["settled_cost_usd"] is None
        assert payload["cost"]["accruing_cost_usd"] is None
        assert payload["cost"]["estimated_cost_usd"] is None
        assert payload["cost"]["data_state"] == "not_started"
        assert payload["cost"]["provider_hard_cap_supported"] is False

    def test_explicit_unavailable_state_keeps_null_values(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_compute_spend_usd=5.0))
        with db.session_scope() as session:
            mark_cost_unavailable(session, run_id)
        payload = make_manager(db, run_id).status().to_dict()
        assert payload["cost"]["data_state"] == CostDataState.UNAVAILABLE.value
        assert payload["cost"]["settled_cost_usd"] is None
        assert payload["cost"]["estimated_cost_usd"] is None
        assert any("never as zero" in text for text in payload["limitations"])

    def test_cost_tiers_are_separate(self, db):
        run_id = make_run(
            db, budget=RunBudgetConfig(max_compute_spend_usd=5.0), execution_mode="nodus_remote"
        )
        with db.session_scope() as session:
            record_compute_cost(session, run_id, estimated_cost_usd=0.10)
            record_compute_cost(session, run_id, accruing_cost_usd=0.12)
            record_compute_cost(session, run_id, settled_cost_usd=0.11)
        payload = make_manager(db, run_id).status().to_dict()
        assert payload["cost"]["estimated_cost_usd"] == pytest.approx(0.10)
        assert payload["cost"]["accruing_cost_usd"] == pytest.approx(0.12)
        assert payload["cost"]["settled_cost_usd"] == pytest.approx(0.11)
        assert payload["cost"]["data_state"] == "settled"

    def test_zero_ceiling_denies_remote_work(self, db):
        run_id = make_run(
            db,
            budget=RunBudgetConfig(max_compute_spend_usd=0, max_oracle_queries=5),
            execution_mode="nodus_remote",
        )
        manager = make_manager(db, run_id)
        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_oracle_query("billable")
        assert exc.value.reason is BudgetStopReason.COMPUTE_BUDGET_EXHAUSTED
        assert read_usage(db, run_id)["queries_reserved"] == 0

    def test_estimate_reaching_ceiling_stops_future_scheduling(self, db):
        run_id = make_run(
            db,
            budget=RunBudgetConfig(max_compute_spend_usd=0.20, max_oracle_queries=5),
            execution_mode="nodus_remote",
        )
        manager = make_manager(db, run_id)
        manager.reserve_oracle_query("first")
        with db.session_scope() as session:
            record_compute_cost(session, run_id, estimated_cost_usd=0.20)

        with pytest.raises(BudgetAdmissionDenied) as exc:
            manager.reserve_oracle_query("second")
        assert exc.value.reason is BudgetStopReason.COMPUTE_BUDGET_EXHAUSTED
        assert read_usage(db, run_id)["stop_reason"] == "COMPUTE_BUDGET_EXHAUSTED"

    def test_local_run_ceiling_does_not_block_work(self, db):
        run_id = make_run(
            db,
            budget=RunBudgetConfig(max_compute_spend_usd=0, max_oracle_queries=1),
            execution_mode="local",
        )
        manager = make_manager(db, run_id)
        assert manager.reserve_oracle_query("local-work")
        assert manager.status().cost_enforcement is CostEnforcement.NOT_APPLICABLE


# ==============================================================================
# 7. Hypothesis and evaluation limits
# ==============================================================================

class TestEvaluationAndHypothesisLimits:
    def test_hypothesis_limit_is_enforced_and_persisted(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_generated_hypotheses=1))
        with db.session_scope() as session:
            propose_hypothesis_service(
                session,
                run_id=run_id,
                model_family="linear",
                formula="y = a*x + b",
                parameters={"a": 1.0, "b": 0.0},
                hypothesis_id="hyp-first",
            )

        with pytest.raises(HypothesisBudgetExhaustedError) as exc:
            with db.session_scope() as session:
                propose_hypothesis_service(
                    session,
                    run_id=run_id,
                    model_family="linear",
                    formula="y = a*x + b",
                    parameters={"a": 2.0, "b": 0.0},
                    hypothesis_id="hyp-second",
                )
        assert exc.value.reason is BudgetStopReason.HYPOTHESIS_LIMIT_REACHED
        assert read_usage(db, run_id)["hypotheses_generated"] == 1

        # The API boundary persists the terminal decision in its own
        # transaction after the denied request rolls back (see API tests).
        with db.session_scope() as session:
            persist_exhaustion(session, run_id, exc.value.reason)
        assert read_usage(db, run_id)["stop_reason"] == "HYPOTHESIS_LIMIT_REACHED"
        assert read_run(db, run_id)["status"] == "budget_exhausted"

    def test_omitted_hypothesis_limit_does_not_block(self, db):
        run_id = make_run(db, budget=RunBudgetConfig())
        with db.session_scope() as session:
            for index in range(3):
                propose_hypothesis_service(
                    session,
                    run_id=run_id,
                    model_family="linear",
                    formula="y = a*x + b",
                    parameters={"a": 1.0, "b": 0.0},
                    hypothesis_id=f"hyp-{index}",
                )
        status = make_manager(db, run_id).status()
        assert status.generated_hypotheses.limit is None
        assert status.generated_hypotheses.used == 3
        assert status.generated_hypotheses.exhausted is False

    def test_evaluation_limit_is_enforced_by_parallel_runner(self, db):
        run_id = make_run(db, budget=RunBudgetConfig(max_candidate_evaluations=1))
        runner = ParallelEvaluationRunner(
            _StubAdapter(),
            ParallelEvaluationConfig(
                execution_mode=ParallelExecutionMode.LOCAL_SEQUENTIAL
            ),
            budget=make_manager(db, run_id),
        )
        observations = make_observations()
        first = runner.run([make_request("hyp-1")], observations, run_id=run_id)
        assert first.results[0].outcome == TaskOutcome.SUCCEEDED

        second = runner.run(
            [make_request("hyp-2", request_id="evreq-2")],
            observations,
            run_id=run_id,
        )
        assert second.results[0].outcome == TaskOutcome.FAILED
        assert second.results[0].error.code == ERROR_EVALUATION_BUDGET_EXHAUSTED
        assert read_usage(db, run_id)["evaluations_reserved"] == 1


# ==============================================================================
# 8. Status contract
# ==============================================================================

class TestBudgetStatusContract:
    def test_status_exposes_limits_usage_and_counters(self, db):
        run_id = make_run(
            db,
            budget=RunBudgetConfig(
                max_oracle_queries=4,
                max_candidate_evaluations=6,
                max_concurrent_remote_tasks=2,
                max_runtime_seconds=60,
                max_generated_hypotheses=3,
            ),
        )
        manager = make_manager(db, run_id)
        first = manager.reserve_oracle_query("status-1")
        manager.record_oracle_query_outcome(first, "completed")
        second = manager.reserve_oracle_query("status-2")
        manager.record_oracle_query_outcome(second, "failed")

        payload = manager.status().to_dict()
        assert payload["limits"]["max_oracle_queries"] == 4
        assert payload["usage"]["query_outcomes"]["attempted"] == 2
        assert payload["usage"]["query_outcomes"]["completed"] == 1
        assert payload["usage"]["query_outcomes"]["failed"] == 1
        assert payload["usage"]["oracle_queries"]["remaining"] == 2
        assert payload["usage"]["candidate_evaluations"]["remaining"] == 6
        assert payload["usage"]["max_concurrent_remote_tasks"] == 2
        assert payload["runtime"]["started_at"] is not None
        assert payload["runtime"]["deadline_at"] is not None
        assert payload["runtime"]["remaining_seconds"] is not None
        assert payload["zero_semantics"]["max_oracle_queries"]
        assert payload["stop_reason"] is None
        assert payload["is_exhausted"] is False

    def test_legacy_run_without_budget_row_gets_documented_defaults(self, db):
        with db.session_scope() as session:
            session.add(RunModel(run_id="run-legacy", name="Legacy", status="pending"))
        with db.session_scope() as session:
            status = get_budget_status(session, "run-legacy")
        assert status.config == RunBudgetConfig()
        assert status.cost_data_state is CostDataState.NOT_STARTED


# ==============================================================================
# Engine test scaffolding
# ==============================================================================

def make_engine_config(**overrides):
    values = {
        "max_query_budget": 3,
        "max_iterations": 5,
        "domain_bounds": NumericalDomainBounds(
            variable_name="x", min_val=-2.0, max_val=2.0, domain_name="1d_polynomial"
        ),
        "hypothesis_config": HypothesisGenerationConfig(
            min_degree=0, max_degree=2, candidate_count=4, seed=11
        ),
        "ranking_policy": RankingPolicyConfig(
            min_samples_for_plausibility=3, performance_acceptance_threshold=1e9
        ),
        "experiment_pool_size": 4,
    }
    values.update(overrides)
    return DiscoveryRunConfig(**values)


class TimeoutThenSuccessOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def query(self, experiment_input):
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("transient timeout")
        return super().query(experiment_input)


class RejectingOracle(PolynomialLocalOracle):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def validate_input(self, experiment_input):
        return StructuredError(code="OUT_OF_RANGE", message="Input is outside range.")

    def query(self, experiment_input):
        self.calls += 1
        return super().query(experiment_input)


class AdvancingOracle(PolynomialLocalOracle):
    def __init__(self, clock, step_seconds=1):
        super().__init__()
        self.clock = clock
        self.step_seconds = step_seconds
        self.calls = 0

    def query(self, experiment_input):
        self.calls += 1
        self.clock.advance(self.step_seconds)
        return super().query(experiment_input)


class _StubAdapter:
    """Local-mode runner never calls the adapter; a mode attribute is enough."""

    class _Config:
        execution_mode = "mock"

    config = _Config()


def make_candidate(degree=2, hypothesis_id="hyp-1"):
    coefficients = [0.0] * (degree + 1)
    coefficients[degree] = 1.0
    specification = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
        "input_variable": "x",
        "output_variable": "y",
    }
    return HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula=f"candidate-degree-{degree}",
        parameters=specification,
        complexity=degree + 1,
        hypothesis_id=hypothesis_id,
    )


def make_observations():
    points = [-2.0, -1.0, 0.0, 1.0, 2.0]
    observations = {}
    for index, x in enumerate(points):
        observation_id = f"obs-{index}"
        observations[observation_id] = Observation(
            observation_id=observation_id,
            input_id=f"inp-{index}",
            input_values={"x": x},
            output_values={"y": x ** 2},
            status=ObservationStatus.SUCCESS,
            sequence_number=index,
        )
    return observations


def make_request(hypothesis_id="hyp-1", request_id="evreq-1"):
    return EvaluationRequest(
        hypothesis=make_candidate(hypothesis_id=hypothesis_id),
        observation_ids=sorted(make_observations()),
        held_out_observation_ids=[],
        request_id=request_id,
        metric_names=["mae", "rmse"],
    )
