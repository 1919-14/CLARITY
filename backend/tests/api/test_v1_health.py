"""API v1 tests: GET /api/v1/health and dependency status.

Verifies:
  - 200 with healthy status when database is reachable
  - Response includes version, service, environment, timestamp, and dependencies
  - No secrets or internal URIs appear in the response
"""

import pytest


def test_health_returns_200_healthy(client):
    """Health check returns 200 with healthy status."""
    res = client.get("/api/v1/health")
    assert res.status_code == 200
    data = res.get_json()
    assert data["status"] == "healthy"
    assert data["service"] == "clarity-backend"
    assert data["version"] == "v1"
    assert "environment" in data
    assert "timestamp" in data


def test_health_includes_dependency_status(client):
    """Health response includes database dependency status."""
    res = client.get("/api/v1/health")
    data = res.get_json()
    assert "dependencies" in data
    deps = data["dependencies"]
    assert "database" in deps
    # In testing with SQLite in-memory the DB should be ok
    assert deps["database"] == "ok"


def test_health_no_secrets_leaked(client):
    """Health response must not expose credentials, URIs, or stack traces."""
    res = client.get("/api/v1/health")
    body = res.get_data(as_text=True)
    assert "sqlite" not in body.lower()
    assert "mysql" not in body.lower()
    assert "password" not in body.lower()
    assert "secret" not in body.lower()
    assert "traceback" not in body.lower()


def test_health_method_not_allowed(client):
    """POST to /health should return 405."""
    res = client.post("/api/v1/health")
    assert res.status_code == 405
