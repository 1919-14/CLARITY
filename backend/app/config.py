import os
from pathlib import Path


# SQLite resolves a relative database path against the process working
# directory, so `python backend/run.py` (run from the repository root) and
# `python run.py` (run from backend/) used to open two different databases.
# Anchor the development default to the backend package directory so both
# invocations share one schema. Deployments should still set DATABASE_URL.
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{(BACKEND_DIR / 'clarity_dev.db').as_posix()}"


def normalize_database_url(url: str) -> str:
    """Anchor a relative SQLite file URL to the backend directory.

    `backend/.env` and `backend/.env.example` use `sqlite:///clarity_dev.db`.
    SQLite would interpret that against the process working directory, which
    made running from the repository root open a different, empty database.
    In-memory databases and absolute paths are returned unchanged.
    """
    prefix = "sqlite:///"
    if not url or not url.startswith(prefix) or url.startswith(prefix + "/"):
        return url
    path = url[len(prefix):]
    if path in ("", ":memory:") or Path(path).is_absolute():
        return url
    return f"{prefix}{(BACKEND_DIR / path).as_posix()}"


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


    # Uploaded black-box artifacts (researcher-supplied simulators and models).
    # Artifacts execute in-process, so this directory holds trusted code: keep it
    # outside the repository in deployments that accept uploads.
    ORACLE_STORAGE_DIR = os.environ.get("CLARITY_ORACLES_DIR") or None

    # Database configuration
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
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
    SQLALCHEMY_DATABASE_URI = normalize_database_url(
        os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
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
