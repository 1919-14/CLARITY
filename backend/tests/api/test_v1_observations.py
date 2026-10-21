"""API v1 tests: GET /api/v1/observations/<observation_id>.

Covers:
  - Single observation retrieval
  - 404 for unknown observation_id
  - Oracle internals never exposed
"""

import uuid
import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import ObservationModel
from app.adapters.persistence.repository import ClarityRepository


def _seed(app, run_id, seq=1, held_out=False):
    obs_id = f"obs-{uuid.uuid4().hex[:8]}"
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        repo.add_observation(ObservationModel(
            observation_id=obs_id,
            run_id=run_id,
            input_id=f"inp-{seq}",
            input_values={"x": seq},
            output_values={"y": seq * 3},
            status="success",
            sequence_number=seq,
            is_held_out=held_out,
        ))
    return obs_id


def _make_run(client, name="Obs Resource Run"):
    res = client.post("/api/v1/runs", json={"name": name})
    assert res.status_code == 201
    return res.get_json()["run"]


class TestGetObservation:
    def test_returns_existing_observation(self, client, app):
        """Single observation is returned with correct fields."""
        run = _make_run(client)
        obs_id = _seed(app, run["run_id"])
        res = client.get(f"/api/v1/observations/{obs_id}")
        assert res.status_code == 200
        data = res.get_json()["observation"]
        assert data["observation_id"] == obs_id
        assert data["input_values"] == {"x": 1}
        assert data["output_values"] == {"y": 3}
        assert data["status"] == "success"
        assert "run_id" in data
        assert "sequence_number" in data
        assert "is_held_out" in data
        assert "created_at" in data

    def test_unknown_observation_returns_404(self, client):
        """Non-existent observation_id returns 404."""
        res = client.get("/api/v1/observations/obs-does-not-exist")
        assert res.status_code == 404
        data = res.get_json()
        assert data["error"] == "Not Found"
        assert data["status_code"] == 404

    def test_no_oracle_internals_exposed(self, client, app):
        """Observation response never contains oracle formula or hidden ground truth."""
        run = _make_run(client, "Secret Obs")
        obs_id = _seed(app, run["run_id"])
        res = client.get(f"/api/v1/observations/{obs_id}")
        body = res.get_data(as_text=True)
        assert "oracle_formula" not in body
        assert "ground_truth" not in body
        assert "hidden" not in body

    def test_response_structure(self, client, app):
        """Response wraps observation under 'observation' key."""
        run = _make_run(client)
        obs_id = _seed(app, run["run_id"])
        res = client.get(f"/api/v1/observations/{obs_id}")
        assert "observation" in res.get_json()

    def test_method_not_allowed(self, client, app):
        """POST to single observation resource returns 405."""
        run = _make_run(client)
        obs_id = _seed(app, run["run_id"])
        res = client.post(f"/api/v1/observations/{obs_id}")
        assert res.status_code == 405
