import os


class Config:
    """Base application configuration."""

    SECRET_KEY = None
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
    # Development default fallback key; strictly confined to development
    SECRET_KEY = os.environ.get(
        "SECRET_KEY", "dev-clarity-secret-key-change-in-production"
    )


class TestingConfig(Config):
    """Testing environment configuration."""

    TESTING = True
    DEBUG = False
    ENV_NAME = "testing"
    SECRET_KEY = "testing-secret-key"
    # Allow test origins for isolated unit testing
    CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://testserver"]


class ProductionConfig(Config):
    """Production environment configuration.
    
    Must not inherit or fall back to any hardcoded secret key.
    """

    DEBUG = False
    ENV_NAME = "production"
    SECRET_KEY = os.environ.get("SECRET_KEY")


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
