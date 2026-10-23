"""API v1 hypothesis endpoints (resource-level).

Endpoints:
  GET    /api/v1/hypotheses/<hypothesis_id>             Retrieve a hypothesis
  PATCH  /api/v1/hypotheses/<hypothesis_id>             Pin / exclude / restore
  POST   /api/v1/hypotheses/<hypothesis_id>/reevaluate  Request re-evaluation
  GET    /api/v1/hypotheses/<hypothesis_id>/evaluations Evaluation history

Run-scoped hypothesis listing and creation live in runs.py.
"""

import uuid

from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
from ...services.hypothesis_service import (
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    reevaluate_hypothesis_service,
    update_hypothesis_flags_service,
)
from .shared import (
    error_response,
    log_safe_error,
    serialize_evaluation,
    serialize_hypothesis,
)

hypotheses_v1_bp = Blueprint("hypotheses_v1", __name__)


def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


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

    db = _get_db()
    try:
        with db.session_scope() as session:
            hyp = update_hypothesis_flags_service(
                session=session,
                hypothesis_id=hypothesis_id,
                is_pinned=is_pinned if has_pin else None,
                is_excluded=is_excluded if has_exclude else None,
                expected_revision=expected_revision,
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
