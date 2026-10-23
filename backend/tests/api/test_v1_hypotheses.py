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


def _create_hypothesis(client, run_id, formula="y = a*x + b", model_family="linear", parameters=None):
    payload = {
        "model_family": model_family,
        "formula": formula,
        "complexity": 1,
    }
    if parameters is not None:
        payload["parameters"] = parameters
    elif "a" in formula or "b" in formula:
        payload["parameters"] = {"a": 1.0, "b": 0.0}
    res = client.post(f"/api/v1/runs/{run_id}/hypotheses", json=payload)
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
    def _seed_observation(self, app, run_id, x=1.0, y=2.0, seq=1):
        import uuid
        from app.adapters.persistence.database import get_db_manager
        from app.adapters.persistence.models import ObservationModel
        from app.adapters.persistence.repository import ClarityRepository
        db = get_db_manager(app.config.get("SQLALCHEMY_DATABASE_URI"))
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            repo.add_observation(ObservationModel(
                observation_id=f"obs-{uuid.uuid4().hex[:8]}",
                run_id=run_id,
                input_id=f"inp-{seq}",
                input_values={"x": x},
                output_values={"y": y},
                status="success",
                sequence_number=seq,
                is_held_out=False,
            ))

    def test_reevaluate_executes_pipeline_with_observations(self, client, app):
        """Re-evaluate executes through EvaluationPipeline when observations exist, returning 200."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        # Seed 2 observations so linear polynomial fitting succeeds
        self._seed_observation(app, run["run_id"], x=1.0, y=1.0, seq=1)
        self._seed_observation(app, run["run_id"], x=2.0, y=2.0, seq=2)

        res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        assert res.status_code == 200
        data = res.get_json()
        assert "evaluation" in data
        assert data["evaluation"]["status"] == "completed"
        assert "mae" in data["evaluation"]["metrics"]
        assert "rmse" in data["evaluation"]["metrics"]
        assert data["evaluation"]["sample_count"] == 2

    def test_reevaluate_handles_empty_observations(self, client):
        """Re-evaluate on run with no observations marks evaluation as failed (not phantom pending)."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        assert res.status_code == 200
        data = res.get_json()
        assert data["evaluation"]["status"] == "failed"
        assert data["evaluation"]["error_message"] is not None

    def test_reevaluate_dispatches_when_consumer_configured(self, client, app):
        """When an asynchronous queue/dispatcher is configured, work is enqueued returning 202."""
        dispatched_jobs = []

        def mock_dispatcher(run_id, hypothesis_id, evaluation_id=None):
            dispatched_jobs.append((run_id, hypothesis_id, evaluation_id))

        app.config["EVALUATION_DISPATCHER"] = mock_dispatcher
        try:
            run = _create_run(client)
            hyp = _create_hypothesis(client, run["run_id"])
            res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
            assert res.status_code == 202
            data = res.get_json()
            assert data["evaluation"]["status"] == "pending"
            assert "enqueued" in data["message"].lower()
            assert len(dispatched_jobs) == 1
            assert dispatched_jobs[0][0] == run["run_id"]
            assert dispatched_jobs[0][1] == hyp["hypothesis_id"]
        finally:
            app.config.pop("EVALUATION_DISPATCHER", None)

    def test_pending_reevaluation_is_consumed_by_worker(self, client, app):
        """Prove that a pending re-evaluation enqueued via dispatcher is consumed by the worker pipeline."""
        from app.adapters.persistence.database import get_db_manager
        from app.services.hypothesis_service import consume_pending_evaluation

        dispatched_queue = []

        def mock_dispatcher(run_id, hypothesis_id, evaluation_id=None):
            dispatched_queue.append((run_id, hypothesis_id, evaluation_id))

        app.config["EVALUATION_DISPATCHER"] = mock_dispatcher
        try:
            run = _create_run(client)
            hyp = _create_hypothesis(client, run["run_id"])
            # Seed observations so pipeline can fit and evaluate
            self._seed_observation(app, run["run_id"], x=1.0, y=1.0, seq=1)
            self._seed_observation(app, run["run_id"], x=2.0, y=2.0, seq=2)

            # 1. Enqueue re-evaluation -> returns 202 with pending status
            res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
            assert res.status_code == 202
            eval_id = res.get_json()["evaluation"]["evaluation_id"]
            assert res.get_json()["evaluation"]["status"] == "pending"
            assert len(dispatched_queue) == 1

            # 2. Evaluations history confirms it is pending before worker runs
            hist_before = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations").get_json()
            assert hist_before["evaluations"][0]["status"] == "pending"

            # 3. Worker consumer picks up and processes the pending evaluation
            db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
            db = get_db_manager(db_uri)
            with db.session_scope() as session:
                consumed = consume_pending_evaluation(session, evaluation_id=eval_id)
                assert consumed is not None
                assert consumed.evaluation_id == eval_id
                assert consumed.status == "completed"

            # 4. Evaluations history proves it transitioned to completed with real metrics
            hist_after = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations").get_json()
            assert hist_after["count"] == 1
            latest_eval = hist_after["evaluations"][0]
            assert latest_eval["status"] == "completed"
            assert latest_eval["sample_count"] == 2
            assert "mae" in latest_eval["metrics"]
            assert "rmse" in latest_eval["metrics"]
        finally:
            app.config.pop("EVALUATION_DISPATCHER", None)

    def test_reevaluate_deferred_mode_returns_truthful_501(self, client, app):
        """When EVALUATION_DEFERRED is enabled, endpoint truthfully returns 501 Not Implemented."""
        app.config["EVALUATION_DEFERRED"] = True
        try:
            run = _create_run(client)
            hyp = _create_hypothesis(client, run["run_id"])
            res = client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
            assert res.status_code == 501
            assert "deferred" in res.get_json()["message"].lower()
        finally:
            app.config.pop("EVALUATION_DEFERRED", None)

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
        """Evaluation from /reevaluate appears in the evaluation history."""
        run = _create_run(client)
        hyp = _create_hypothesis(client, run["run_id"])
        client.post(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/reevaluate")
        res = client.get(f"/api/v1/hypotheses/{hyp['hypothesis_id']}/evaluations")
        data = res.get_json()
        assert data["count"] == 1
        assert "status" in data["evaluations"][0]

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
