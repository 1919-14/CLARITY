import os
from flask import Flask, jsonify
from flask_cors import CORS
from .config import config_by_name, Config


def create_app(config_name=None):
    """Application factory for the CLARITY Flask backend."""
    app = Flask(__name__)

    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    config_class = config_by_name.get(config_name, Config)
    app.config.from_object(config_class)

    # Configure CORS for API routes
    cors_origins = app.config.get("CORS_ORIGINS", ["http://localhost:5173", "http://127.0.0.1:5173"])
    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        supports_credentials=True,
    )

    # Register blueprints
    from .routes.health import health_bp

    app.register_blueprint(health_bp, url_prefix="/api")

    # Error handlers returning consistent JSON payloads
    @app.errorhandler(404)
    def not_found_error(error):
        return (
            jsonify(
                {
                    "error": "Not Found",
                    "message": "The requested resource was not found.",
                    "status_code": 404,
                }
            ),
            404,
        )

    @app.errorhandler(500)
    def internal_error(error):
        return (
            jsonify(
                {
                    "error": "Internal Server Error",
                    "message": "An unexpected server error occurred.",
                    "status_code": 500,
                }
            ),
            500,
        )

    return app
