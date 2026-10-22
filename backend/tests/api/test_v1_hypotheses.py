"""API v1 tests: Hypothesis resource endpoints.

Covers:
  GET    /api/v1/hypotheses/<id>             — retrieval, 404
  PATCH  /api/v1/hypotheses/<id>             — pin, exclude, restore, audit trail
  POST   /api/v1/hypotheses/<id>/reevaluate  — enqueue re-evaluation
  GET    /api/v1/hypotheses/<id>/evaluations — evaluation history
"""

import pytest


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _create_run(client, name="Hyp Test Run"):
    res = client.post("/api/v1/runs", json={"name": name})
    assert res.status_code == 201
    return res.get_json()["run"]


def _create_hypothesis(client, run_id, formula="y = a*x + b", model_family="linear"):
    res = client.post(f"/api/v1/runs/{run_id}/hypotheses", json={
        "model_family": model_family,
        "formula": formula,
        "parameters": {"a": 1.0, "b": 0.0},
        "complexity": 1,
    })
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["hypothesis"]


# ---------------------------------------------------------------------------
# GET /api/v1/hypotheses/<hypothesis_id>
# ---------------------------------------------------------------------------

class TestGetHypothesis:
    def test_returns_hypothesis_with_revisions(self, client):
        """Retrieves hypothesis with full revision history."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}")
        assert res.status_code == 200
        data = res.get_json()["hypothesis"]
        assert data["hypothesis_id"] == hyp["hypothesis_id"]
        assert "revisions" in data
        assert len(data["revisions"]) == 1
        assert data["revisions"][0]["formula"] == "y = a*x + b"

    def test_unknown_hypothesis_returns_404(self, client):
        """Non-existent hypothesis_id returns 404."""
        res = client.get("/api/v1/hypotheses/hyp-does-not-exist")
        assert res.status_code == 404
        data = res.get_json()
        assert data["error"] == "Not Found"

    def test_no_ground_truth_in_response(self, client):
        """Response must not expose oracle internals."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}")
        body = res.get_data(as_text=True)
        assert "ground_truth" not in body
        assert "oracle_formula" not in body


# ---------------------------------------------------------------------------
# PATCH /api/v1/hypotheses/<hypothesis_id>
# ---------------------------------------------------------------------------

class TestUpdateHypothesis:
    def test_pin_hypothesis(self, client):
        """Pinning a hypothesis sets is_pinned=True."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        assert hyp["is_pinned"] is False
        res = client.patch(
            f"/api/v1/hypotheses/{hyp['hypothesis_id']}",
            json={"is_pinned": True},
        )
        assert res.status_code == 200
        assert res.get_json()["hypothesis"]["is_pinned"] is True

    def test_unpin_hypothesis(self, client):
        """Unpinning sets is_pinned=False."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={"is_pinned": True})
        res = client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={"is_pinned": False})
        assert res.status_code == 200
        assert res.get_json()["hypothesis"]["is_pinned"] is False

    def test_exclude_hypothesis(self, client):
        """Excluding a hypothesis sets is_excluded=True."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.patch(
            f"/api/v1/hypotheses/{hyp['hypothesis_id']}",
            json={"is_excluded": True},
        )
        assert res.status_code == 200
        assert res.get_json()["hypothesis"]["is_excluded"] is True

    def test_restore_hypothesis(self, client):
        """Restoring sets is_excluded=False."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={"is_excluded": True})
        res = client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={"is_excluded": False})
        assert res.status_code == 200
        assert res.get_json()["hypothesis"]["is_excluded"] is False

    def test_pin_and_exclude_simultaneously(self, client):
        """Can pin and exclude in a single PATCH."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.patch(
            f"/api/v1/hypotheses/{hyp['hypothesis_id']}",
            json={"is_pinned": True, "is_excluded": True},
        )
        assert res.status_code == 200
        data = res.get_json()["hypothesis"]
        assert data["is_pinned"] is True
        assert data["is_excluded"] is True

    def test_patch_requires_at_least_one_field(self, client):
        """Empty patch body returns 400."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={})
        assert res.status_code == 400

    def test_patch_non_boolean_is_pinned_rejected(self, client):
        """Non-boolean is_pinned returns 400."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.patch(
            f"/api/v1/hypotheses/{hyp['hypothesis_id']}",
            json={"is_pinned": "yes"},
        )
        assert res.status_code == 400

    def test_patch_unknown_hypothesis_returns_404(self, client):
        """PATCH to unknown hypothesis_id returns 404."""
        res = client.patch("/api/v1/hypotheses/hyp-ghost", json={"is_pinned": True})
        assert res.status_code == 404

    def test_patch_non_json_returns_400(self, client):
        """Non-JSON PATCH body returns 400."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.patch(
            f"/api/v1/hypotheses/{hyp['hypothesis_id']}",
            data="not json",
            content_type="text/plain",
        )
        assert res.status_code == 400

    def test_pinning_does_not_affect_scientific_scores(self, client):
        """Pinning changes only display flag; revision formula and parameters unchanged."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"], formula="y = 2*x")
        formula_before = hyp["latest_revision"]["formula"]
        client.patch(f"/api/v1/hypotheses/{hyp['hypothesis_id']}", json={"is_pinned": True})
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}")
        assert res.get_json()["hypothesis"]["latest_revision"]["formula"] == formula_before


# ---------------------------------------------------------------------------
# POST /api/v1/hypotheses/<hypothesis_id>/reevaluate
# ---------------------------------------------------------------------------

class TestReevaluateHypothesis:
    def test_reevaluate_enqueues_pending_evaluation(self, client):
        """Re-evaluate returns 202 with a pending evaluation record."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        assert res.status_code == 202
        data = res.get_json()
        assert "evaluation" in data
        assert data["evaluation"]["status"] == "pending"
        assert "message" in data

    def test_reevaluate_returns_evaluation_id(self, client):
        """Re-evaluate response includes a valid evaluation_id."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        eval_id = res.get_json()["evaluation"]["evaluation_id"]
        assert eval_id.startswith("eva-")

    def test_reevaluate_unknown_hypothesis_returns_404(self, client):
        """Re-evaluate on unknown hypothesis returns 404."""
        res = client.post("/api/v1/hypotheses/hyp-ghost/reevaluate")
        assert res.status_code == 404

    def test_reevaluate_does_not_expose_oracle(self, client):
        """Re-evaluate response does not contain oracle formula or ground truth."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        body = res.get_data(as_text=True)
        assert "oracle_formula" not in body
        assert "ground_truth" not in body


# ---------------------------------------------------------------------------
# GET /api/v1/hypotheses/<hypothesis_id>/evaluations
# ---------------------------------------------------------------------------

class TestHypothesisEvaluations:
    def test_empty_evaluations(self, client):
        """Hypothesis with no evaluations returns empty list."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations")
        assert res.status_code == 200
        data = res.get_json()
        assert data["evaluations"] == []
        assert data["count"] == 0
        assert data["hypothesis_id"] == hyp["hypothesis_id"]

    def test_evaluations_appear_after_reevaluate(self, client):
        """Pending evaluation from /reevaluate appears in the evaluation history."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations")
        data = res.get_json()
        assert data["count"] == 1
        assert data["evaluations"][0]["status"] == "pending"

    def test_evaluations_unknown_hypothesis_returns_404(self, client):
        """Unknown hypothesis_id returns 404."""
        res = client.get("/api/v1/hypotheses/hyp-ghost/evaluations")
        assert res.status_code == 404

    def test_evaluation_response_no_oracle_data(self, client):
        """Evaluation history does not expose held-out oracle ground truth."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations")
        body = res.get_data(as_text=True)
        assert "oracle_formula" not in body
        assert "hidden" not in body
