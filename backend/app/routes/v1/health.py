"""API v1 health endpoint.

GET /api/v1/health — Returns service status and dependency availability.
Does not expose secrets, credentials, or internal infrastructure details.
"""

from datetime import datetime, timezone

import sqlalchemy
from flask import Blueprint, current_app, jsonify

from ...adapters.persistence.database import get_db_manager

health_v1_bp = Blueprint("health_v1", __name__)


@health_v1_bp.route("/health", methods=["GET"])
def health_check():
    """Service health endpoint.

    Returns:
        200 — Service is healthy with dependency status summary.
        503 — Service is degraded (database unreachable).
    """
    db_status = "unknown"
    try:
        db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            session.execute(sqlalchemy.text("SELECT 1"))
        db_status = "ok"
    except Exception:  # noqa: BLE001 — never expose internal detail
        db_status = "unavailable"

    status = "healthy" if db_status == "ok" else "degraded"
    payload = {
        "status": status,
        "service": "clarity-backend",
        "version": "v1",
        "environment": current_app.config.get("ENV_NAME", "development"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencies": {
            "database": db_status,
        },
    }
    return jsonify(payload), 200 if status == "healthy" else 503
