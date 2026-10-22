"""Shared service layer for hypothesis management and re-evaluation.

Encapsulates hypothesis proposal, revision tracking, flag toggling, and
re-evaluation execution via the domain EvaluationPipeline or task dispatcher.
"""

from datetime import datetime, timezone
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import BudgetAdmissionDenied
from ..domain.contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
)
from ..domain.evaluation import EvaluationPipeline
from ..domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from .budget_service import complete_hypothesis_generation, reserve_hypotheses


class HypothesisServiceError(Exception):
    """Base exception for hypothesis service operations."""


class HypothesisNotFoundError(HypothesisServiceError):
    """Raised when a hypothesis cannot be found."""


class HypothesisValidationError(HypothesisServiceError):
    """Raised when hypothesis data fails validation."""


class HypothesisBudgetExhaustedError(HypothesisServiceError):
    """Raised when the run's hypothesis-generation limit has been reached."""

    def __init__(self, denial: BudgetAdmissionDenied):
        self.denial = denial
        self.reason = denial.reason
        super().__init__(denial.message)

    def to_dict(self) -> Dict[str, Any]:
        return self.denial.to_dict()


def propose_hypothesis_service(
    session: Session,
    run_id: str,
    model_family: str,
    formula: str,
    parameters: Dict[str, Any],
    complexity: int = 1,
    creation_source: str = "researcher",
    hypothesis_id: Optional[str] = None,
) -> HypothesisModel:
    """Create a new candidate hypothesis and its initial revision under a run."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise HypothesisNotFoundError(f"Run '{run_id}' not found.")

    hyp_id = hypothesis_id or f"hyp-{uuid.uuid4().hex[:12]}"
    rev_id = f"rev-{uuid.uuid4().hex[:12]}"

    # New candidate hypotheses are admitted against the run's persisted
    # generation budget before anything is written.
    budget_reservation = None
    try:
        budget_reservation, _created = reserve_hypotheses(
            session, run_id, 1, f"hypothesis:{hyp_id}"
        )
    except BudgetAdmissionDenied as exc:
        raise HypothesisBudgetExhaustedError(exc) from None

    hypothesis = HypothesisModel(
        hypothesis_id=hyp_id,
        run_id=run_id,
        model_family=model_family,
        current_revision=1,
        is_pinned=False,
        is_excluded=False,
    )
    initial_rev = HypothesisRevisionModel(
        revision_id=rev_id,
        hypothesis_id=hyp_id,
        revision_number=1,
        formula=formula,
        parameters=parameters,
        complexity=complexity,
        creation_source=creation_source,
    )
    # The reservation lives in this same transaction: if the insert fails, the
    # enclosing rollback also returns the hypothesis-generation capacity.
    repo.create_hypothesis_with_initial_revision(hypothesis, initial_rev)

    audit = AuditEventModel(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        event_type="HYPOTHESIS_PROPOSED",
        actor="researcher",
        metadata_json={
            "hypothesis_id": hyp_id,
            "revision_id": rev_id,
            "model_family": model_family,
            "formula": formula,
            "complexity": complexity,
        },
    )
    repo.record_audit_event(audit)
    if budget_reservation is not None:
        complete_hypothesis_generation(session, budget_reservation, True)
    session.flush()
    return hypothesis


def update_hypothesis_flags_service(
    session: Session,
    hypothesis_id: str,
    is_pinned: Optional[bool] = None,
    is_excluded: Optional[bool] = None,
) -> HypothesisModel:
    """Update pinning and exclusion flags on a hypothesis with appropriate audit trail."""
    repo = ClarityRepository(session)
    hyp = repo.get_hypothesis(hypothesis_id)
    if not hyp:
        raise HypothesisNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")

    if is_pinned is not None and hyp.is_pinned != is_pinned:
        event_type = "HYPOTHESIS_PINNED" if is_pinned else "HYPOTHESIS_UNPINNED"
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=hyp.run_id,
                event_type=event_type,
                actor="researcher",
                metadata_json={"hypothesis_id": hypothesis_id, "is_pinned": is_pinned},
            )
        )

    if is_excluded is not None and hyp.is_excluded != is_excluded:
        event_type = "HYPOTHESIS_EXCLUDED" if is_excluded else "HYPOTHESIS_RESTORED"
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=hyp.run_id,
                event_type=event_type,
                actor="researcher",
                metadata_json={"hypothesis_id": hypothesis_id, "is_excluded": is_excluded},
            )
        )

    repo.update_hypothesis_flags(
        hypothesis_id,
        is_pinned=is_pinned,
        is_excluded=is_excluded,
    )
    session.flush()
    return hyp


def _execute_pipeline_for_revision(
    session: Session,
    repo: ClarityRepository,
    hyp: HypothesisModel,
    latest_rev: HypothesisRevisionModel,
    eval_model: EvaluationResultModel,
    audit_event_type: str = "HYPOTHESIS_REEVALUATED",
) -> EvaluationResultModel:
    """Execute domain EvaluationPipeline on a hypothesis revision and update eval_model."""
    obs_models = repo.list_observations_for_run(hyp.run_id)
    training_ids = [o.observation_id for o in obs_models if not o.is_held_out]
    held_out_ids = [o.observation_id for o in obs_models if o.is_held_out]

    obs_domain: Dict[str, Observation] = {}
    for o in obs_models:
        status_enum = (
            ObservationStatus.SUCCESS
            if o.status == "success"
            else ObservationStatus.INVALID_INPUT
        )
        obs_domain[o.observation_id] = Observation(
            observation_id=o.observation_id,
            input_id=o.input_id,
            input_values=o.input_values or {},
            output_values=o.output_values,
            status=status_enum,
            sequence_number=o.sequence_number,
            is_held_out=o.is_held_out,
        )

    params = dict(latest_rev.parameters or {})
    model_family = hyp.model_family
    if model_family in ("linear", "1d_linear") or ("a" in params and "b" in params and "coefficients" not in params):
        model_family = POLYNOMIAL_FAMILY_ID
        params = {
            "family": POLYNOMIAL_FAMILY_ID,
            "degree": 1,
            "coefficient_order": "ascending",
            "coefficients": {
                "c0": float(params.get("b", 0.0)),
                "c1": float(params.get("a", 1.0)),
            },
            "input_variable": "x",
            "output_variable": "y",
        }
    elif "family" not in params and model_family == POLYNOMIAL_FAMILY_ID:
        params["family"] = POLYNOMIAL_FAMILY_ID

    candidate_repr = HypothesisRepresentation(
        model_family=model_family,
        formula=latest_rev.formula,
        parameters=params,
        complexity=latest_rev.complexity,
        hypothesis_id=hyp.hypothesis_id,
        revision=latest_rev.revision_number,
    )

    request = EvaluationRequest(
        hypothesis=candidate_repr,
        observation_ids=training_ids,
        held_out_observation_ids=held_out_ids,
        metric_names=["mae", "rmse"],
    )

    pipeline = EvaluationPipeline()
    result: EvaluationResult = pipeline.evaluate(request, obs_domain, dataset_id=hyp.run_id)

    eval_status = "completed" if result.status == EvaluationStatus.COMPLETED else "failed"
    error_msg = result.error.message if result.error else None

    eval_model.status = eval_status
    eval_model.sample_count = result.sample_count
    eval_model.dataset_version = len(training_ids)
    eval_model.metrics = result.metrics or {}
    eval_model.held_out_metrics = result.held_out_metrics
    eval_model.error_message = error_msg
    eval_model.evaluated_at = datetime.now(timezone.utc)

    repo.record_audit_event(
        AuditEventModel(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            run_id=hyp.run_id,
            event_type=audit_event_type,
            actor="worker" if audit_event_type == "HYPOTHESIS_REEVALUATION_CONSUMED" else "researcher",
            metadata_json={
                "hypothesis_id": hyp.hypothesis_id,
                "revision_id": latest_rev.revision_id,
                "evaluation_id": eval_model.evaluation_id,
                "status": eval_status,
                "sample_count": result.sample_count,
            },
        )
    )
    session.flush()
    return eval_model


def reevaluate_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    dispatcher: Optional[Callable[..., Any]] = None,
) -> Tuple[EvaluationResultModel, bool]:
    """Execute or dispatch re-evaluation for a hypothesis's latest revision.

    If `dispatcher` is supplied (e.g. background worker queue), dispatches work
    and returns (pending_eval, True).

    If `dispatcher` is None, routes re-evaluation directly through CLARITY's
    domain EvaluationPipeline against run observations, persisting the completed/failed
    result and returning (evaluated_eval, False).
    """
    repo = ClarityRepository(session)
    hyp = repo.get_hypothesis(hypothesis_id)
    if not hyp:
        raise HypothesisNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")

    revisions = sorted(hyp.revisions, key=lambda r: r.revision_number)
    if not revisions:
        raise HypothesisValidationError(
            f"Hypothesis '{hypothesis_id}' has no revisions and cannot be re-evaluated."
        )

    latest_rev = revisions[-1]
    eval_id = f"eva-{uuid.uuid4().hex[:12]}"

    if dispatcher is not None:
        # Enqueue to background worker consumer
        pending_eval = EvaluationResultModel(
            evaluation_id=eval_id,
            revision_id=latest_rev.revision_id,
            run_id=hyp.run_id,
            status="pending",
            sample_count=0,
            dataset_version=0,
            metrics={},
            held_out_metrics=None,
            error_message=None,
        )
        repo.record_evaluation(pending_eval)
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=hyp.run_id,
                event_type="HYPOTHESIS_REEVALUATE_REQUESTED",
                actor="researcher",
                metadata_json={
                    "hypothesis_id": hypothesis_id,
                    "revision_id": latest_rev.revision_id,
                    "evaluation_id": eval_id,
                    "dispatched": True,
                },
            )
        )
        session.flush()

        def _dispatch_after_commit(*args, **kwargs):
            try:
                dispatcher(hyp.run_id, hypothesis_id, eval_id)
            except TypeError:
                dispatcher(hyp.run_id, hypothesis_id)

        try:
            from sqlalchemy import event
            event.listen(session, "after_commit", _dispatch_after_commit, once=True)
        except Exception:
            _dispatch_after_commit()

        return pending_eval, True

    # Synchronous evaluation through domain EvaluationPipeline
    eval_model = EvaluationResultModel(
        evaluation_id=eval_id,
        revision_id=latest_rev.revision_id,
        run_id=hyp.run_id,
        status="pending",
        sample_count=0,
        dataset_version=0,
        metrics={},
        held_out_metrics=None,
        error_message=None,
    )
    repo.record_evaluation(eval_model)
    session.flush()

    completed_eval = _execute_pipeline_for_revision(
        session=session,
        repo=repo,
        hyp=hyp,
        latest_rev=latest_rev,
        eval_model=eval_model,
        audit_event_type="HYPOTHESIS_REEVALUATED",
    )
    return completed_eval, False


def consume_pending_evaluation(
    session: Session,
    evaluation_id: Optional[str] = None,
) -> Optional[EvaluationResultModel]:
    """Worker task consumer that claims and processes a pending evaluation.

    Pulls a pending EvaluationResultModel, fits and evaluates the hypothesis
    revision against run observations using CLARITY's domain EvaluationPipeline,
    updates the record to 'completed' or 'failed', updates metrics, and records
    a HYPOTHESIS_REEVALUATION_CONSUMED audit event.

    Returns the completed/failed EvaluationResultModel, or None if no pending work exists.
    """
    repo = ClarityRepository(session)
    stmt = (
        select(EvaluationResultModel)
        .where(EvaluationResultModel.status == "pending")
        .order_by(EvaluationResultModel.evaluation_id.asc())
    )
    if evaluation_id:
        stmt = stmt.where(EvaluationResultModel.evaluation_id == evaluation_id)

    eval_model = session.scalars(stmt).first()
    if not eval_model:
        return None

    rev = session.get(HypothesisRevisionModel, eval_model.revision_id)
    if not rev:
        eval_model.status = "failed"
        eval_model.error_message = f"Revision '{eval_model.revision_id}' not found."
        session.flush()
        return eval_model

    hyp = session.get(HypothesisModel, rev.hypothesis_id)
    if not hyp:
        eval_model.status = "failed"
        eval_model.error_message = f"Hypothesis '{rev.hypothesis_id}' not found."
        session.flush()
        return eval_model

    return _execute_pipeline_for_revision(
        session=session,
        repo=repo,
        hyp=hyp,
        latest_rev=rev,
        eval_model=eval_model,
        audit_event_type="HYPOTHESIS_REEVALUATION_CONSUMED",
    )

