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
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
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
