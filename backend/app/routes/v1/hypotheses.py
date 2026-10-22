"""API v1 hypothesis endpoints (resource-level).

Endpoints:
  GET    /api/v1/hypotheses/<hypothesis_id>             Retrieve a hypothesis
  PATCH  /api/v1/hypotheses/<hypothesis_id>             Pin / exclude / restore
  POST   /api/v1/hypotheses/<hypothesis_id>/reevaluate  Request re-evaluation
  GET    /api/v1/hypotheses/<hypothesis_id>/evaluations Evaluation history

Run-scoped hypothesis listing and creation live in runs.py.
"""

import uuid

from flask import Blueprint, current_app, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.models import AuditEventModel, EvaluationResultModel
from ...adapters.persistence.repository import ClarityRepository
from .shared import (
    error_response,
    serialize_evaluation,
    serialize_hypothesis,
)
from flask import jsonify

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
        current_app.logger.error("get_hypothesis db error: %s", exc)
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

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            hyp = repo.get_hypothesis(hypothesis_id)
            if not hyp:
                return error_response(f"Hypothesis '{hypothesis_id}' not found.", 404)

            if has_pin and hyp.is_pinned != is_pinned:
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

            if has_exclude and hyp.is_excluded != is_excluded:
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
                is_pinned=is_pinned if has_pin else None,
                is_excluded=is_excluded if has_exclude else None,
            )
            serialized = serialize_hypothesis(hyp, repo, include_revisions=True)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("update_hypothesis db error: %s", exc)
        return error_response("An internal error occurred while updating the hypothesis.", 500)

    return jsonify({"hypothesis": serialized}), 200


@hypotheses_v1_bp.route("/hypotheses/<hypothesis_id>/reevaluate", methods=["POST"])
def reevaluate_hypothesis(hypothesis_id: str):
    """POST /api/v1/hypotheses/<hypothesis_id>/reevaluate — Request re-evaluation.

    Records a pending evaluation result and emits an audit event. The actual
    evaluation work is delegated to the discovery engine's evaluation pipeline.
    This endpoint does not perform the evaluation inline.
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
                return error_response(
                    f"Hypothesis '{hypothesis_id}' has no revisions and cannot be re-evaluated.", 422
                )

            latest_rev = revisions[-1]

            # Record a pending evaluation to enqueue re-evaluation
            eval_id = f"eva-{uuid.uuid4().hex[:12]}"
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
                    },
                )
            )
            serialized_eval = serialize_evaluation(pending_eval)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("reevaluate_hypothesis db error: %s", exc)
        return error_response("An internal error occurred while requesting re-evaluation.", 500)

    return jsonify({
        "evaluation": serialized_eval,
        "message": "Re-evaluation enqueued. Poll the evaluation history for results.",
    }), 202


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
        current_app.logger.error("list_hypothesis_evaluations db error: %s", exc)
        return error_response("An internal error occurred while retrieving evaluation history.", 500)

    return jsonify({
        "hypothesis_id": hypothesis_id,
        "revision_id": latest_rev.revision_id,
        "evaluations": serialized,
        "count": len(serialized),
    }), 200
