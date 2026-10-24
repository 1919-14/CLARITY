"""API v1 hypothesis endpoints (resource-level).

Endpoints:
  GET    /api/v1/hypotheses/<hypothesis_id>             Retrieve a hypothesis
  PATCH  /api/v1/hypotheses/<hypothesis_id>             Pin / exclude / restore
  POST   /api/v1/hypotheses/<hypothesis_id>/revisions    Append a hypothesis revision (edit)
  GET    /api/v1/hypotheses/<hypothesis_id>/revisions    Revision history
  GET    /api/v1/hypotheses/<hypothesis_id>/revisions/<revision_id>/evaluations
                                                        Evaluation history for one revision
  POST   /api/v1/hypotheses/<hypothesis_id>/reevaluate  Request re-evaluation
  GET    /api/v1/hypotheses/<hypothesis_id>/evaluations Evaluation history (latest revision)

Run-scoped hypothesis listing and creation live in runs.py.
"""

import uuid
from typing import List, Optional

from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
from ...domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    Observation,
    ObservationStatus,
    StructuredError,
)
from ...domain.evidence_tracker import (
    collect_evidence,
    derive_plausibility_status,
    summarize_evidence,
)
from ...domain.ranking_contracts import RankingPolicyConfig
from ...domain.history_contracts import (
    ACTOR_RESEARCHER,
    InvalidHistoryCursorError,
)
from ...services.history_service import (
    HistoryNotFoundError,
    list_hypothesis_revisions,
    list_revision_evaluations,
)
from ...services.hypothesis_service import (
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    reevaluate_hypothesis_service,
    revise_hypothesis_service,
    update_hypothesis_flags_service,
)
from .shared import (
    error_response,
    log_safe_error,
    serialize_evaluation,
    serialize_hypothesis,
    serialize_revision,
)

hypotheses_v1_bp = Blueprint("hypotheses_v1", __name__)

_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


def _parse_pagination():
    """Return (limit, offset) or raise ValueError with a user-facing message."""
    try:
        limit = int(request.args.get("limit", _DEFAULT_PAGE_SIZE))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        raise ValueError("Query parameters 'limit' and 'offset' must be integers.")
    if limit <= 0 or offset < 0:
        raise ValueError("'limit' must be > 0 and 'offset' must be >= 0.")
    return min(limit, _MAX_PAGE_SIZE), offset


def _correlation_id() -> Optional[str]:
    """Request tracing identifier supplied by the caller, when present."""
    for header in ("X-Request-ID", "X-Correlation-ID"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()[:128]
    return None


def _actor_id() -> Optional[str]:
    """Authenticated actor identity.

    CLARITY has no authentication layer yet, so this is only populated from a
    trusted gateway header; it is not derived from user input in the body and is
    stored as an audit attribute, never as an authorization decision.
    """
    value = request.headers.get("X-Actor-ID")
    if value and value.strip():
        return value.strip()[:128]
    return None


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>", methods=["GET"])
def get_hypothesis(hypothesis_id: str):
    """GET /api/v1/hypotheses/<hypothesis_id> — Retrieve a hypothesis and its current state.

    Includes all revisions and the latest evaluation result.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            hyp = repo.get_hypothesis(hypothesis_id)
            if not hyp:
                return error_response(f"Hypothesis '{hypothesis_id}' not found.", 404)
            serialized = serialize_hypothesis(hyp, repo, include_revisions=True)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_hypothesis db error", exc)
        return error_response("An internal error occurred while retrieving the hypothesis.", 500)

    return jsonify({"hypothesis": serialized}), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>", methods=["PATCH"])
def update_hypothesis(hypothesis_id: str):
    """PATCH /api/v1/hypotheses/<hypothesis_id> — Supported lifecycle changes.

    Supported fields: is_pinned (bool), is_excluded (bool).
    Pinning is a display preference and does not affect scientific scores.
    Excluding removes a candidate from active search but preserves history.
    """
    if not request.is_json:
        return error_response("Payload must be JSON with Content-Type application/json.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response("Request body must be a JSON object.", 400)

    has_pin = "is_pinned" in data
    has_exclude = "is_excluded" in data

    if not has_pin and not has_exclude:
        return error_response(
            "Payload must specify at least one of 'is_pinned' or 'is_excluded'.", 400
        )

    is_pinned = data.get("is_pinned")
    if has_pin and not isinstance(is_pinned, bool):
        return error_response("Field 'is_pinned' must be a boolean.", 400)

    is_excluded = data.get("is_excluded")
    if has_exclude and not isinstance(is_excluded, bool):
        return error_response("Field 'is_excluded' must be a boolean.", 400)

    expected_revision = data.get("expected_revision")
    if expected_revision is not None:
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int) or expected_revision < 1:
            return error_response("Field 'expected_revision' must be an integer >= 1.", 400)

    expected_version = data.get("expected_version")
    if expected_version is not None:
        if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
            return error_response("Field 'expected_version' must be an integer >= 1.", 400)

    expected_version_token = data.get("expected_version_token")
    if expected_version_token is not None:
        if not isinstance(expected_version_token, str) or not expected_version_token.strip():
            return error_response("Field 'expected_version_token' must be a non-empty string.", 400)
        expected_version_token = expected_version_token.strip()

    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        return error_response("Field 'reason' must be a non-empty string when provided.", 400)

    db = _get_db()
    try:
        with db.session_scope() as session:
            hyp = update_hypothesis_flags_service(
                session=session,
                hypothesis_id=hypothesis_id,
                is_pinned=is_pinned if has_pin else None,
                is_excluded=is_excluded if has_exclude else None,
                expected_revision=expected_revision,
                expected_version=expected_version,
                expected_version_token=expected_version_token,
                reason=reason.strip() if isinstance(reason, str) else None,
                actor=ACTOR_RESEARCHER,
                actor_id=_actor_id(),
                correlation_id=_correlation_id(),
            )
            repo = ClarityRepository(session)
            serialized = serialize_hypothesis(hyp, repo, include_revisions=True)

    except HypothesisNotFoundError as exc:
        return error_response(str(exc), 404)
    except HypothesisConflictError as exc:
        return error_response(str(exc), 409)
    except HypothesisValidationError as exc:
        return error_response(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("update_hypothesis db error", exc)
        return error_response("An internal error occurred while updating the hypothesis.", 500)

    return jsonify({"hypothesis": serialized}), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/revisions", methods=["POST"])
def create_hypothesis_revision(hypothesis_id: str):
    """POST /api/v1/hypotheses/<hypothesis_id>/revisions — Append an edit as a new revision.

    The previous revision is preserved untouched. The request may supply any of
    ``formula``, ``parameters``, ``complexity`` or ``model_family``; omitted
    fields inherit the current revision's values.

    Concurrency: send ``expected_revision`` or ``expected_version_token`` to
    reject the edit when someone else changed the hypothesis first (409).

    Idempotency: send an ``Idempotency-Key`` header (or ``idempotency_key`` in
    the body). Replaying the same key with the same payload returns the revision
    that already committed (200); reusing the key with a different payload is
    rejected (409).
    """
    if not request.is_json:
        return error_response("Payload must be JSON with Content-Type application/json.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response("Request body must be a JSON object.", 400)

    formula = data.get("formula")
    if formula is not None and (not isinstance(formula, str) or not formula.strip()):
        return error_response("Field 'formula' must be a non-empty string when provided.", 400)

    parameters = data.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        return error_response("Field 'parameters' must be a JSON object when provided.", 400)

    complexity = data.get("complexity")
    if complexity is not None:
        if isinstance(complexity, bool) or not isinstance(complexity, int) or complexity < 1:
            return error_response("Field 'complexity' must be an integer >= 1 when provided.", 400)

    model_family = data.get("model_family")
    if model_family is not None and (
        not isinstance(model_family, str) or not model_family.strip()
    ):
        return error_response("Field 'model_family' must be a non-empty string when provided.", 400)

    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or not reason.strip()):
        return error_response("Field 'reason' must be a non-empty string when provided.", 400)

    expected_revision = data.get("expected_revision")
    if expected_revision is not None:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            return error_response("Field 'expected_revision' must be an integer >= 1.", 400)

    expected_version_token = data.get("expected_version_token")
    if expected_version_token is not None and (
        not isinstance(expected_version_token, str) or not expected_version_token.strip()
    ):
        return error_response("Field 'expected_version_token' must be a non-empty string.", 400)

    idempotency_key = request.headers.get("Idempotency-Key")
    if idempotency_key is None:
        idempotency_key = data.get("idempotency_key")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return error_response("Idempotency key must be a non-empty string.", 400)
        idempotency_key = idempotency_key.strip()[:128]

    if not any(
        value is not None
        for value in (formula, parameters, complexity, model_family)
    ):
        return error_response(
            "Payload must specify at least one of 'formula', 'parameters', "
            "'complexity' or 'model_family'.",
            400,
        )

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            existing = repo.get_hypothesis(hypothesis_id)
            if not existing:
                return error_response(f"Hypothesis '{hypothesis_id}' not found.", 404)
            replay = bool(idempotency_key) and repo.get_revision_by_request_key(
                hypothesis_id, idempotency_key
            ) is not None

            revision = revise_hypothesis_service(
                session=session,
                hypothesis_id=hypothesis_id,
                formula=formula,
                parameters=parameters,
                complexity=complexity,
                model_family=model_family,
                reason=reason.strip() if isinstance(reason, str) else None,
                expected_revision=expected_revision,
                expected_version_token=expected_version_token.strip()
                if isinstance(expected_version_token, str)
                else None,
                request_key=idempotency_key,
                actor=ACTOR_RESEARCHER,
                actor_id=_actor_id(),
                correlation_id=_correlation_id(),
            )
            serialized = serialize_revision(revision)
            hyp = repo.get_hypothesis(hypothesis_id)
            current_revision = hyp.current_revision if hyp else revision.revision_number
    except HypothesisNotFoundError as exc:
        return error_response(str(exc), 404)
    except HypothesisConflictError as exc:
        return error_response(str(exc), 409)
    except HypothesisValidationError as exc:
        return error_response(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("create_hypothesis_revision db error", exc)
        return error_response("An internal error occurred while creating the revision.", 500)

    return jsonify({
        "revision": serialized,
        "hypothesis_id": hypothesis_id,
        "current_revision": current_revision,
        "idempotent_replay": replay,
    }), 200 if replay else 201


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/revisions", methods=["GET"])
def list_revisions(hypothesis_id: str):
    """GET /api/v1/hypotheses/<hypothesis_id>/revisions — Revision history.

    Returns every revision in ascending revision order, each with its parent
    link, change reason and actor. Prior definitions are returned exactly as
    they were recorded, never re-derived from the current formula.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            hypothesis, revisions = list_hypothesis_revisions(session, hypothesis_id)
            serialized = [serialize_revision(r) for r in revisions]
            payload = {
                "hypothesis_id": hypothesis_id,
                "run_id": hypothesis.run_id,
                "current_revision": hypothesis.current_revision,
                "is_pinned": hypothesis.is_pinned,
                "is_excluded": hypothesis.is_excluded,
                "revisions": serialized,
                "count": len(serialized),
            }
    except HistoryNotFoundError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_revisions db error", exc)
        return error_response("An internal error occurred while retrieving revisions.", 500)

    return jsonify(payload), 200


@hypotheses_v1_bp.route(
    "/hypotheses/<hypothesis_id>/revisions/<revision_id>/evaluations",
    methods=["GET"],
)
def list_revision_evaluation_history(hypothesis_id: str, revision_id: str):
    """GET .../revisions/<revision_id>/evaluations — Evaluation history for one revision.

    Returns evaluations oldest-first, each referencing the exact dataset snapshot
    used. Query params: ``limit`` (1–200, default 50) and ``offset``.
    """
    try:
        limit, offset = _parse_pagination()
    except ValueError as exc:
        return error_response(str(exc), 400)

    db = _get_db()
    try:
        with db.session_scope() as session:
            revision, evaluations, total, applied_limit, applied_offset = (
                list_revision_evaluations(
                    session,
                    revision_id,
                    hypothesis_id=hypothesis_id,
                    limit=limit,
                    offset=offset,
                )
            )
            serialized = [serialize_evaluation(e) for e in evaluations]
            payload = {
                "hypothesis_id": hypothesis_id,
                "revision_id": revision_id,
                "revision_number": revision.revision_number,
                "evaluations": serialized,
                "count": len(serialized),
                "total": total,
                "limit": applied_limit,
                "offset": applied_offset,
            }
    except HistoryNotFoundError as exc:
        return error_response(str(exc), 404)
    except InvalidHistoryCursorError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_revision_evaluation_history db error", exc)
        return error_response(
            "An internal error occurred while retrieving evaluation history.", 500
        )

    return jsonify(payload), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/reevaluate", methods=["POST"])
def reevaluate_hypothesis(hypothesis_id: str):
    """POST /api/v1/hypotheses/<hypothesis_id>/reevaluate — Request re-evaluation.

    Routes re-evaluation through CLARITY's domain EvaluationPipeline against
    run observations, or dispatches to a configured background consumer if available.
    If EVALUATION_DEFERRED is configured, returns 501 Not Implemented with a truthful status.
    """
    if current_app.config.get("EVALUATION_DEFERRED"):
        return error_response(
            "Re-evaluation is deferred in this deployment: asynchronous evaluation worker is not enabled.",
            501,
        )

    db = _get_db()
    dispatcher = current_app.config.get("EVALUATION_DISPATCHER")
    try:
        with db.session_scope() as session:
            eval_model, is_enqueued = reevaluate_hypothesis_service(
                session=session,
                hypothesis_id=hypothesis_id,
                dispatcher=dispatcher,
                correlation_id=_correlation_id(),
                actor_id=_actor_id(),
            )
            serialized_eval = serialize_evaluation(eval_model)
    except HypothesisNotFoundError as exc:
        return error_response(str(exc), 404)
    except HypothesisValidationError as exc:
        return error_response(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("reevaluate_hypothesis db error", exc)
        return error_response("An internal error occurred while requesting re-evaluation.", 500)

    if is_enqueued:
        return jsonify({
            "evaluation": serialized_eval,
            "message": "Re-evaluation enqueued. Poll the evaluation history for results.",
        }), 202

    return jsonify({
        "evaluation": serialized_eval,
        "message": "Re-evaluation completed via evaluation pipeline.",
    }), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/evaluations", methods=["GET"])
def list_hypothesis_evaluations(hypothesis_id: str):
    """GET /api/v1/hypotheses/<hypothesis_id>/evaluations — Evaluation history.

    Returns all evaluation results for the latest revision of the hypothesis,
    sorted most-recent first. Ground-truth oracle details are never exposed.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            hyp = repo.get_hypothesis(hypothesis_id)
            if not hyp:
                return error_response(f"Hypothesis '{hypothesis_id}' not found.", 404)

            revisions = sorted(hyp.revisions, key=lambda r: r.revision_number)
            if not revisions:
                return jsonify({
                    "hypothesis_id": hypothesis_id,
                    "evaluations": [],
                    "count": 0,
                    "note": "No revisions exist for this hypothesis.",
                }), 200

            latest_rev = revisions[-1]
            evals = repo.list_evaluations_for_revision(latest_rev.revision_id)
            serialized = [serialize_evaluation(e) for e in evals]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_hypothesis_evaluations db error", exc)
        return error_response("An internal error occurred while retrieving evaluation history.", 500)

    return jsonify({
        "hypothesis_id": hypothesis_id,
        "revision_id": latest_rev.revision_id,
        "evaluations": serialized,
        "count": len(serialized),
    }), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/evidence", methods=["GET"])
def get_hypothesis_evidence(hypothesis_id: str):
    """GET /api/v1/hypotheses/<hypothesis_id>/evidence — Supporting and contradicting evidence.

    Returns classified observation evidence (supporting, contradicting, indeterminate)
    along with observed values, model predicted values, residuals, and applied tolerance.
    Evaluator-only ground truth and hidden oracle rules are NEVER exposed.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            hyp = repo.get_hypothesis(hypothesis_id)
            if not hyp:
                return error_response(f"Hypothesis '{hypothesis_id}' not found.", 404)

            revisions = sorted(hyp.revisions, key=lambda r: r.revision_number) if hyp.revisions else []
            if not revisions:
                return jsonify({
                    "hypothesis_id": hypothesis_id,
                    "records": [],
                    "summary": {
                        "supporting_count": 0,
                        "contradicting_count": 0,
                        "indeterminate_count": 0,
                        "total_evaluated": 0,
                        "unverified_membership_count": 0,
                        "supporting_observation_ids": [],
                        "contradicting_observation_ids": [],
                        "indeterminate_observation_ids": [],
                        "plausibility_status": "unevaluated",
                    },
                    "note": "No revisions exist for this hypothesis.",
                }), 200

            latest_rev = revisions[-1]
            evals = repo.list_evaluations_for_revision(latest_rev.revision_id)
            latest_ev = evals[0] if evals else None

            # Fetch run observations
            raw_obs = repo.list_observations_for_run(hyp.run_id)
            if not raw_obs:
                return jsonify({
                    "hypothesis_id": hypothesis_id,
                    "revision_id": latest_rev.revision_id,
                    "evaluation_id": latest_ev.evaluation_id if latest_ev else None,
                    "records": [],
                    "summary": {
                        "supporting_count": 0,
                        "contradicting_count": 0,
                        "indeterminate_count": 0,
                        "total_evaluated": 0,
                        "unverified_membership_count": 0,
                        "supporting_observation_ids": [],
                        "contradicting_observation_ids": [],
                        "indeterminate_observation_ids": [],
                        "plausibility_status": "unevaluated",
                    },
                    "note": "No observations recorded for this run.",
                }), 200

            training_ids: List[str] = []
            held_out_ids: List[str] = []
            domain_obs: List[Observation] = []
            for o in raw_obs:
                status_enum = ObservationStatus.SUCCESS if o.status == "success" else ObservationStatus.ORACLE_FAILURE
                if o.is_held_out:
                    held_out_ids.append(o.observation_id)
                else:
                    training_ids.append(o.observation_id)
                domain_obs.append(
                    Observation(
                        observation_id=o.observation_id,
                        input_id=o.input_id or o.observation_id,
                        input_values=o.input_values or {},
                        output_values=o.output_values,
                        status=status_enum,
                        sequence_number=o.sequence_number,
                        is_held_out=bool(o.is_held_out),
                    )
                )

            # Build domain evaluation result
            eval_status = EvaluationStatus.COMPLETED if (latest_ev and latest_ev.status == "completed") else EvaluationStatus.PENDING
            eval_err = None
            if latest_ev and latest_ev.error_message:
                eval_err = StructuredError(code="EVALUATION_FAILED", message=latest_ev.error_message)

            eval_res = EvaluationResult(
                evaluation_id=latest_ev.evaluation_id if latest_ev else "eval-pending",
                hypothesis_id=hyp.hypothesis_id,
                status=eval_status,
                metrics=latest_ev.metrics or {} if latest_ev else {},
                held_out_metrics=latest_ev.held_out_metrics if latest_ev else None,
                sample_count=latest_ev.sample_count or len(training_ids) if latest_ev else len(training_ids),
                held_out_sample_count=len(held_out_ids) if (latest_ev and latest_ev.held_out_metrics) else None,
                error=eval_err,
                model_family=hyp.model_family,
                fitted_specification=latest_rev.parameters or {},
                metric_version="1.0.0",
                metric_definitions={
                    "mae": "Mean Absolute Error",
                    "rmse": "Root Mean Squared Error",
                },
                dataset_ids={
                    "dataset_id": hyp.run_id,
                    "dataset_version": latest_ev.dataset_version if latest_ev else 1,
                    "observation_ids": training_ids,
                    "training_observation_ids": training_ids,
                    "held_out_observation_ids": held_out_ids,
                },
            )

            policy = RankingPolicyConfig()
            records = collect_evidence(eval_res, domain_obs, policy)
            summary = summarize_evidence(hyp.hypothesis_id, records)
            plausibility = derive_plausibility_status(eval_res, summary, policy)

            serialized_records = [
                {
                    "evidence_id": r.evidence_id,
                    "observation_id": r.observation_id,
                    "experiment_id": r.experiment_id,
                    "partition": r.partition,
                    "classification": r.classification.value,
                    "observed_value": r.observed_value,
                    "predicted_value": r.predicted_value,
                    "residual": r.residual,
                    "tolerance": r.tolerance,
                    "is_held_out": r.is_held_out,
                    "dataset_id": r.dataset_id,
                    "evaluation_id": r.evaluation_id,
                    "dataset_version": r.dataset_version,
                    "timestamp": r.timestamp,
                    "membership_verified": r.membership_verified,
                }
                for r in records
            ]

            serialized_summary = {
                "supporting_count": summary.supporting_count,
                "contradicting_count": summary.contradicting_count,
                "indeterminate_count": summary.indeterminate_count,
                "total_evaluated": summary.total_evaluated,
                "unverified_membership_count": summary.unverified_membership_count,
                "supporting_observation_ids": summary.supporting_observation_ids,
                "contradicting_observation_ids": summary.contradicting_observation_ids,
                "indeterminate_observation_ids": summary.indeterminate_observation_ids,
                "plausibility_status": plausibility.value,
            }

    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_hypothesis_evidence db error", exc)
        return error_response("An internal error occurred while retrieving hypothesis evidence.", 500)

    return jsonify({
        "hypothesis_id": hypothesis_id,
        "revision_id": latest_rev.revision_id,
        "evaluation_id": latest_ev.evaluation_id if latest_ev else None,
        "records": serialized_records,
        "summary": serialized_summary,
        "count": len(serialized_records),
    }), 200
