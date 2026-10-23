"""Run Orchestration Service.

Connects RunLifecycleManager to actual execution via DiscoveryEngine,
propagates progress counters, checks for cancellation before scheduling work,
and classifies/handles failures with the bounded RetryPolicy.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Sequence
from datetime import datetime, timezone

from .contracts import Observation
from .discovery_contracts import (
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryRunResult,
    DiscoveryTerminationReason,
)
from .discovery_engine import DiscoveryEngine
from .run_lifecycle import (
    ErrorCategory,
    InvalidStateTransitionError,
    RetryPolicy,
    RunLifecycleManager,
    RunLifecycleState,
    RunPhase,
    default_lifecycle_manager,
)

logger = logging.getLogger(__name__)


class RunOrchestrator:
    """Coordinates lifecycle status, real work execution, and failure/retry routing."""

    def __init__(
        self,
        lifecycle_manager: Optional[RunLifecycleManager] = None,
        retry_policy: Optional[RetryPolicy] = None,
    ):
        self.lifecycle_manager = lifecycle_manager or default_lifecycle_manager
        self.retry_policy = retry_policy or RetryPolicy()

    def execute_run(
        self,
        run_id: str,
        engine: DiscoveryEngine,
        initial_observations: Sequence[Observation] = (),
        session_factory: Optional[Callable[[], Any]] = None,
    ) -> Optional[DiscoveryRunResult]:
        """Execute a discovery run through the lifecycle manager and discovery engine.

        1. Starts the run in the lifecycle manager (transitions QUEUED -> RUNNING).
        2. Monitors progress and cancellation requests before and during iterations.
        3. Synchronizes terminal state (COMPLETED, STOPPED_BY_BUDGET, CANCELLED, or FAILED).
        """
        def _get_session():
            return session_factory() if session_factory else None

        # Register cancellation hooks and token on the engine so in-flight execution halts immediately upon cancellation
        if hasattr(engine, "request_cancellation"):
            self.lifecycle_manager.register_cancellation_hook(
                run_id, lambda r_id=None: engine.request_cancellation()
            )
        elif hasattr(engine, "cancel"):
            self.lifecycle_manager.register_cancellation_hook(
                run_id, lambda r_id=None: engine.cancel()
            )

        if hasattr(engine, "set_cancellation_token"):
            engine.set_cancellation_token(lambda: self.lifecycle_manager.is_cancellation_requested(run_id))

        # Check cancellation before starting computation
        if self.lifecycle_manager.is_cancellation_requested(run_id):
            session = _get_session()
            try:
                self.lifecycle_manager.confirm_cancellation(
                    run_id, reason="Cancelled before execution started", session=session
                )
            finally:
                if session:
                    session.close()
            return None

        session = _get_session()
        try:
            progress = self.lifecycle_manager.get_progress(run_id, session=session)
            if progress.status != RunLifecycleState.RUNNING:
                self.lifecycle_manager.start_run(run_id, session=session)
        except Exception as exc:
            logger.error("Failed to start run '%s': %s", run_id, exc)
            raise
        finally:
            if session:
                session.close()

        # Check cancellation again before launching loop
        if self.lifecycle_manager.is_cancellation_requested(run_id):
            session = _get_session()
            try:
                self.lifecycle_manager.confirm_cancellation(
                    run_id, reason="Cancelled before execution started", session=session
                )
            finally:
                if session:
                    session.close()
            return None

        # Execute discovery loop with retry and error routing
        try:
            result = self.retry_policy.execute_with_retry(
                action=lambda: self._run_engine_with_lifecycle_tracking(
                    run_id, engine, initial_observations, session_factory
                ),
                on_retry=lambda attempt, exc, delay: logger.warning(
                    "Retrying run '%s' (attempt %d/%d) after error: %s (backoff %.1fs)",
                    run_id, attempt, self.retry_policy.max_attempts, exc, delay
                ),
            )
            return result
        except Exception as exc:
            if self.lifecycle_manager.is_cancellation_requested(run_id):
                session = _get_session()
                try:
                    self.lifecycle_manager.confirm_cancellation(
                        run_id, reason="Cancelled during discovery execution", session=session
                    )
                finally:
                    if session:
                        session.close()
                return None
            session = _get_session()
            try:
                category = self.retry_policy.classify_error(exc)
                fail_code = "TRANSIENT_FAILURE_EXHAUSTED" if category == ErrorCategory.RETRYABLE else "EXECUTION_FAILURE"
                self.lifecycle_manager.fail_run(
                    run_id, error=exc, failure_code=fail_code, session=session
                )
            finally:
                if session:
                    session.close()
            raise

    def _run_engine_with_lifecycle_tracking(
        self,
        run_id: str,
        engine: DiscoveryEngine,
        initial_observations: Sequence[Observation],
        session_factory: Optional[Callable[[], Any]],
    ) -> Optional[DiscoveryRunResult]:
        """Internal worker executing engine while checking cancellation and updating progress."""
        session = session_factory() if session_factory else None
        try:
            self.lifecycle_manager.update_progress(
                run_id,
                phase=RunPhase.CANDIDATE_GENERATION,
                activity="Generating candidate hypotheses",
                session=session,
            )
        finally:
            if session:
                session.close()

        if self.lifecycle_manager.is_cancellation_requested(run_id):
            session = session_factory() if session_factory else None
            try:
                self.lifecycle_manager.confirm_cancellation(
                    run_id, reason="Cancelled by user request", session=session
                )
            finally:
                if session:
                    session.close()
            return None

        cancellation_token = lambda: self.lifecycle_manager.is_cancellation_requested(run_id)
        try:
            result = engine.run(initial_observations, cancellation_token=cancellation_token)
        except TypeError:
            result = engine.run(initial_observations)

        if result is None:
            if self.lifecycle_manager.is_cancellation_requested(run_id):
                session = session_factory() if session_factory else None
                try:
                    self.lifecycle_manager.confirm_cancellation(
                        run_id, reason="Cancelled during discovery execution", session=session
                    )
                finally:
                    if session:
                        session.close()
            return None

        # Synchronize terminal result with lifecycle manager
        session = session_factory() if session_factory else None
        try:
            accounting = getattr(result, "query_accounting", None)
            queries_count = getattr(accounting, "attempted_queries", getattr(accounting, "total_queries", 0)) if accounting else 0
            evals = getattr(result, "evaluations", None) or getattr(result, "final_evaluations", ())
            evals_count = len(evals) if evals else 0

            self.lifecycle_manager.update_progress(
                run_id,
                phase=RunPhase.REPORT_GENERATION,
                queries_used=queries_count,
                evaluations_completed=evals_count,
                activity="Finalizing discovery run results",
                session=session,
            )

            if (
                self.lifecycle_manager.is_cancellation_requested(run_id)
                or getattr(result, "status", None) == DiscoveryLifecycleStatus.TERMINATED_CANCELLED
            ):
                self.lifecycle_manager.confirm_cancellation(
                    run_id, reason="Cancelled during discovery execution", session=session
                )
            elif result.status == DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED:
                self.lifecycle_manager.stop_by_budget(
                    run_id, reason="Resource query budget ceiling reached", session=session
                )
            elif result.status in (
                DiscoveryLifecycleStatus.TERMINATED_STOPPING_CRITERION,
                DiscoveryLifecycleStatus.TERMINATED_MAX_ITERATIONS,
                DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS,
            ):
                stop_msg = f"Completed: {result.termination_reason.value if result.termination_reason else 'stopping criterion met'}"
                self.lifecycle_manager.complete_run(run_id, reason=stop_msg, session=session)
            elif result.status == DiscoveryLifecycleStatus.FAILED:
                err_msg = "; ".join(e.message for e in result.errors) if result.errors else "Discovery engine reported failure"
                self.lifecycle_manager.fail_run(
                    run_id, error=err_msg, failure_code="DISCOVERY_ENGINE_FAILURE", session=session
                )
        finally:
            if session:
                session.close()

        return result
