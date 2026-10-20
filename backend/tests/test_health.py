import os
import pytest
from datetime import datetime
from app import create_app


def test_app_initialization_in_testing_mode(app):
    """Verify application initializes in testing mode without external dependencies."""
    assert app is not None
    assert app.config["TESTING"] is True
    assert app.config["ENV_NAME"] == "testing"


def test_health_endpoint_success(client):
    """Verify GET /api/health returns HTTP 200 and expected JSON structure."""
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.is_json

    data = response.get_json()
    assert data["status"] == "healthy"
    assert data["service"] == "clarity-backend"
    assert data["environment"] == "testing"
    assert "timestamp" in data

    # Verify timestamp parses as a valid ISO format datetime
    parsed_time = datetime.fromisoformat(data["timestamp"])
    assert parsed_time is not None


def test_404_not_found_returns_structured_json(client):
    """Verify unmatched routes return a consistent JSON 404 response."""
    response = client.get("/api/nonexistent_resource")

    assert response.status_code == 404
    assert response.is_json

    data = response.get_json()
    assert data["error"] == "Not Found"
    assert "message" in data
    assert data["status_code"] == 404


def test_cors_headers_without_credentials(client):
    """Verify CORS headers are returned for allowed origins and credentials are not enabled."""
    origin = "http://localhost:5173"
    response = client.get("/api/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == origin
    # Ensure supports_credentials is not enabled
    assert response.headers.get("Access-Control-Allow-Credentials") is None


def test_production_config_fails_without_secret_key(monkeypatch):
    """Verify production mode fails safely if SECRET_KEY is missing or uses dev default."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(ValueError, match="A secure SECRET_KEY"):
        create_app("production")

    monkeypatch.setenv("SECRET_KEY", "dev-clarity-secret-key-change-in-production")
    with pytest.raises(ValueError, match="A secure SECRET_KEY"):
        create_app("production")


def test_production_config_succeeds_with_valid_secret(monkeypatch):
    """Verify production mode succeeds when valid SECRET_KEY and DATABASE_URL are supplied."""
    monkeypatch.setenv("SECRET_KEY", "super-secret-production-key-xyz-987")
    monkeypatch.setenv("DATABASE_URL", "mysql+pymysql://clarity_user:secret@localhost:3306/clarity_prod")
    prod_app = create_app("production")
    assert prod_app is not None
    assert prod_app.config["ENV_NAME"] == "production"
    assert prod_app.config["SECRET_KEY"] == "super-secret-production-key-xyz-987"
    assert prod_app.config["SQLALCHEMY_DATABASE_URI"] == "mysql+pymysql://clarity_user:secret@localhost:3306/clarity_prod"
