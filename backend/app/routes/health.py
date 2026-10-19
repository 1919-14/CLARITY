from datetime import datetime, timezone
from flask import Blueprint, current_app, jsonify

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint returning service status and environment metadata."""
    payload = {
        "status": "healthy",
        "service": "clarity-backend",
        "environment": current_app.config.get("ENV_NAME", "development"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload), 200
