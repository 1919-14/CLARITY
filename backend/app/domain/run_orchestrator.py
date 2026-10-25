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

            # Persist scientific output artifacts (observations, candidate hypotheses, evaluations)
            _persist_run_artifacts(session, run_id, result)

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


def _persist_run_artifacts(session: Any, run_id: str, result: Any) -> None:
    """Save generated observations, hypotheses, revisions, and evaluations into DB session."""
    if not session or not result:
        return
    try:
        from ..adapters.persistence.models import (
            EvaluationResultModel,
            HypothesisModel,
            HypothesisRevisionModel,
            ObservationModel,
        )
        from .contracts import thaw_payload

        # Persist observations
        observations = getattr(result, "observations", None) or ()
        for seq, obs in enumerate(observations, start=1):
            obs_id = getattr(obs, "observation_id", None)
            if not obs_id:
                continue
            existing = session.query(ObservationModel).filter_by(observation_id=obs_id).first()
            if not existing:
                session.add(
                    ObservationModel(
                        observation_id=obs_id,
                        run_id=run_id,
                        input_id=getattr(obs, "input_id", f"inp-{seq}"),
                        input_values=thaw_payload(getattr(obs, "input_values", {})),
                        output_values=thaw_payload(getattr(obs, "output_values", None)) if getattr(obs, "output_values", None) else None,
                        status=getattr(obs.status, "value", str(obs.status)),
                        sequence_number=seq,
                        is_held_out=bool(getattr(obs, "is_held_out", False)),
                    )
                )

        # Persist hypotheses & revisions
        hypotheses = getattr(result, "hypotheses", None) or ()
        for hyp in hypotheses:
            hyp_id = getattr(hyp, "hypothesis_id", None)
            if not hyp_id:
                continue
            existing_h = session.query(HypothesisModel).filter_by(hypothesis_id=hyp_id).first()
            rev_num = getattr(hyp, "revision", 1) or 1
            if not existing_h:
                session.add(
                    HypothesisModel(
                        hypothesis_id=hyp_id,
                        run_id=run_id,
                        model_family=getattr(hyp, "model_family", "polynomial"),
                        current_revision=rev_num,
                    )
                )
                session.add(
                    HypothesisRevisionModel(
                        revision_id=f"rev-{hyp_id}-{rev_num}",
                        hypothesis_id=hyp_id,
                        revision_number=rev_num,
                        formula=getattr(hyp, "formula", str(hyp)),
                        parameters=thaw_payload(getattr(hyp, "parameters", {})),
                        complexity=getattr(hyp, "complexity", 1),
                    )
                )

        # Persist evaluations
        evaluations = getattr(result, "evaluations", None) or ()
        for ev in evaluations:
            ev_id = getattr(ev, "evaluation_id", None)
            if not ev_id:
                continue
            existing_ev = session.query(EvaluationResultModel).filter_by(evaluation_id=ev_id).first()
            if not existing_ev:
                session.add(
                    EvaluationResultModel(
                        evaluation_id=ev_id,
                        revision_id=f"rev-{getattr(ev, 'hypothesis_id', '')}-1",
                        run_id=run_id,
                        status=getattr(ev.status, "value", str(ev.status)),
                        sample_count=getattr(ev, "sample_count", 0),
                        dataset_version=getattr(ev, "dataset_version", 1) or 1,
                        metrics=thaw_payload(getattr(ev, "metrics", {})),
                        held_out_metrics=thaw_payload(getattr(ev, "held_out_metrics", None)) if getattr(ev, "held_out_metrics", None) else None,
                    )
                )

        session.flush()
    except Exception as exc:
        logger.warning("Could not persist discovery run artifacts for '%s': %s", run_id, exc)

