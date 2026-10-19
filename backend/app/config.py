import os


class Config:
    """Base application configuration."""

    SECRET_KEY = os.environ.get(
        "SECRET_KEY", "dev-clarity-secret-key-change-in-production"
    )
    TESTING = False
    DEBUG = False
    ENV_NAME = "base"

    # CORS configuration: comma-separated list of allowed origins
    _cors_env = os.environ.get(
        "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )
    CORS_ORIGINS = [origin.strip() for origin in _cors_env.split(",") if origin.strip()]


class DevelopmentConfig(Config):
    """Development environment configuration."""

    DEBUG = True
    ENV_NAME = "development"


class TestingConfig(Config):
    """Testing environment configuration."""

    TESTING = True
    DEBUG = False
    ENV_NAME = "testing"
    # Allow test origins or allow all for isolated unit testing
    CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://testserver"]


class ProductionConfig(Config):
    """Production environment configuration."""

    DEBUG = False
    ENV_NAME = "production"


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
