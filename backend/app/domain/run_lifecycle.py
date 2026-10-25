"""CLARITY Run Lifecycle Management and Status Tracking.

Defines the explicit state machine, valid state transitions, progress tracking,
failure classification, bounded retry policy, and recovery for discovery runs.
Preserves scientific history, prevents fabricated progress percentages, and
coordinates persistence and orchestration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Set
import uuid

# NOTE: app.adapters imports are intentionally deferred to method bodies.
# Importing them at module level would violate the pure-domain isolation
# contract enforced by test_evaluation_module_does_not_import_oracle_or_evaluator
# and test_generation_module_does_not_import_oracle_or_evaluator.
# All adapter imports happen only when a DB session is explicitly passed in.


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ==============================================================================
# 1. Lifecycle States and Phases
# ==============================================================================

class RunLifecycleState(str, Enum):
    """Explicit lifecycle states for a CLARITY discovery run.

    State progression:
      QUEUED -> RUNNING -> COMPLETED
                        -> STOPPED_BY_BUDGET
                        -> CANCELLING -> CANCELLED
                        -> FAILED
    """

    QUEUED = "queued"
    PENDING = "pending"  # Synonymous with QUEUED for backward compatibility
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    STOPPED_BY_BUDGET = "stopped_by_budget"
    BUDGET_EXHAUSTED = "budget_exhausted"  # Synonymous with STOPPED_BY_BUDGET
    CANCELLED = "cancelled"
    FAILED = "failed"


# Canonical normalized representation of states
NORMALIZED_STATES = {
    RunLifecycleState.PENDING: RunLifecycleState.QUEUED,
    RunLifecycleState.BUDGET_EXHAUSTED: RunLifecycleState.STOPPED_BY_BUDGET,
}


def normalize_state(state: RunLifecycleState | str) -> RunLifecycleState:
    """Normalize state aliases (e.g. pending -> queued, budget_exhausted -> stopped_by_budget)."""
    if isinstance(state, str):
        try:
            state = RunLifecycleState(state.lower())
        except ValueError:
            raise ValueError(f"Unknown lifecycle state: {state}")
    return NORMALIZED_STATES.get(state, state)


class RunPhase(str, Enum):
    """Granular execution phase within an active or terminal discovery run."""

    CREATED = "created"
    INITIALIZING = "initializing"
    CANDIDATE_GENERATION = "candidate_generation"
    CANDIDATE_EVALUATION = "candidate_evaluation"
    EXPERIMENT_SELECTION = "experiment_selection"
    ORACLE_QUERY = "oracle_query"
    OBSERVATION_PERSISTENCE = "observation_persistence"
    REPORT_GENERATION = "report_generation"
    COMPLETED = "completed"
    TERMINATED = "terminated"


# Terminal states from which no further transitions are permitted
TERMINAL_STATES: Set[RunLifecycleState] = {
    RunLifecycleState.COMPLETED,
    RunLifecycleState.STOPPED_BY_BUDGET,
    RunLifecycleState.BUDGET_EXHAUSTED,
    RunLifecycleState.CANCELLED,
    RunLifecycleState.FAILED,
}


# Permitted state transitions graph
PERMITTED_TRANSITIONS: Dict[RunLifecycleState, Set[RunLifecycleState]] = {
    RunLifecycleState.QUEUED: {
        RunLifecycleState.RUNNING,
        RunLifecycleState.CANCELLING,
        RunLifecycleState.CANCELLED,
        RunLifecycleState.FAILED,
    },
    RunLifecycleState.PENDING: {
        RunLifecycleState.RUNNING,
        RunLifecycleState.CANCELLING,
        RunLifecycleState.CANCELLED,
        RunLifecycleState.FAILED,
    },
    RunLifecycleState.RUNNING: {
        RunLifecycleState.CANCELLING,
        RunLifecycleState.COMPLETED,
        RunLifecycleState.STOPPED_BY_BUDGET,
        RunLifecycleState.BUDGET_EXHAUSTED,
        RunLifecycleState.CANCELLED,
        RunLifecycleState.FAILED,
    },
    RunLifecycleState.CANCELLING: {
        RunLifecycleState.CANCELLED,
        RunLifecycleState.FAILED,
    },
    RunLifecycleState.COMPLETED: set(),
    RunLifecycleState.STOPPED_BY_BUDGET: set(),
    RunLifecycleState.BUDGET_EXHAUSTED: set(),
    RunLifecycleState.CANCELLED: set(),
    RunLifecycleState.FAILED: set(),
}


# ==============================================================================
# 2. Exceptions
# ==============================================================================

class RunLifecycleError(Exception):
    """Base exception for lifecycle management failures."""


class InvalidStateTransitionError(RunLifecycleError):
    """Raised when an illegal state transition is attempted."""

    def __init__(self, from_state: str, to_state: str, reason: str = ""):
        msg = f"Invalid state transition from '{from_state}' to '{to_state}'."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg)
        self.from_state = from_state
        self.to_state = to_state


class DuplicateExecutionError(RunLifecycleError):
    """Raised when attempting to execute or start an already-active or terminal run."""


class RunNotFoundError(RunLifecycleError):
    """Raised when operating on a non-existent run ID."""


# ==============================================================================
# 3. Error Classification and Retry Policy
# ==============================================================================

class ErrorCategory(str, Enum):
    """Classification of errors for bounded retry decisions."""

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"


@dataclass(frozen=True)
class RetryPolicy:
    """Configurable bounded retry policy with backoff and retry execution."""

    max_attempts: int = 3
    initial_delay_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_delay_seconds: float = 5.0

    def classify_error(self, exc: Exception) -> ErrorCategory:
        """Classify an exception as retryable or non-retryable."""
        exc_name = type(exc).__name__.lower()
        msg = str(exc).lower()

        # Transient connection/timeout issues are retryable
        retryable_keywords = ["timeout", "connectionreset", "eagain", "temporarily unavailable", "transient", "deadlock"]
        if any(kw in exc_name or kw in msg for kw in retryable_keywords):
            return ErrorCategory.RETRYABLE

        # Explicit non-retryable errors
        non_retryable_keywords = [
            "invalid", "unsupported", "bad request", "syntax", "exhausted",
            "duplicate", "not found", "forbidden", "unauthorized", "leakage"
        ]
        if any(kw in exc_name or kw in msg for kw in non_retryable_keywords):
            return ErrorCategory.NON_RETRYABLE

        return ErrorCategory.NON_RETRYABLE

    def calculate_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay for the given attempt index (0-based)."""
        if attempt <= 0:
            return 0.0
        delay = self.initial_delay_seconds * (self.backoff_multiplier ** (attempt - 1))
        return min(delay, self.max_delay_seconds)

    def execute_with_retry(self, action: Callable[[], Any], on_retry: Optional[Callable[[int, Exception, float], None]] = None) -> Any:
        """Execute a callable with bounded exponential backoff retries for retryable exceptions."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return action()
            except Exception as exc:
                last_exc = exc
                category = self.classify_error(exc)
                if category != ErrorCategory.RETRYABLE or attempt >= self.max_attempts:
                    raise
                import time
                delay = self.calculate_delay(attempt)
                if on_retry:
                    on_retry(attempt, exc, delay)
                if delay > 0:
                    time.sleep(delay)
        if last_exc:
            raise last_exc


# ==============================================================================
# 4. Progress and Status Representation
# ==============================================================================

@dataclass
class RunProgress:
    """Accurate progress snapshot without fabricated percentages."""

    run_id: str
    status: RunLifecycleState
    phase: RunPhase = RunPhase.CREATED
    queries_used: int = 0
    max_queries: Optional[int] = None
    evaluations_completed: int = 0
    max_evaluations: Optional[int] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    current_activity: Optional[str] = None
    stop_reason: Optional[str] = None
    failure_details: Optional[Dict[str, Any]] = None
    last_updated_at: datetime = field(default_factory=_utc_now)

    @property
    def queries_remaining(self) -> Optional[int]:
        if self.max_queries is None:
            return None
        return max(0, self.max_queries - self.queries_used)

    @property
    def evaluations_remaining(self) -> Optional[int]:
        if self.max_evaluations is None:
            return None
        return max(0, self.max_evaluations - self.evaluations_completed)

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at:
            return 0.0
        end_time = self.completed_at or _utc_now()
        return max(0.0, (end_time - self.started_at).total_seconds())

    def to_dict(self) -> Dict[str, Any]:
        """Serialize progress safely using explicit allowlist without secrets or stack traces."""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "phase": self.phase.value,
            "queries_used": self.queries_used,
            "queries_remaining": self.queries_remaining,
            "evaluations_completed": self.evaluations_completed,
            "evaluations_remaining": self.evaluations_remaining,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "current_activity": self.current_activity,
            "stop_reason": self.stop_reason,
            "failure_details": self.failure_details,
            "last_updated_at": self.last_updated_at.isoformat(),
        }


# ==============================================================================
# 5. Run Lifecycle Manager
# ==============================================================================

class RunLifecycleManager:
    """Coordinates lifecycle transitions, persistence, progress tracking, and failure handling.

    Thread-safe per run ID using reentrant locks.
    """

    def __init__(self, repository_factory=None, retry_policy: Optional[RetryPolicy] = None):
        self._repo_factory = repository_factory
        self._retry_policy = retry_policy or RetryPolicy()
        self._progress_registry: Dict[str, RunProgress] = {}
        self._run_locks: Dict[str, threading.RLock] = {}
        self._global_lock = threading.RLock()
        self._cancellation_events: Dict[str, threading.Event] = {}
        self._cancellation_hooks: Dict[str, List[Callable[[], Any]]] = {}

    def _get_lock(self, run_id: str) -> threading.RLock:
        with self._global_lock:
            if run_id not in self._run_locks:
                self._run_locks[run_id] = threading.RLock()
            return self._run_locks[run_id]

    def deregister_run(self, run_id: str) -> None:
        """Remove a run from in-memory registry and cleanup locks (used on creation rollback)."""
        with self._global_lock:
            self._progress_registry.pop(run_id, None)
            self._run_locks.pop(run_id, None)
            self._cancellation_events.pop(run_id, None)
            self._cancellation_hooks.pop(run_id, None)

    def is_cancellation_requested(self, run_id: str) -> bool:
        """Check if cancellation has been requested for a run."""
        with self._get_lock(run_id):
            evt = self._cancellation_events.get(run_id)
            if evt and evt.is_set():
                return True
            prog = self._progress_registry.get(run_id)
            if prog and prog.status == RunLifecycleState.CANCELLING:
                return True
            return False

    def register_cancellation_hook(self, run_id: str, hook: Callable[..., Any]) -> None:
        """Register a callback to be invoked when cancellation is requested."""
        with self._get_lock(run_id):
            if run_id not in self._cancellation_hooks:
                self._cancellation_hooks[run_id] = []
            self._cancellation_hooks[run_id].append(hook)
            if self.is_cancellation_requested(run_id):
                try:
                    hook(run_id)
                except TypeError:
                    try:
                        hook()
                    except Exception:
                        pass
                except Exception:
                    pass


    # --------------------------------------------------------------------------
    # Lifecycle Transitions
    # --------------------------------------------------------------------------

    def initialize_run(
        self,
        run_id: str,
        name: str,
        max_queries: Optional[int] = None,
        max_evaluations: Optional[int] = None,
        domain: str = "default",
        session=None,
    ) -> RunProgress:
        """Initialize a new run in the QUEUED state."""
        with self._get_lock(run_id):
            if run_id in self._progress_registry:
                raise DuplicateExecutionError(f"Run '{run_id}' already initialized in lifecycle manager.")

            progress = RunProgress(
                run_id=run_id,
                status=RunLifecycleState.QUEUED,
                phase=RunPhase.CREATED,
                max_queries=max_queries,
                max_evaluations=max_evaluations,
                current_activity="Run initialized; queued for execution",
            )
            self._progress_registry[run_id] = progress

            if session:
                from ..adapters.persistence.repository import ClarityRepository
                from ..adapters.persistence.models import RunModel
                repo = ClarityRepository(session)
                existing = repo.get_run(run_id)
                if not existing:
                    run_model = RunModel(
                        run_id=run_id,
                        name=name,
                        domain=domain,
                        status=RunLifecycleState.QUEUED.value,
                        max_queries=max_queries,
                        max_evaluations=max_evaluations,
                    )
                    repo.create_run(run_model)
                else:
                    repo.update_run_status(run_id, RunLifecycleState.QUEUED.value)

            return progress

    def transition_state(
        self,
        run_id: str,
        target_state: RunLifecycleState | str,
        phase: Optional[RunPhase] = None,
        stop_reason: Optional[str] = None,
        failure_details: Optional[Dict[str, Any]] = None,
        activity: Optional[str] = None,
        session=None,
    ) -> RunProgress:
        """Validate and execute a state transition.

        Raises InvalidStateTransitionError if the requested transition is illegal.
        """
        target_state = normalize_state(target_state)

        with self._get_lock(run_id):
            progress = self._get_or_load_progress(run_id, session=session)
            current_state = normalize_state(progress.status)

            # Idempotent no-op if transitioning to current state
            if current_state == target_state:
                if phase:
                    progress.phase = phase
                if activity:
                    progress.current_activity = activity
                if stop_reason:
                    progress.stop_reason = stop_reason
                progress.last_updated_at = _utc_now()
                return progress

            # Validate permitted transition
            permitted = PERMITTED_TRANSITIONS.get(current_state, set())
            if target_state not in permitted:
                raise InvalidStateTransitionError(
                    from_state=current_state.value,
                    to_state=target_state.value,
                    reason=f"Transition from '{current_state.value}' to '{target_state.value}' is not permitted.",
                )

            # Apply state update
            now = _utc_now()
            progress.status = target_state
            if phase:
                progress.phase = phase
            if activity:
                progress.current_activity = self._sanitize_text(activity)
            if stop_reason:
                progress.stop_reason = self._sanitize_text(stop_reason)
            if failure_details:
                # Sanitize failure details to avoid leaking credentials or ground truth
                progress.failure_details = self._sanitize_diagnostics(failure_details)

            # Timestamps
            if target_state == RunLifecycleState.RUNNING and not progress.started_at:
                progress.started_at = now
            elif target_state in TERMINAL_STATES and not progress.completed_at:
                progress.completed_at = now
                if not phase:
                    progress.phase = RunPhase.TERMINATED

            progress.last_updated_at = now

            # Sync to persistence if session available
            if session:
                from ..adapters.persistence.repository import ClarityRepository
                from ..adapters.persistence.models import AuditEventModel
                repo = ClarityRepository(session)
                run_model = repo.get_run(run_id)
                if run_model:
                    run_model.status = target_state.value
                    if progress.stop_reason:
                        run_model.stop_reason = progress.stop_reason
                    if progress.started_at and not run_model.started_at:
                        run_model.started_at = progress.started_at
                    if progress.completed_at and not run_model.completed_at:
                        run_model.completed_at = progress.completed_at

                    # Record audit event with unique ID to avoid collisions
                    event_id = f"evt-lc-{uuid.uuid4().hex[:12]}"
                    metadata = {
                        "from_state": current_state.value,
                        "to_state": target_state.value,
                        "stop_reason": progress.stop_reason,
                        "previous_status": current_state.value,
                    }
                    if progress.failure_details:
                        metadata["failure_details"] = progress.failure_details

                    repo.record_audit_event(
                        AuditEventModel(
                            event_id=event_id,
                            run_id=run_id,
                            event_type=f"RUN_{target_state.value.upper()}",
                            actor="lifecycle_manager",
                            metadata_json=metadata,
                        )
                    )
                    session.flush()

            return progress

    # --------------------------------------------------------------------------
    # Specialized Lifecycle Actions
    # --------------------------------------------------------------------------

    def start_run(self, run_id: str, session=None) -> RunProgress:
        """Start execution of a queued run under one atomic lock acquisition."""
        with self._get_lock(run_id):
            progress = self._get_or_load_progress(run_id, session=session)
            if progress.status == RunLifecycleState.RUNNING:
                raise DuplicateExecutionError(f"Run '{run_id}' is already actively running.")
            if progress.status in TERMINAL_STATES:
                raise DuplicateExecutionError(f"Run '{run_id}' is already in terminal state '{progress.status.value}'.")
            if progress.status == RunLifecycleState.CANCELLING:
                raise InvalidStateTransitionError(
                    from_state=progress.status.value,
                    to_state=RunLifecycleState.RUNNING.value,
                    reason="Run is currently being cancelled and cannot be started.",
                )

            return self.transition_state(
                run_id,
                target_state=RunLifecycleState.RUNNING,
                phase=RunPhase.INITIALIZING,
                activity="Starting discovery loop",
                session=session,
            )

    def update_progress(
        self,
        run_id: str,
        phase: Optional[RunPhase] = None,
        queries_used: Optional[int] = None,
        evaluations_completed: Optional[int] = None,
        activity: Optional[str] = None,
        session=None,
    ) -> RunProgress:
        """Update activity and work counters without fabricating percentages, persisting durably to DB."""
        with self._get_lock(run_id):
            progress = self._get_or_load_progress(run_id, session=session)
            if progress.status in TERMINAL_STATES:
                # Terminal runs do not accept in-flight counter increments
                return progress

            if phase:
                progress.phase = phase
            if queries_used is not None:
                progress.queries_used = max(progress.queries_used or 0, queries_used)
            if evaluations_completed is not None:
                progress.evaluations_completed = max(progress.evaluations_completed or 0, evaluations_completed)
            if activity:
                progress.current_activity = self._sanitize_text(activity)
            progress.last_updated_at = _utc_now()

            if session:
                from ..adapters.persistence.repository import ClarityRepository
                from ..adapters.persistence.models import AuditEventModel, UsageRecordModel
                repo = ClarityRepository(session)

                # Durably persist progress to usage_records table with safe monotonic counter merging
                latest_usage = repo.get_latest_usage_for_run(run_id)
                queries_count = progress.queries_used
                evals_count = progress.evaluations_completed
                if latest_usage:
                    if queries_count is not None:
                        latest_usage.queries_executed = max(latest_usage.queries_executed or 0, queries_count)
                    if evals_count is not None:
                        latest_usage.evaluations_completed = max(latest_usage.evaluations_completed or 0, evals_count)
                        if latest_usage.evaluations_scheduled is not None:
                            latest_usage.evaluations_scheduled = max(
                                latest_usage.evaluations_scheduled, latest_usage.evaluations_completed
                            )
                    latest_usage.recorded_at = progress.last_updated_at
                else:
                    repo.record_usage(
                        UsageRecordModel(
                            usage_id=f"use-prog-{uuid.uuid4().hex[:10]}",
                            run_id=run_id,
                            queries_executed=queries_count or 0,
                            evaluations_scheduled=evals_count or 0,
                            evaluations_completed=evals_count or 0,
                            estimated_cost_usd=0.0,
                            live_accruing_cost_usd=0.0,
                            settled_cost_usd=None,
                            is_budget_exhausted=False,
                            recorded_at=progress.last_updated_at,
                        )
                    )

                # Persist progress event to audit_events
                repo.record_audit_event(
                    AuditEventModel(
                        event_id=f"evt-prog-{uuid.uuid4().hex[:12]}",
                        run_id=run_id,
                        event_type="RUN_PROGRESS",
                        actor="lifecycle_manager",
                        metadata_json={
                            "phase": progress.phase.value,
                            "queries_used": progress.queries_used,
                            "evaluations_completed": progress.evaluations_completed,
                            "activity": progress.current_activity,
                        },
                    )
                )
                session.flush()

            return progress

    def request_cancellation(
        self,
        run_id: str,
        reason: str = "Cancellation requested by researcher",
        session=None,
    ) -> RunProgress:
        """Request cancellation for a run.

        If RUNNING, transitions to CANCELLING, signals cancellation token, and executes hooks.
        It remains in CANCELLING until the orchestrator/runner confirms shutdown.
        If QUEUED/PENDING, transitions directly to CANCELLED.
        If already terminal, raises InvalidStateTransitionError.
        """
        with self._get_lock(run_id):
            progress = self._get_or_load_progress(run_id, session=session)
            current_state = normalize_state(progress.status)

            if current_state in TERMINAL_STATES:
                raise InvalidStateTransitionError(
                    from_state=current_state.value,
                    to_state=RunLifecycleState.CANCELLED.value,
                    reason=f"Run '{run_id}' is already terminal ('{current_state.value}').",
                )

            # Signal cancellation event
            if run_id not in self._cancellation_events:
                self._cancellation_events[run_id] = threading.Event()
            self._cancellation_events[run_id].set()

            # Execute any registered cancellation hooks (e.g. cancelling Nodus compute tasks)
            hooks = self._cancellation_hooks.get(run_id, [])
            for hook in hooks:
                try:
                    hook(run_id)
                except TypeError:
                    try:
                        hook()
                    except Exception:
                        pass
                except Exception:
                    pass

            if current_state == RunLifecycleState.RUNNING:
                # Transition to CANCELLING and hold until orchestrator confirms shutdown
                return self.transition_state(
                    run_id,
                    target_state=RunLifecycleState.CANCELLING,
                    phase=RunPhase.TERMINATED,
                    stop_reason=reason,
                    activity="Cancellation in progress: signaling discovery loop and stopping tasks",
                    session=session,
                )

            # If QUEUED or CANCELLING, finalize to CANCELLED
            return self.transition_state(
                run_id,
                target_state=RunLifecycleState.CANCELLED,
                phase=RunPhase.TERMINATED,
                stop_reason=reason,
                activity="Run cancelled",
                session=session,
            )

    def confirm_cancellation(
        self,
        run_id: str,
        reason: Optional[str] = None,
        session=None,
    ) -> RunProgress:
        """Confirm shutdown of a cancelling run, transitioning state from CANCELLING to CANCELLED."""
        with self._get_lock(run_id):
            progress = self._get_or_load_progress(run_id, session=session)
            if progress.status != RunLifecycleState.CANCELLING:
                # If already cancelled or terminal, return as is
                if progress.status in TERMINAL_STATES:
                    return progress
                raise InvalidStateTransitionError(
                    from_state=progress.status.value,
                    to_state=RunLifecycleState.CANCELLED.value,
                    reason=f"Cannot confirm cancellation of run in state '{progress.status.value}'.",
                )

            final_reason = self._sanitize_text(progress.stop_reason or reason or "Cancellation confirmed after task cleanup")
            return self.transition_state(
                run_id,
                target_state=RunLifecycleState.CANCELLED,
                phase=RunPhase.TERMINATED,
                stop_reason=final_reason,
                activity="Run cancelled: execution confirmed stopped",
                session=session,
            )

    def stop_by_budget(self, run_id: str, reason: str = "Configured resource limit reached", session=None) -> RunProgress:
        """Stop a run due to budget exhaustion."""
        return self.transition_state(
            run_id,
            target_state=RunLifecycleState.STOPPED_BY_BUDGET,
            phase=RunPhase.TERMINATED,
            stop_reason=reason,
            activity="Stopped: Resource budget ceiling reached",
            session=session,
        )

    def complete_run(self, run_id: str, reason: str = "Discovery stopping criterion reached", session=None) -> RunProgress:
        """Complete a run successfully."""
        return self.transition_state(
            run_id,
            target_state=RunLifecycleState.COMPLETED,
            phase=RunPhase.COMPLETED,
            stop_reason=reason,
            activity="Discovery completed successfully",
            session=session,
        )

    def fail_run(
        self,
        run_id: str,
        error: Exception | str,
        failure_code: str = "EXECUTION_FAILURE",
        diagnostics: Optional[Dict[str, Any]] = None,
        session=None,
    ) -> RunProgress:
        """Fail a run and record structured, sanitized diagnostic context."""
        safe_reason = self._sanitize_text(str(error)) or "Unknown failure"
        diag = {
            "error_code": failure_code,
            "error_message": safe_reason,
            "timestamp": _utc_now().isoformat(),
        }
        if diagnostics and isinstance(diagnostics, dict):
            diag.update(self._sanitize_diagnostics(diagnostics))
        return self.transition_state(
            run_id,
            target_state=RunLifecycleState.FAILED,
            phase=RunPhase.TERMINATED,
            stop_reason=safe_reason,
            failure_details=diag,
            activity=f"Failed: {safe_reason}",
            session=session,
        )

    # --------------------------------------------------------------------------
    # Interrupted Runs / System Restart Recovery
    # --------------------------------------------------------------------------

    def recover_interrupted_runs(self, session) -> List[str]:
        """Detect and explicitly mark runs that were left in active state across restart.

        Does not silently convert interrupted runs to completed or delete scientific results.
        Marks them as FAILED with an explicit diagnostic explanation and records audit events.
        """
        from sqlalchemy import select
        from ..adapters.persistence.models import AuditEventModel, RunModel

        active_statuses = [RunLifecycleState.RUNNING.value, RunLifecycleState.CANCELLING.value]
        stmt = select(RunModel).where(RunModel.status.in_(active_statuses))
        interrupted_runs = list(session.scalars(stmt).all())

        recovered_ids = []
        now = _utc_now()
        for run in interrupted_runs:
            run_id = run.run_id
            prior_state = run.status
            reason = "Interrupted by system restart; marked for researcher review"
            run.status = RunLifecycleState.FAILED.value
            run.stop_reason = reason
            run.completed_at = now
            recovered_ids.append(run_id)

            fail_diag = {
                "error_code": "SYSTEM_RESTART_INTERRUPTED",
                "error_message": reason,
                "prior_state": prior_state,
                "interrupted_at": now.isoformat(),
            }

            # Update in-memory snapshot if present
            if run_id in self._progress_registry:
                prog = self._progress_registry[run_id]
                prog.status = RunLifecycleState.FAILED
                prog.phase = RunPhase.TERMINATED
                prog.stop_reason = reason
                prog.completed_at = now
                prog.current_activity = reason
                prog.failure_details = fail_diag
                prog.last_updated_at = now

            # Record audit event
            session.add(
                AuditEventModel(
                    event_id=f"evt-recov-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    event_type="RUN_FAILED",
                    actor="system_recovery",
                    metadata_json={
                        "from_state": prior_state,
                        "to_state": RunLifecycleState.FAILED.value,
                        "stop_reason": reason,
                        "failure_details": fail_diag,
                    },
                )
            )

        if interrupted_runs:
            session.flush()

        return recovered_ids

    # --------------------------------------------------------------------------
    # Queries & Helpers
    # --------------------------------------------------------------------------

    def get_progress(self, run_id: str, session=None) -> RunProgress:
        """Get the current progress snapshot for a run."""
        with self._get_lock(run_id):
            return self._get_or_load_progress(run_id, session=session)

    def _get_or_load_progress(self, run_id: str, session=None) -> RunProgress:
        """Internal helper to fetch progress from memory or reconstruct from DB."""
        if run_id in self._progress_registry:
            prog = self._progress_registry[run_id]
            if session and prog.status == RunLifecycleState.RUNNING:
                try:
                    from ..adapters.persistence.models import RunBudgetUsageModel
                    usage = session.query(RunBudgetUsageModel).filter_by(run_id=run_id).first()
                    if usage:
                        prog.queries_used = usage.queries_completed
                        prog.evaluations_completed = usage.evaluations_completed
                        if usage.queries_completed > 0:
                            prog.phase = RunPhase.ORACLE_QUERY
                            prog.current_activity = f"Iterating discovery loop (query {usage.queries_completed})"
                except Exception:
                    pass
            return prog

        if session:
            from ..adapters.persistence.repository import ClarityRepository
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if run:
                latest_usage = repo.get_latest_usage_for_run(run_id)
                queries_used = latest_usage.queries_executed if latest_usage else 0
                evaluations_completed = latest_usage.evaluations_completed if latest_usage else 0

                audit_events = repo.list_audit_events_for_run(run_id)
                failure_details = None
                phase = RunPhase.TERMINATED if run.status in [s.value for s in TERMINAL_STATES] else RunPhase.INITIALIZING
                current_activity = None

                for evt in reversed(audit_events):
                    if evt.event_type == "RUN_FAILED" and evt.metadata_json:
                        failure_details = evt.metadata_json.get("failure_details")
                    if evt.event_type == "RUN_PROGRESS" and evt.metadata_json:
                        if not current_activity:
                            current_activity = evt.metadata_json.get("activity")
                        if phase == RunPhase.INITIALIZING and "phase" in evt.metadata_json:
                            try:
                                phase = RunPhase(evt.metadata_json["phase"])
                            except ValueError:
                                pass

                prog = RunProgress(
                    run_id=run.run_id,
                    status=normalize_state(run.status),
                    phase=phase,
                    queries_used=queries_used,
                    max_queries=run.max_queries,
                    evaluations_completed=evaluations_completed,
                    max_evaluations=run.max_evaluations,
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                    current_activity=current_activity or (run.stop_reason if run.status in [s.value for s in TERMINAL_STATES] else None),
                    stop_reason=run.stop_reason,
                    failure_details=failure_details,
                )
                self._progress_registry[run_id] = prog
                return prog

        raise RunNotFoundError(f"Run '{run_id}' not found.")

    @staticmethod
    def _sanitize_text(text: Optional[str]) -> Optional[str]:
        """Strip sensitive credentials, tokens, formulas, and ground truth from text strings."""
        if text is None:
            return None
        if not isinstance(text, str):
            text = str(text)

        # Redact JWT tokens
        sanitized = re.sub(
            r"ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
            "[REDACTED_TOKEN]",
            text,
        )

        # Redact sensitive key-value pairs (password, secret, token, api_key, ground_truth, oracle_formula, etc.)
        pattern = re.compile(
            r"(?i)\b(password|secret|token|api[_-]?key|bearer|ground_truth|oracle_formula|evaluator_only|true_formula)\b(\s*[:=]\s*)(['\"]?[^\s,'\"}]+['\"]?)",
        )
        sanitized = pattern.sub(r"\1\2[REDACTED]", sanitized)

        # Redact Authorization: Bearer <token>
        sanitized = re.sub(
            r"(?i)\b(Bearer\s+)[A-Za-z0-9_\-\.]{8,}\b",
            r"\1[REDACTED]",
            sanitized,
        )

        # Truncate length
        if len(sanitized) > 500:
            sanitized = sanitized[:500] + "... [truncated]"

        return sanitized

    def _sanitize_diagnostics(self, diag: Dict[str, Any]) -> Dict[str, Any]:
        """Strip sensitive keys, redact sensitive text patterns, and truncate long strings."""
        sanitized = {}
        for k, v in diag.items():
            if any(secret_term in k.lower() for secret_term in ["secret", "password", "token", "ground_truth", "oracle_formula", "evaluator_only"]):
                continue
            if isinstance(v, str):
                v = self._sanitize_text(v)
            elif isinstance(v, dict):
                v = self._sanitize_diagnostics(v)
            sanitized[k] = v
        return sanitized


# Global default manager instance for application-wide use
default_lifecycle_manager = RunLifecycleManager()
