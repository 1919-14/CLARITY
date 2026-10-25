import os
from flask import Flask, jsonify
from flask_cors import CORS
from .config import config_by_name, Config


def create_app(config_name=None):
    """Application factory for the CLARITY Flask backend."""
    app = Flask(__name__)

    if isinstance(config_name, dict):
        app.config.from_object(Config)
        app.config.update(config_name)
    else:
        if config_name is None:
            config_name = os.environ.get("FLASK_ENV", "development")
        config_class = config_by_name.get(config_name, Config)
        app.config.from_object(config_class)

    # Fail safely in production if a secure SECRET_KEY and DATABASE_URL are not explicitly provided
    if config_name == "production":
        secret_key = os.environ.get("SECRET_KEY")
        if not secret_key or secret_key == "dev-clarity-secret-key-change-in-production":
            raise ValueError(
                "A secure SECRET_KEY environment variable must be explicitly configured in production mode."
            )
        app.config["SECRET_KEY"] = secret_key

        database_url = os.environ.get("DATABASE_URL")
        if not database_url:
            raise ValueError(
                "A valid DATABASE_URL environment variable must be explicitly configured in production mode."
            )
        if database_url.startswith("sqlite"):
            raise ValueError(
                "SQLite database URI cannot be used in production environment. "
                "A production database (e.g., MySQL) URL must be configured."
            )
        app.config["SQLALCHEMY_DATABASE_URI"] = database_url

    # Configure CORS with explicit allowed origins without unnecessary credentials support
    cors_origins = app.config.get("CORS_ORIGINS", ["http://localhost:5173", "http://127.0.0.1:5173"])
    CORS(
        app,
        resources={r"/api/*": {"origins": cors_origins}},
        supports_credentials=False,
    )

    # Register blueprints — original /api routes (backward compatible)
    from .routes.health import health_bp
    from .routes.runs import runs_bp

    app.register_blueprint(health_bp, url_prefix="/api")
    app.register_blueprint(runs_bp, url_prefix="/api")

    # Versioned /api/v1 routes
    from .routes.v1 import (
        health_v1_bp,
        runs_v1_bp,
        hypotheses_v1_bp,
        observations_v1_bp,
        benchmarks_v1_bp,
        oracles_v1_bp,
    )

    app.register_blueprint(health_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(runs_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(hypotheses_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(observations_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(benchmarks_v1_bp, url_prefix="/api/v1")
    app.register_blueprint(oracles_v1_bp, url_prefix="/api/v1")

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

    # Recover interrupted runs left in active state across restart
    if app.config.get("RECOVER_ON_STARTUP", True) and not app.config.get("TESTING_SKIP_RECOVERY", False):
        try:
            from .adapters.persistence.database import get_db_manager
            from .domain.run_lifecycle import default_lifecycle_manager
            mgr = app.config.get("LIFECYCLE_MANAGER", default_lifecycle_manager)
            db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
            db = get_db_manager(db_uri)
            with db.session_scope() as session:
                mgr.recover_interrupted_runs(session)
        except Exception as exc:  # noqa: BLE001
            app.logger.warning("Could not execute restart recovery: %s", exc)

    return app
