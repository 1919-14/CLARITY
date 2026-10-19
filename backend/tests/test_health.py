from datetime import datetime


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


def test_cors_headers_on_health_endpoint(client):
    """Verify CORS headers are returned for allowed origins."""
    origin = "http://localhost:5173"
    response = client.get("/api/health", headers={"Origin": origin})

    assert response.status_code == 200
    assert response.headers.get("Access-Control-Allow-Origin") == origin
