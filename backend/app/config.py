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


    # Database configuration
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///clarity_dev.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    @classmethod
    def llm_scientist_config(cls):
        """Optional LLM Scientist configuration (Issue #41), read from the environment.

        Single source of truth: ``LLMScientistConfig.from_environment`` owns the
        variable names, bounds, and defaults. Deterministic-only remains the
        default, and no credential is read here. Invalid values raise ``ValueError``
        so a misconfigured deployment fails loudly instead of silently running
        with an unintended provider.
        """
        from .domain.llm_scientist_contracts import LLMScientistConfig

        return LLMScientistConfig.from_environment()


class DevelopmentConfig(Config):
    """Development environment configuration."""

    DEBUG = True
    ENV_NAME = "development"
    # Development default fallback key; strictly confined to development
    SECRET_KEY = os.environ.get(
        "SECRET_KEY", "dev-clarity-secret-key-change-in-production"
    )
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///clarity_dev.db"
    )


class TestingConfig(Config):
    """Testing environment configuration."""

    TESTING = True
    DEBUG = False
    ENV_NAME = "testing"
    SECRET_KEY = "testing-secret-key"
    # Allow test origins for isolated unit testing
    CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://testserver"]
    # Fast in-memory SQLite database for isolated test execution
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///:memory:"
    )


class ProductionConfig(Config):
    """Production environment configuration.
    
    Must not inherit or fall back to any hardcoded secret key.
    """

    DEBUG = False
    ENV_NAME = "production"
    SECRET_KEY = os.environ.get("SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")


config_by_name = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
