"""API v1 observation endpoints (resource-level).

Endpoints:
  GET  /api/v1/observations/<observation_id>  Retrieve a single observation

Run-scoped observation listing lives in runs.py.
The hidden oracle implementation and evaluator ground truth are never exposed.
"""

from flask import Blueprint, current_app, jsonify

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
from .shared import error_response, serialize_observation

observations_v1_bp = Blueprint("observations_v1", __name__)


def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


@observations_v1_bp.route("/observations/<observation_id>", methods=["GET"])
def get_observation(observation_id: str):
    """GET /api/v1/observations/<observation_id> — Retrieve a single observation.

    Returns oracle input/output and status. The oracle's hidden formula and
    private evaluator ground truth are never included in the response.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            obs = repo.get_observation(observation_id)
            if not obs:
                return error_response(f"Observation '{observation_id}' not found.", 404)
            serialized = serialize_observation(obs)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.error("get_observation db error: %s", exc)
        return error_response("An internal error occurred while retrieving the observation.", 500)

    return jsonify({"observation": serialized}), 200
