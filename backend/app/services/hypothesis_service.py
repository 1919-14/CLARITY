"""Shared service layer for hypothesis management and re-evaluation.

Encapsulates hypothesis proposal, revision tracking, flag toggling, and
re-evaluation execution via the domain EvaluationPipeline or task dispatcher.
"""

from datetime import datetime, timezone
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import BudgetAdmissionDenied, BudgetReservationReplay
from ..domain.contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
)
from ..domain.evaluation import EvaluationPipeline
from ..domain.expression_validator import validate_hypothesis_expression
from ..domain.history_contracts import (
    ACTOR_RESEARCHER,
    AUDIT_HYPOTHESIS_REVISED,
    REVISION_SOURCE_RESEARCHER,
    build_dataset_snapshot,
    compute_revision_request_fingerprint,
)
from ..domain.hypothesis_models import POLYNOMIAL_FAMILY_ID
from ..domain.sanitization import sanitize_reason_text
from .budget_service import complete_hypothesis_generation, reserve_hypotheses


class HypothesisServiceError(Exception):
    """Base exception for hypothesis service operations."""


class HypothesisNotFoundError(HypothesisServiceError):
    """Raised when a hypothesis cannot be found."""


class HypothesisValidationError(HypothesisServiceError):
    """Raised when hypothesis data fails validation."""


class HypothesisConflictError(HypothesisServiceError):
    """Raised on concurrent edit conflict or duplicate identifier."""


class HypothesisAlreadyAdmittedError(HypothesisServiceError):
    """Raised when a hypothesis id replays an already-admitted generation slot."""

    def __init__(self, replay: BudgetReservationReplay):
        self.replay = replay
        super().__init__(str(replay))

    def to_dict(self) -> Dict[str, Any]:
        return self.replay.to_dict()


class HypothesisBudgetExhaustedError(HypothesisServiceError):
    """Raised when the run's hypothesis-generation limit has been reached."""

    def __init__(self, denial: BudgetAdmissionDenied):
        self.denial = denial
        self.reason = denial.reason
        super().__init__(denial.message)

    def to_dict(self) -> Dict[str, Any]:
        return self.denial.to_dict()


def _revision_snapshot(revision: Optional[HypothesisRevisionModel]) -> Optional[Dict[str, Any]]:
    """Return the scientifically meaningful part of a revision for history payloads."""
    if revision is None:
        return None
    return {
        "revision_id": revision.revision_id,
        "revision_number": revision.revision_number,
        "model_family": getattr(revision, "model_family", None),
        "formula": revision.formula,
        "parameters": dict(revision.parameters or {}),
        "complexity": revision.complexity,
    }


def propose_hypothesis_service(
    session: Session,
    run_id: str,
    model_family: str,
    formula: str,
    parameters: Optional[Dict[str, Any]] = None,
    complexity: Optional[int] = None,
    creation_source: str = "researcher",
    hypothesis_id: Optional[str] = None,
    actor: str = ACTOR_RESEARCHER,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> HypothesisModel:
    """Validate and create a new candidate hypothesis and its initial revision under a run."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise HypothesisNotFoundError(f"Run '{run_id}' not found.")

    # 1. Validate expression safely using AST parser (strictly rejects code execution/eval/exec/imports)
    validated, error = validate_hypothesis_expression(
        formula=formula,
        model_family=model_family,
        parameters=parameters,
        complexity=complexity,
    )
    if error:
        raise HypothesisValidationError(error.message)

    # 2. Check for duplicate ID / conflict
    if hypothesis_id and repo.get_hypothesis(hypothesis_id):
        raise HypothesisConflictError(f"Hypothesis '{hypothesis_id}' already exists.")

    hyp_id = hypothesis_id or f"hyp-{uuid.uuid4().hex[:12]}"
    rev_id = f"rev-{uuid.uuid4().hex[:12]}"

    final_complexity = complexity if (complexity and complexity >= 1) else (validated.complexity if validated else 1)
    final_params = validated.parameters if validated and validated.parameters else (dict(parameters) if parameters else {})

    # New candidate hypotheses are admitted against the run's persisted
    # generation budget before anything is written.
    budget_reservation = None
    try:
        budget_reservation, _created = reserve_hypotheses(
            session, run_id, 1, f"hypothesis:{hyp_id}"
        )
    except BudgetAdmissionDenied as exc:
        raise HypothesisBudgetExhaustedError(exc) from None
    except BudgetReservationReplay as exc:
        # The slot was already admitted for this hypothesis id; the hypothesis
        # row it belongs to either exists or was rolled back. Never write a
        # second one under the replayed accounting.
        raise HypothesisAlreadyAdmittedError(exc) from None

    hypothesis = HypothesisModel(
        hypothesis_id=hyp_id,
        run_id=run_id,
        model_family=model_family.strip(),
        current_revision=1,
        is_pinned=False,
        is_excluded=False,
    )
    initial_rev = HypothesisRevisionModel(
        revision_id=rev_id,
        hypothesis_id=hyp_id,
        revision_number=1,
        formula=formula.strip(),
        parameters=final_params,
        complexity=final_complexity,
        # The validated family is the one evaluations can actually replay; the
        # hypothesis row keeps the researcher's submitted spelling for display.
        model_family=validated.model_family if validated else model_family.strip(),
        creation_source=creation_source,
        previous_revision_id=None,
        change_reason=reason,
        actor=actor,
        actor_id=actor_id,
    )
    # The reservation lives in this same transaction: if the insert fails, the
    # enclosing rollback also returns the hypothesis-generation capacity.
    repo.create_hypothesis_with_initial_revision(hypothesis, initial_rev)

    audit = AuditEventModel(
        event_id=f"evt-{uuid.uuid4().hex[:12]}",
        run_id=run_id,
        event_type="HYPOTHESIS_PROPOSED",
        actor=actor,
        actor_id=actor_id,
        correlation_id=correlation_id,
        hypothesis_id=hyp_id,
        revision_id=rev_id,
        reason=reason,
        metadata_json={
            "hypothesis_id": hyp_id,
            "revision_id": rev_id,
            "revision_number": 1,
            "model_family": model_family.strip(),
            "formula": formula.strip(),
            "complexity": final_complexity,
        },
        before_json=None,
        after_json=_revision_snapshot(initial_rev),
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
    expected_revision: Optional[int] = None,
    expected_version: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    reason: Optional[str] = None,
    actor: str = ACTOR_RESEARCHER,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisModel:
    """Update pinning and exclusion flags on a hypothesis with optimistic locking and audit trail.

    Pinning and exclusion are lifecycle/presentation state, not scientific
    evidence: no revision is created, no metric changes, and the event carries
    the before/after flag values only.
    """
    repo = ClarityRepository(session)
    hyp = repo.get_hypothesis(hypothesis_id)
    if not hyp:
        raise HypothesisNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")

    # 1. Version token check
    if expected_version_token is not None:
        current_token = getattr(hyp, "version_token", None)
        if current_token != expected_version_token:
            raise HypothesisConflictError(
                f"Hypothesis '{hypothesis_id}' version token conflict: expected token '{expected_version_token}', "
                f"but current token is '{current_token}'."
            )

    current_ver = getattr(hyp, "version", 1) or 1

    # 2. Numeric version check
    if expected_version is not None:
        if current_ver != expected_version:
            raise HypothesisConflictError(
                f"Hypothesis '{hypothesis_id}' version conflict: expected version {expected_version}, "
                f"but current version is {current_ver}."
            )

    # 3. Expected revision check (safeguards against concurrent flag updates as well as formula updates)
    if expected_revision is not None:
        if hyp.current_revision != expected_revision or current_ver != expected_revision:
            raise HypothesisConflictError(
                f"Hypothesis '{hypothesis_id}' revision conflict: expected revision/version {expected_revision}, "
                f"but current version is {current_ver} (current revision {hyp.current_revision})."
            )

    latest_revision = repo.get_latest_revision(hypothesis_id)
    latest_revision_id = latest_revision.revision_id if latest_revision else None
    # Free-text explanations are stored, so credentials pasted into them are redacted.
    safe_reason = sanitize_reason_text(reason)

    if is_pinned is not None and hyp.is_pinned != is_pinned:
        event_type = "HYPOTHESIS_PINNED" if is_pinned else "HYPOTHESIS_UNPINNED"
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=hyp.run_id,
                event_type=event_type,
                actor=actor,
                actor_id=actor_id,
                correlation_id=correlation_id,
                hypothesis_id=hypothesis_id,
                revision_id=latest_revision_id,
                reason=safe_reason,
                metadata_json={"hypothesis_id": hypothesis_id, "is_pinned": is_pinned},
                before_json={"is_pinned": bool(hyp.is_pinned)},
                after_json={"is_pinned": bool(is_pinned)},
            )
        )

    if is_excluded is not None and hyp.is_excluded != is_excluded:
        event_type = "HYPOTHESIS_EXCLUDED" if is_excluded else "HYPOTHESIS_RESTORED"
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=hyp.run_id,
                event_type=event_type,
                actor=actor,
                actor_id=actor_id,
                correlation_id=correlation_id,
                hypothesis_id=hypothesis_id,
                revision_id=latest_revision_id,
                reason=safe_reason,
                metadata_json={"hypothesis_id": hypothesis_id, "is_excluded": is_excluded},
                before_json={"is_excluded": bool(hyp.is_excluded)},
                after_json={"is_excluded": bool(is_excluded)},
            )
        )

    repo.update_hypothesis_flags(
        hypothesis_id,
        is_pinned=is_pinned,
        is_excluded=is_excluded,
    )
    session.flush()
    return hyp


def _revision_matches_request(
    revision: HypothesisRevisionModel,
    request_fingerprint: str,
    formula: str,
    parameters: Optional[Mapping[str, Any]],
    complexity: Optional[int],
) -> bool:
    """Return True when a committed revision already represents this request.

    Revisions written with a canonical ``request_fingerprint`` are compared on
    that fingerprint, which covers model family and creation source too. Legacy
    rows without one fall back to the definition fields.
    """
    stored = getattr(revision, "request_fingerprint", None)
    if stored:
        return stored == request_fingerprint
    return (
        revision.formula == formula
        and dict(revision.parameters or {}) == dict(parameters or {})
        and revision.complexity == complexity
    )


def revise_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    formula: Optional[str] = None,
    parameters: Optional[Dict[str, Any]] = None,
    complexity: Optional[int] = None,
    model_family: Optional[str] = None,
    reason: Optional[str] = None,
    expected_revision: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    request_key: Optional[str] = None,
    creation_source: str = REVISION_SOURCE_RESEARCHER,
    actor: str = ACTOR_RESEARCHER,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisRevisionModel:
    """Append a new immutable revision for an existing hypothesis.

    Guarantees:
    -----------
    * The previous revision is never modified: its row keeps the exact formula,
      parameters, complexity and model family it was created with, and the new
      revision links to it through ``previous_revision_id``.
    * The requested model family is validated and stored on the new revision in
      canonical form, so an evaluation can be replayed against the family the
      revision was actually created under rather than the hypothesis's current
      field. The hypothesis row follows the newest revision as a current-state
      projection.
    * Validation happens before anything is written, so a rejected edit creates
      neither a revision nor an audit event.
    * Concurrency is guarded by the caller's ``expected_revision`` /
      ``expected_version_token`` and by the database's unique constraints on
      ``(hypothesis_id, revision_number)`` and ``(hypothesis_id, request_key)``.
    * A repeated ``request_key`` with the same payload returns the revision that
      already committed instead of writing a duplicate; the same key with a
      different payload is rejected as a conflict.
    * The revision and its audit event are written in the caller's transaction,
      so they commit or roll back together.
    * If the database rejects the write because another writer won the race, the
      caller's transaction is rolled back before the conflict is raised.
    """
    repo = ClarityRepository(session)
    hyp = repo.get_hypothesis(hypothesis_id)
    if not hyp:
        raise HypothesisNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")

    base = repo.get_latest_revision(hypothesis_id)
    if base is None:
        raise HypothesisValidationError(
            f"Hypothesis '{hypothesis_id}' has no revisions and cannot be edited."
        )

    target_formula = (formula if formula is not None else base.formula) or ""
    target_formula = target_formula.strip()
    # An edit inherits the family of the revision it extends, because the family
    # is part of the revision definition. Falling back to the hypothesis row is
    # only needed for revisions written before the family was recorded there.
    inherited_family = getattr(base, "model_family", None) or hyp.model_family or ""
    target_family = (model_family or inherited_family or "").strip()

    # A formula edit derives its own canonical coefficients; inheriting the
    # previous revision's parameters would contradict the new expression.
    if formula is not None:
        validation_parameters = parameters
        validation_complexity = complexity
    else:
        validation_parameters = (
            dict(parameters) if parameters is not None else dict(base.parameters or {})
        )
        validation_complexity = (
            complexity if complexity is not None else base.complexity
        )

    # 1. Validation happens before any persistence, so a rejected edit leaves no
    #    partial revision and no audit event behind.
    validated, error = validate_hypothesis_expression(
        formula=target_formula,
        model_family=target_family,
        parameters=validation_parameters,
        complexity=validation_complexity,
    )
    if error:
        raise HypothesisValidationError(error.message)

    final_parameters = (
        validated.parameters
        if validated and validated.parameters
        else (
            validation_parameters
            if validation_parameters is not None
            else dict(base.parameters or {})
        )
    )
    final_complexity = (
        complexity
        if complexity is not None
        else (
            validated.complexity
            if validated and validated.complexity
            else base.complexity
        )
    )
    # The family a revision is created under is part of its scientific record,
    # so it is stored on the revision (in validated/canonical form) rather than
    # being read back from the mutable hypothesis row later.
    effective_family = validated.model_family if validated else target_family

    # Every semantically relevant field of the request is fingerprinted, not just
    # formula/parameters/complexity: replaying a key with a different model
    # family (or creation source) is a conflicting request, not a replay.
    request_fingerprint = compute_revision_request_fingerprint(
        formula=target_formula,
        parameters=final_parameters,
        complexity=final_complexity,
        model_family=target_family,
        creation_source=creation_source,
    )

    # 2. Idempotent replay of an already-committed request.
    if request_key:
        replay = repo.get_revision_by_request_key(hypothesis_id, request_key)
        if replay is not None:
            if not _revision_matches_request(
                replay,
                request_fingerprint=request_fingerprint,
                formula=target_formula,
                parameters=final_parameters,
                complexity=final_complexity,
            ):
                raise HypothesisConflictError(
                    f"Request key '{request_key}' was already used for a different revision "
                    f"of hypothesis '{hypothesis_id}'."
                )
            return replay

    # 3. Optimistic concurrency control against the expected current state.
    if expected_version_token is not None:
        current_token = getattr(hyp, "version_token", None)
        if current_token != expected_version_token:
            raise HypothesisConflictError(
                f"Hypothesis '{hypothesis_id}' version token conflict: expected token "
                f"'{expected_version_token}', but current token is '{current_token}'."
            )
    if expected_revision is not None and hyp.current_revision != expected_revision:
        raise HypothesisConflictError(
            f"Hypothesis '{hypothesis_id}' revision conflict: expected revision "
            f"{expected_revision}, but current revision is {hyp.current_revision}."
        )

    if (
        target_formula == base.formula
        and dict(final_parameters or {}) == dict(base.parameters or {})
        and final_complexity == base.complexity
    ):
        raise HypothesisValidationError(
            "The requested change does not alter the hypothesis definition; no revision was created."
        )

    # 4. Append the revision; a lost database race is reported as a conflict.
    new_revision = HypothesisRevisionModel(
        revision_id=f"rev-{uuid.uuid4().hex[:12]}",
        hypothesis_id=hypothesis_id,
        revision_number=hyp.current_revision + 1,
        formula=target_formula,
        parameters=final_parameters,
        complexity=final_complexity,
        model_family=effective_family,
        creation_source=creation_source,
        previous_revision_id=base.revision_id,
        change_reason=sanitize_reason_text(reason),
        actor=actor,
        actor_id=actor_id,
        request_key=request_key or None,
        request_fingerprint=request_fingerprint,
    )

    try:
        session.add(new_revision)
        hyp.current_revision = new_revision.revision_number
        # Keep the hypothesis row as an accurate projection of the revision it
        # now points at; the revision remains the historical source of truth.
        hyp.model_family = effective_family
        hyp.version = (getattr(hyp, "version", 1) or 1) + 1
        hyp.version_token = f"vt-{uuid.uuid4().hex[:12]}"
        hyp.updated_at = datetime.now(timezone.utc)
        session.flush()
    except IntegrityError:
        # A competing writer already used this revision number or request key.
        # The transaction is rolled back so no partial revision survives, and the
        # caller can retry against fresh state.
        session.rollback()
        if request_key:
            replay = repo.get_revision_by_request_key(hypothesis_id, request_key)
            if replay is not None and _revision_matches_request(
                replay,
                request_fingerprint=request_fingerprint,
                formula=target_formula,
                parameters=final_parameters,
                complexity=final_complexity,
            ):
                return replay
        raise HypothesisConflictError(
            f"Hypothesis '{hypothesis_id}' was modified concurrently; no revision was written."
        ) from None

    repo.record_audit_event(
        AuditEventModel(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            run_id=hyp.run_id,
            event_type=AUDIT_HYPOTHESIS_REVISED,
            actor=actor,
            actor_id=actor_id,
            correlation_id=correlation_id,
            hypothesis_id=hypothesis_id,
            revision_id=new_revision.revision_id,
            reason=sanitize_reason_text(reason),
            metadata_json={
                "hypothesis_id": hypothesis_id,
                "revision_id": new_revision.revision_id,
                "revision_number": new_revision.revision_number,
                "previous_revision_id": base.revision_id,
                "previous_revision_number": base.revision_number,
                "model_family": effective_family,
                "creation_source": creation_source,
            },
            before_json=_revision_snapshot(base),
            after_json=_revision_snapshot(new_revision),
        )
    )
    session.flush()
    return new_revision


def pin_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    expected_revision: Optional[int] = None,
    expected_version: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisModel:
    """Pin a hypothesis (display preference, does not alter scores)."""
    return update_hypothesis_flags_service(
        session=session,
        hypothesis_id=hypothesis_id,
        is_pinned=True,
        expected_revision=expected_revision,
        expected_version=expected_version,
        expected_version_token=expected_version_token,
        reason=reason,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )


def unpin_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    expected_revision: Optional[int] = None,
    expected_version: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisModel:
    """Unpin a hypothesis."""
    return update_hypothesis_flags_service(
        session=session,
        hypothesis_id=hypothesis_id,
        is_pinned=False,
        expected_revision=expected_revision,
        expected_version=expected_version,
        expected_version_token=expected_version_token,
        reason=reason,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )


def exclude_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    expected_revision: Optional[int] = None,
    expected_version: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisModel:
    """Exclude a hypothesis from active candidate selection without deleting history."""
    return update_hypothesis_flags_service(
        session=session,
        hypothesis_id=hypothesis_id,
        is_excluded=True,
        expected_revision=expected_revision,
        expected_version=expected_version,
        expected_version_token=expected_version_token,
        reason=reason,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )


def restore_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    expected_revision: Optional[int] = None,
    expected_version: Optional[int] = None,
    expected_version_token: Optional[str] = None,
    reason: Optional[str] = None,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> HypothesisModel:
    """Restore an excluded hypothesis to active candidate selection."""
    return update_hypothesis_flags_service(
        session=session,
        hypothesis_id=hypothesis_id,
        is_excluded=False,
        expected_revision=expected_revision,
        expected_version=expected_version,
        expected_version_token=expected_version_token,
        reason=reason,
        actor_id=actor_id,
        correlation_id=correlation_id,
    )



def _execute_pipeline_for_revision(
    session: Session,
    repo: ClarityRepository,
    hyp: HypothesisModel,
    latest_rev: HypothesisRevisionModel,
    eval_model: EvaluationResultModel,
    audit_event_type: str = "HYPOTHESIS_REEVALUATED",
    correlation_id: Optional[str] = None,
) -> EvaluationResultModel:
    """Execute domain EvaluationPipeline on a hypothesis revision and update eval_model.

    The evaluation record is stamped with the exact scientific inputs it used: the
    hypothesis revision that was evaluated, plus a content-addressed snapshot of
    the observations supplied to the pipeline and the manifest it was computed
    from. Because the fingerprint covers observation content and not just ids, a
    changed input/output/status/partition changes the snapshot too, and the stored
    manifest keeps the evaluated data reconstructable after the hypothesis is
    edited or new observations arrive.
    """
    obs_models = repo.list_observations_for_run(hyp.run_id)
    training_ids = [o.observation_id for o in obs_models if not o.is_held_out]
    held_out_ids = [o.observation_id for o in obs_models if o.is_held_out]
    dataset_snapshot = build_dataset_snapshot(hyp.run_id, obs_models)
    dataset_snapshot_id = dataset_snapshot.snapshot_id

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
    # The revision is the source of truth for what is being evaluated: the
    # parent hypothesis field is a current-state projection that a later
    # revision (or a stale row) may have moved on from. Only revisions written
    # before the column existed fall back to the parent value.
    model_family = getattr(latest_rev, "model_family", None) or hyp.model_family
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
    eval_model.dataset_snapshot_id = dataset_snapshot_id
    eval_model.dataset_snapshot = dataset_snapshot.manifest
    # A failed evaluation keeps whatever the pipeline actually produced (often an
    # empty mapping). Missing metrics are never fabricated into zeros.
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
            correlation_id=correlation_id,
            hypothesis_id=hyp.hypothesis_id,
            revision_id=latest_rev.revision_id,
            metadata_json={
                "hypothesis_id": hyp.hypothesis_id,
                "revision_id": latest_rev.revision_id,
                "revision_number": latest_rev.revision_number,
                "evaluation_id": eval_model.evaluation_id,
                "status": eval_status,
                "model_family": candidate_repr.model_family,
                "sample_count": result.sample_count,
                "dataset_version": len(training_ids),
                "dataset_snapshot_id": dataset_snapshot_id,
                "dataset_observation_count": dataset_snapshot.manifest[
                    "observation_count"
                ],
            },
            before_json=None,
            after_json={
                "status": eval_status,
                "sample_count": result.sample_count,
                "dataset_snapshot_id": dataset_snapshot_id,
                "metrics": dict(result.metrics or {}),
            },
        )
    )
    session.flush()
    return eval_model


def reevaluate_hypothesis_service(
    session: Session,
    hypothesis_id: str,
    dispatcher: Optional[Callable[..., Any]] = None,
    correlation_id: Optional[str] = None,
    actor_id: Optional[str] = None,
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

    if hyp.is_excluded:
        raise HypothesisValidationError(
            f"Cannot re-evaluate excluded hypothesis '{hypothesis_id}'. "
            f"Restore it first before requesting re-evaluation."
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
                actor_id=actor_id,
                correlation_id=correlation_id,
                hypothesis_id=hypothesis_id,
                revision_id=latest_rev.revision_id,
                metadata_json={
                    "hypothesis_id": hypothesis_id,
                    "revision_id": latest_rev.revision_id,
                    "revision_number": latest_rev.revision_number,
                    "evaluation_id": eval_id,
                    "dispatched": True,
                },
                before_json=None,
                after_json={"evaluation_id": eval_id, "status": "pending"},
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
        correlation_id=correlation_id,
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

