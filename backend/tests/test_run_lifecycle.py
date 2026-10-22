"""Unit tests for the RunLifecycleManager domain service.

Covers:
  - State machine: valid and invalid transitions
  - Terminal state immutability
  - State alias normalisation (pending -> queued, budget_exhausted -> stopped_by_budget)
  - Idempotent transitions (same state -> same state)
  - Initialization: duplicate run detection
  - Progress tracking: counters, elapsed time, no fabricated percentages
  - Cancellation: queued -> cancelled, running -> cancelling -> cancelled
  - Budget stop: running -> stopped_by_budget
  - Completion: running -> completed
  - Failure: explicit diagnostics, diagnostic sanitisation
  - Retry policy: delay calculation, error classification
  - Thread safety: concurrent transitions serialised per run
"""

import threading
from datetime import timezone

import pytest

from app.domain.run_lifecycle import (
    DuplicateExecutionError,
    ErrorCategory,
    InvalidStateTransitionError,
    RetryPolicy,
    RunLifecycleManager,
    RunLifecycleState,
    RunNotFoundError,
    RunPhase,
    normalize_state,
)


def _new_manager() -> RunLifecycleManager:
    return RunLifecycleManager()


def _init(manager, run_id="run-test-001", **kwargs):
    return manager.initialize_run(run_id=run_id, name="Test Run", **kwargs)


class TestNormalizeState:
    def test_queued_passthrough(self):
        assert normalize_state(RunLifecycleState.QUEUED) == RunLifecycleState.QUEUED

    def test_pending_normalises_to_queued(self):
        assert normalize_state(RunLifecycleState.PENDING) == RunLifecycleState.QUEUED

    def test_budget_exhausted_normalises_to_stopped_by_budget(self):
        assert normalize_state(RunLifecycleState.BUDGET_EXHAUSTED) == RunLifecycleState.STOPPED_BY_BUDGET

    def test_string_input(self):
        assert normalize_state("pending") == RunLifecycleState.QUEUED

    def test_unknown_string_raises(self):
        with pytest.raises(ValueError, match="Unknown lifecycle state"):
            normalize_state("not_a_real_state")

    def test_running_passthrough(self):
        assert normalize_state("running") == RunLifecycleState.RUNNING


class TestInitializeRun:
    def test_creates_queued_progress(self):
        m = _new_manager()
        prog = _init(m, "r1")
        assert prog.run_id == "r1"
        assert prog.status == RunLifecycleState.QUEUED
        assert prog.phase == RunPhase.CREATED

    def test_stores_budget_limits(self):
        m = _new_manager()
        prog = _init(m, "r2", max_queries=100, max_evaluations=500)
        assert prog.max_queries == 100
        assert prog.max_evaluations == 500

    def test_duplicate_init_raises(self):
        m = _new_manager()
        _init(m, "r3")
        with pytest.raises(DuplicateExecutionError):
            _init(m, "r3")

    def test_initial_activity_not_none(self):
        m = _new_manager()
        prog = _init(m, "r4")
        assert prog.current_activity is not None


class TestValidTransitions:
    def test_queued_to_running(self):
        m = _new_manager(); _init(m, "rv1")
        assert m.transition_state("rv1", RunLifecycleState.RUNNING).status == RunLifecycleState.RUNNING

    def test_queued_to_cancelled(self):
        m = _new_manager(); _init(m, "rv2")
        assert m.transition_state("rv2", RunLifecycleState.CANCELLED).status == RunLifecycleState.CANCELLED

    def test_queued_to_failed(self):
        m = _new_manager(); _init(m, "rv3")
        assert m.transition_state("rv3", RunLifecycleState.FAILED).status == RunLifecycleState.FAILED

    def test_running_to_completed(self):
        m = _new_manager(); _init(m, "rv4")
        m.transition_state("rv4", RunLifecycleState.RUNNING)
        assert m.transition_state("rv4", RunLifecycleState.COMPLETED).status == RunLifecycleState.COMPLETED

    def test_running_to_stopped_by_budget(self):
        m = _new_manager(); _init(m, "rv5")
        m.transition_state("rv5", RunLifecycleState.RUNNING)
        assert m.transition_state("rv5", RunLifecycleState.STOPPED_BY_BUDGET).status == RunLifecycleState.STOPPED_BY_BUDGET

    def test_running_to_cancelling_to_cancelled(self):
        m = _new_manager(); _init(m, "rv6")
        m.transition_state("rv6", RunLifecycleState.RUNNING)
        m.transition_state("rv6", RunLifecycleState.CANCELLING)
        assert m.transition_state("rv6", RunLifecycleState.CANCELLED).status == RunLifecycleState.CANCELLED

    def test_alias_budget_exhausted_accepted(self):
        m = _new_manager(); _init(m, "rv8")
        m.transition_state("rv8", RunLifecycleState.RUNNING)
        p = m.transition_state("rv8", "budget_exhausted")
        assert p.status == RunLifecycleState.STOPPED_BY_BUDGET


class TestInvalidTransitions:
    def test_queued_to_completed_rejected(self):
        m = _new_manager(); _init(m, "ri1")
        with pytest.raises(InvalidStateTransitionError) as exc_info:
            m.transition_state("ri1", RunLifecycleState.COMPLETED)
        assert "queued" in str(exc_info.value).lower()

    def test_completed_is_immutable(self):
        m = _new_manager(); _init(m, "ri3")
        m.transition_state("ri3", RunLifecycleState.RUNNING)
        m.transition_state("ri3", RunLifecycleState.COMPLETED)
        with pytest.raises(InvalidStateTransitionError):
            m.transition_state("ri3", RunLifecycleState.RUNNING)

    def test_cancelled_is_immutable(self):
        m = _new_manager(); _init(m, "ri4")
        m.transition_state("ri4", RunLifecycleState.CANCELLED)
        with pytest.raises(InvalidStateTransitionError):
            m.transition_state("ri4", RunLifecycleState.RUNNING)

    def test_failed_is_immutable(self):
        m = _new_manager(); _init(m, "ri5")
        m.transition_state("ri5", RunLifecycleState.RUNNING)
        m.transition_state("ri5", RunLifecycleState.FAILED)
        with pytest.raises(InvalidStateTransitionError):
            m.transition_state("ri5", RunLifecycleState.COMPLETED)

    def test_stopped_by_budget_is_immutable(self):
        m = _new_manager(); _init(m, "ri7")
        m.transition_state("ri7", RunLifecycleState.RUNNING)
        m.transition_state("ri7", RunLifecycleState.STOPPED_BY_BUDGET)
        with pytest.raises(InvalidStateTransitionError):
            m.transition_state("ri7", RunLifecycleState.RUNNING)


class TestIdempotentTransitions:
    def test_same_state_is_noop(self):
        m = _new_manager(); _init(m, "rid1")
        m.transition_state("rid1", RunLifecycleState.RUNNING)
        p = m.transition_state("rid1", RunLifecycleState.RUNNING, activity="still running")
        assert p.status == RunLifecycleState.RUNNING
        assert p.current_activity == "still running"


class TestStartRun:
    def test_start_moves_to_running(self):
        m = _new_manager(); _init(m, "rs1")
        p = m.start_run("rs1")
        assert p.status == RunLifecycleState.RUNNING
        assert p.phase == RunPhase.INITIALIZING

    def test_double_start_raises(self):
        m = _new_manager(); _init(m, "rs2")
        m.start_run("rs2")
        with pytest.raises(DuplicateExecutionError):
            m.start_run("rs2")

    def test_start_terminal_raises(self):
        m = _new_manager(); _init(m, "rs3")
        m.transition_state("rs3", RunLifecycleState.CANCELLED)
        with pytest.raises(DuplicateExecutionError):
            m.start_run("rs3")


class TestCancelRun:
    def test_cancel_queued_run(self):
        m = _new_manager(); _init(m, "rc1")
        p = m.request_cancellation("rc1", reason="user requested")
        assert p.status == RunLifecycleState.CANCELLED
        assert p.stop_reason == "user requested"

    def test_cancel_running_run(self):
        m = _new_manager(); _init(m, "rc2")
        m.start_run("rc2")
        p = m.request_cancellation("rc2")
        assert p.status == RunLifecycleState.CANCELLED

    def test_cancel_already_terminal_raises(self):
        m = _new_manager(); _init(m, "rc3")
        m.transition_state("rc3", RunLifecycleState.RUNNING)
        m.transition_state("rc3", RunLifecycleState.COMPLETED)
        with pytest.raises(InvalidStateTransitionError):
            m.request_cancellation("rc3")


class TestStopByBudget:
    def test_stop_by_budget_marks_run(self):
        m = _new_manager(); _init(m, "rb1")
        m.start_run("rb1")
        p = m.stop_by_budget("rb1")
        assert p.status == RunLifecycleState.STOPPED_BY_BUDGET
        assert p.stop_reason is not None
        assert p.phase == RunPhase.TERMINATED

    def test_stop_by_budget_sets_completed_at(self):
        m = _new_manager(); _init(m, "rb2")
        m.start_run("rb2")
        p = m.stop_by_budget("rb2")
        assert p.completed_at is not None


class TestCompleteRun:
    def test_complete_run_success(self):
        m = _new_manager(); _init(m, "rco1")
        m.start_run("rco1")
        p = m.complete_run("rco1")
        assert p.status == RunLifecycleState.COMPLETED
        assert p.phase == RunPhase.COMPLETED

    def test_complete_sets_completed_at_with_tz(self):
        m = _new_manager(); _init(m, "rco2")
        m.start_run("rco2")
        p = m.complete_run("rco2")
        assert p.completed_at is not None
        assert p.completed_at.tzinfo == timezone.utc


class TestFailRun:
    def test_fail_records_error_code(self):
        m = _new_manager(); _init(m, "rf1")
        m.start_run("rf1")
        p = m.fail_run("rf1", error="crash", failure_code="LOOP_CRASH")
        assert p.status == RunLifecycleState.FAILED
        assert p.failure_details["error_code"] == "LOOP_CRASH"

    def test_fail_does_not_expose_oracle_formula(self):
        m = _new_manager(); _init(m, "rf2")
        m.start_run("rf2")
        p = m.fail_run("rf2", error="oracle mismatch", failure_code="ORACLE_FAILURE")
        for key in (p.failure_details or {}):
            assert "oracle_formula" not in key.lower()

    def test_fail_truncates_long_messages(self):
        m = _new_manager(); _init(m, "rf3")
        m.start_run("rf3")
        p = m.fail_run("rf3", error="x" * 1000)
        msg = (p.failure_details or {}).get("error_message", "")
        assert len(msg) <= 515


class TestProgressTracking:
    def test_update_progress_counters(self):
        m = _new_manager(); _init(m, "rp1", max_queries=100, max_evaluations=200)
        m.start_run("rp1")
        p = m.update_progress("rp1", queries_used=10, evaluations_completed=30)
        assert p.queries_used == 10
        assert p.evaluations_completed == 30

    def test_queries_remaining_computed(self):
        m = _new_manager(); _init(m, "rp2", max_queries=50)
        m.start_run("rp2"); m.update_progress("rp2", queries_used=15)
        assert m.get_progress("rp2").queries_remaining == 35

    def test_queries_remaining_none_when_no_max(self):
        m = _new_manager(); _init(m, "rp3")
        m.start_run("rp3")
        assert m.get_progress("rp3").queries_remaining is None

    def test_no_fabricated_percentage_in_to_dict(self):
        m = _new_manager(); _init(m, "rp4")
        m.start_run("rp4")
        d = m.get_progress("rp4").to_dict()
        for key in d:
            assert "percent" not in key.lower()

    def test_terminal_run_ignores_counter_updates(self):
        m = _new_manager(); _init(m, "rp5", max_queries=50)
        m.start_run("rp5"); m.complete_run("rp5")
        p = m.update_progress("rp5", queries_used=99)
        assert p.queries_used == 0

    def test_to_dict_has_required_keys(self):
        m = _new_manager(); _init(m, "rp7")
        d = m.get_progress("rp7").to_dict()
        for key in ("run_id", "status", "phase", "queries_used", "queries_remaining",
                    "evaluations_completed", "elapsed_seconds", "started_at",
                    "completed_at", "current_activity", "stop_reason",
                    "failure_details", "last_updated_at"):
            assert key in d, f"Missing key: {key}"

    def test_to_dict_no_secret_keys(self):
        m = _new_manager(); _init(m, "rp8")
        d = m.get_progress("rp8").to_dict()
        for key in d:
            for bad in ("password", "token", "oracle_formula", "ground_truth"):
                assert bad not in key.lower()


class TestGetProgress:
    def test_get_missing_run_raises(self):
        m = _new_manager()
        with pytest.raises(RunNotFoundError):
            m.get_progress("run-does-not-exist")

    def test_get_existing_run_returns_progress(self):
        m = _new_manager(); _init(m, "rg1")
        assert m.get_progress("rg1").run_id == "rg1"


class TestSanitizeDiagnostics:
    def test_strips_secret_keys(self):
        m = _new_manager()
        safe = m._sanitize_diagnostics({
            "error_code": "FAIL", "token": "bad", "oracle_formula": "y=2x",
            "password": "h2", "error_message": "ok"
        })
        assert "error_code" in safe and "error_message" in safe
        assert "token" not in safe and "oracle_formula" not in safe and "password" not in safe

    def test_truncates_long_string_values(self):
        m = _new_manager()
        safe = m._sanitize_diagnostics({"trace": "A" * 1000})
        assert len(safe["trace"]) <= 515


class TestRetryPolicy:
    def test_zero_attempt_returns_zero_delay(self):
        assert RetryPolicy().calculate_delay(0) == 0.0

    def test_first_attempt_delay(self):
        assert RetryPolicy(initial_delay_seconds=1.0, backoff_multiplier=2.0).calculate_delay(1) == 1.0

    def test_second_attempt_doubles(self):
        assert RetryPolicy(initial_delay_seconds=1.0, backoff_multiplier=2.0).calculate_delay(2) == 2.0

    def test_third_attempt_quadruples(self):
        assert RetryPolicy(initial_delay_seconds=1.0, backoff_multiplier=2.0).calculate_delay(3) == 4.0

    def test_delay_capped_at_max(self):
        assert RetryPolicy(initial_delay_seconds=1.0, backoff_multiplier=2.0, max_delay_seconds=3.0).calculate_delay(10) == 3.0

    def test_classify_timeout_as_retryable(self):
        assert RetryPolicy().classify_error(TimeoutError("connection timeout")) == ErrorCategory.RETRYABLE

    def test_classify_invalid_as_non_retryable(self):
        assert RetryPolicy().classify_error(ValueError("invalid config")) == ErrorCategory.NON_RETRYABLE

    def test_classify_deadlock_as_retryable(self):
        assert RetryPolicy().classify_error(Exception("database deadlock detected")) == ErrorCategory.RETRYABLE


class TestThreadSafety:
    def test_concurrent_updates_do_not_raise(self):
        m = _new_manager(); _init(m, "rt1", max_queries=1000)
        m.start_run("rt1")
        errors = []

        def increment(i):
            try:
                m.update_progress("rt1", queries_used=i, activity=f"step-{i}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=increment, args=(i,)) for i in range(1, 20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert errors == []

    def test_concurrent_initialize_only_one_succeeds(self):
        m = _new_manager()
        successes, failures = [], []

        def try_init():
            try:
                m.initialize_run("rt2", name="Concurrent")
                successes.append(1)
            except DuplicateExecutionError:
                failures.append(1)

        threads = [threading.Thread(target=try_init) for _ in range(10)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert len(successes) == 1
        assert len(failures) == 9


class TestPhaseUpdates:
    def test_transition_can_set_phase(self):
        m = _new_manager(); _init(m, "rph1")
        p = m.transition_state("rph1", RunLifecycleState.RUNNING, phase=RunPhase.CANDIDATE_GENERATION)
        assert p.phase == RunPhase.CANDIDATE_GENERATION

    def test_completion_sets_completed_phase(self):
        m = _new_manager(); _init(m, "rph3")
        m.start_run("rph3")
        assert m.complete_run("rph3").phase == RunPhase.COMPLETED

    def test_cancellation_sets_terminated_phase(self):
        m = _new_manager(); _init(m, "rph4")
        assert m.request_cancellation("rph4").phase == RunPhase.TERMINATED
