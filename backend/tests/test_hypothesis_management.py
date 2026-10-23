"""Comprehensive acceptance tests for Human-in-the-Loop Hypothesis Management (Issue #16).

Verifies all seven core acceptance criteria:
1. Valid researcher hypothesis submission, stable ID, revision 1, researcher origin, and evaluation pipeline entry.
2. Rejection of arbitrary code execution, dynamic eval/exec, imports, attribute lookups, and malformed expressions.
3. Pinning and unpinning toggle display preference without modifying scientific scores or ranking.
4. Exclusion removes candidates from active search while preserving history, revisions, and evaluation records.
5. Restoring returns excluded candidate to active candidate search.
6. Re-evaluation evaluates against latest observations, records dataset version and metrics, and appends new evaluation records without overwriting past history.
7. Concurrency safeguards (optimistic locking via expected_revision, duplicate ID conflict) and predictable error handling (rejecting re-evaluation of excluded hypotheses).
"""

import uuid
import pytest
from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    AuditEventModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    RunModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
)
from app.domain.expression_validator import (
    ERROR_INVALID_DEGREE,
    ERROR_INVALID_EXPRESSION,
    ERROR_MALFORMED_EXPRESSION,
    ERROR_UNSAFE_EXPRESSION,
    validate_hypothesis_expression,
)
from app.domain.ranking_engine import HypothesisRanker, RankingPolicyConfig
from app.services.hypothesis_service import (
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    exclude_hypothesis_service,
    pin_hypothesis_service,
    propose_hypothesis_service,
    reevaluate_hypothesis_service,
    restore_hypothesis_service,
    unpin_hypothesis_service,
    update_hypothesis_flags_service,
)


# =============================================================================
# Fixtures and Helpers
# =============================================================================

@pytest.fixture
def db_session(app):
    """Provide a scoped database session for unit/service tests."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        yield session


def _create_test_run(session: db_session, name: str = "Hypothesis Management Test Run") -> RunModel:
    repo = ClarityRepository(session)
    run = RunModel(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        name=name,
        domain="physics",
        execution_mode="local",
        sampling_strategy="active_disagreement",
        status="running",
    )
    repo.create_run(run)
    session.flush()
    return run


def _add_observations(session: db_session, run_id: str, count: int = 5):
    repo = ClarityRepository(session)
    for i in range(1, count + 1):
        x_val = float(i)
        y_val = 2.0 * x_val + 1.0  # true law: y = 2x + 1
        obs = ObservationModel(
            observation_id=f"obs-{uuid.uuid4().hex[:8]}",
            run_id=run_id,
            input_id=f"inp-{i}",
            input_values={"x": x_val},
            output_values={"y": y_val},
            status="success",
            sequence_number=i,
            is_held_out=(i > 3),  # 3 training, 2 held-out
        )
        repo.add_observation(obs)
    session.flush()


# =============================================================================
# 1. Researcher-Created Hypotheses
# =============================================================================

class TestResearcherCreatedHypotheses:
    def test_propose_valid_hypothesis_persists_correctly(self, db_session):
        """Researchers can submit a valid hypothesis with stable ID and researcher origin."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="polynomial",
            formula="y = 2.0*x + 1.0",
            parameters={"c0": 1.0, "c1": 2.0},
            complexity=2,
            creation_source="researcher",
        )

        assert hyp.hypothesis_id.startswith("hyp-")
        assert hyp.run_id == run.run_id
        assert hyp.model_family == "polynomial"
        assert hyp.current_revision == 1
        assert hyp.is_pinned is False
        assert hyp.is_excluded is False

        # Verify revision 1 details
        assert len(hyp.revisions) == 1
        rev = hyp.revisions[0]
        assert rev.revision_number == 1
        assert rev.creation_source == "researcher"
        assert rev.formula == "y = 2.0*x + 1.0"

        # Verify audit trail
        repo = ClarityRepository(db_session)
        events = repo.list_audit_events_for_run(run.run_id)
        proposed_events = [e for e in events if e.event_type == "HYPOTHESIS_PROPOSED"]
        assert len(proposed_events) == 1
        assert proposed_events[0].actor == "researcher"
        assert proposed_events[0].metadata_json["hypothesis_id"] == hyp.hypothesis_id

    def test_propose_hypothesis_via_api(self, client):
        """HTTP POST /api/v1/runs/<run_id>/hypotheses creates hypothesis and returns 201."""
        res_run = client.post("/api/v1/runs", json={"name": "API Hyp Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={
                "model_family": "1d_polynomial",
                "formula": "y = 3*x^2 - 1.5*x + 4",
                "parameters": {"c2": 3.0, "c1": -1.5, "c0": 4.0},
                "complexity": 3,
            },
        )
        assert res.status_code == 201
        data = res.get_json()["hypothesis"]
        assert data["run_id"] == run_id
        assert data["latest_revision"]["formula"] == "y = 3*x^2 - 1.5*x + 4"
        assert data["latest_revision"]["creation_source"] == "researcher"


# =============================================================================
# 2. Expression Validation & Security (Arbitrary Code Rejection)
# =============================================================================

class TestExpressionSecurityAndValidation:
    @pytest.mark.parametrize(
        "malicious_formula",
        [
            "__import__('os').system('echo hacked')",
            "exec('import sys; sys.exit(1)')",
            "eval('2 + 2')",
            "open('/etc/passwd').read()",
            "os.remove('file.txt')",
            "subprocess.Popen('ls')",
            "__builtins__.__import__('os').system('id')",
            "lambda x: x + 1",
            "x.__class__.__bases__",
            "(lambda: 1)()",
        ],
    )
    def test_malicious_expressions_strictly_rejected(self, malicious_formula):
        """Proactively reject code execution, dynamic imports, and unsafe constructs."""
        validated, err = validate_hypothesis_expression(formula=malicious_formula)
        assert validated is None
        assert err is not None
        assert err.code in (ERROR_UNSAFE_EXPRESSION, ERROR_INVALID_EXPRESSION, ERROR_MALFORMED_EXPRESSION)

    def test_propose_malicious_formula_raises_validation_error(self, db_session):
        """propose_hypothesis_service raises HypothesisValidationError on code execution attempts."""
        run = _create_test_run(db_session)
        with pytest.raises(HypothesisValidationError) as exc_info:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="__import__('os').system('cat /etc/passwd')",
            )
        assert "Unsafe expression rejected" in str(exc_info.value)

    def test_propose_malicious_formula_via_api_returns_422(self, client):
        """API returns HTTP 422 with actionable error when malicious expression is submitted."""
        res_run = client.post("/api/v1/runs", json={"name": "Security Test Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={
                "model_family": "polynomial",
                "formula": "os.system('rm -rf /')",
            },
        )
        assert res.status_code == 422
        body = res.get_json()
        assert "Unsafe expression" in body["error"] or "Unsafe expression" in body.get("message", "")

    def test_malformed_syntax_rejected(self, db_session):
        """Malformed mathematical syntax is rejected with actionable error."""
        run = _create_test_run(db_session)
        with pytest.raises(HypothesisValidationError) as exc_info:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = 2 * * x + ",
            )
        assert "Malformed" in str(exc_info.value) or "syntax" in str(exc_info.value)

    def test_division_by_zero_rejected(self):
        """Expressions with division by literal zero are rejected."""
        val, err = validate_hypothesis_expression(formula="y = x / 0")
        assert val is None
        assert err is not None
        assert "Division by zero" in err.message

    def test_degree_exceeding_hard_ceiling_rejected(self):
        """Polynomial degrees exceeding the configured ceiling are rejected."""
        val, err = validate_hypothesis_expression(formula="y = x^15")
        assert val is None
        assert err is not None
        assert err.code in (ERROR_INVALID_DEGREE, ERROR_INVALID_EXPRESSION)


# =============================================================================
# 3. Pinning and Unpinning (Display Preference, No Score Distortion)
# =============================================================================

class TestPinningLifecycle:
    def test_pin_and_unpin_services(self, db_session):
        """Pinning sets is_pinned=True, unpinning sets is_pinned=False, recording audit events."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="polynomial",
            formula="y = 3*x",
        )
        assert hyp.is_pinned is False

        # Pin
        pinned = pin_hypothesis_service(db_session, hyp.hypothesis_id)
        assert pinned.is_pinned is True

        # Unpin
        unpinned = unpin_hypothesis_service(db_session, hyp.hypothesis_id)
        assert unpinned.is_pinned is False

        # Audit events check
        repo = ClarityRepository(db_session)
        events = repo.list_audit_events_for_run(run.run_id)
        event_types = [e.event_type for e in events]
        assert "HYPOTHESIS_PINNED" in event_types
        assert "HYPOTHESIS_UNPINNED" in event_types

    def test_pinning_does_not_alter_scientific_rankings(self, db_session):
        """Pinning candidate hypotheses must never alter the ranker's metric scoring or order."""
        run = _create_test_run(db_session)
        _add_observations(db_session, run.run_id, count=5)

        hyp_a = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        hyp_b = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 5*x + 10")

        # Evaluate both
        eval_a, _ = reevaluate_hypothesis_service(db_session, hyp_a.hypothesis_id)
        eval_b, _ = reevaluate_hypothesis_service(db_session, hyp_b.hypothesis_id)

        # Build domain representation for ranking
        rep_a = HypothesisRepresentation(
            model_family="polynomial",
            formula=hyp_a.revisions[0].formula,
            parameters=hyp_a.revisions[0].parameters,
            complexity=hyp_a.revisions[0].complexity,
            hypothesis_id=hyp_a.hypothesis_id,
            revision=1,
        )
        rep_b = HypothesisRepresentation(
            model_family="polynomial",
            formula=hyp_b.revisions[0].formula,
            parameters=hyp_b.revisions[0].parameters,
            complexity=hyp_b.revisions[0].complexity,
            hypothesis_id=hyp_b.hypothesis_id,
            revision=1,
        )

        domain_eval_a = EvaluationResult(
            evaluation_id=eval_a.evaluation_id,
            hypothesis_id=hyp_a.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            sample_count=eval_a.sample_count,
            metrics=eval_a.metrics,
            model_family="polynomial",
        )
        domain_eval_b = EvaluationResult(
            evaluation_id=eval_b.evaluation_id,
            hypothesis_id=hyp_b.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            sample_count=eval_b.sample_count,
            metrics=eval_b.metrics,
            model_family="polynomial",
        )

        from app.domain.contracts import Observation, ObservationStatus
        repo = ClarityRepository(db_session)
        obs_models = repo.list_observations_for_run(run.run_id)
        obs_domain = [
            Observation(
                observation_id=o.observation_id,
                input_id=o.input_id,
                input_values=o.input_values or {},
                output_values=o.output_values,
                status=ObservationStatus.SUCCESS,
                sequence_number=o.sequence_number,
                is_held_out=o.is_held_out,
            )
            for o in obs_models
        ]

        ranker = HypothesisRanker()
        eval_map = {hyp_a.hypothesis_id: domain_eval_a, hyp_b.hypothesis_id: domain_eval_b}
        obs_map = {hyp_a.hypothesis_id: obs_domain, hyp_b.hypothesis_id: obs_domain}

        ranking_unpinned = ranker.rank_candidates(
            candidates=[rep_a, rep_b],
            evaluation_results=eval_map,
            observations_by_hypothesis=obs_map,
        )

        # Now pin hyp_b (the worse-fitting hypothesis)
        pin_hypothesis_service(db_session, hyp_b.hypothesis_id)

        ranking_with_pinned = ranker.rank_candidates(
            candidates=[rep_a, rep_b],
            evaluation_results=eval_map,
            observations_by_hypothesis=obs_map,
        )

        # Pinned flag must NOT change rank order or scientific scores
        unpinned_ids = [c.hypothesis_id for c in ranking_unpinned.ranked_candidates]
        pinned_ids = [c.hypothesis_id for c in ranking_with_pinned.ranked_candidates]
        assert unpinned_ids == pinned_ids
        for c1, c2 in zip(ranking_unpinned.ranked_candidates, ranking_with_pinned.ranked_candidates):
            assert c1.rank == c2.rank
            assert c1.training_score == c2.training_score


# =============================================================================
# 4 & 5. Exclusion and Restoration
# =============================================================================

class TestExclusionAndRestoration:
    def test_exclusion_omits_candidate_from_active_search_preserves_history(self, db_session):
        """Excluding sets is_excluded=True and removes candidate from active list without deleting DB rows."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")

        # Exclude
        exclude_hypothesis_service(db_session, hyp.hypothesis_id)
        assert hyp.is_excluded is True

        repo = ClarityRepository(db_session)

        # Active search query excludes the hypothesis
        active_candidates = repo.list_hypotheses_for_run(run.run_id, include_excluded=False)
        assert hyp.hypothesis_id not in [h.hypothesis_id for h in active_candidates]

        # Historical query includes the hypothesis
        all_hypotheses = repo.list_hypotheses_for_run(run.run_id, include_excluded=True)
        assert hyp.hypothesis_id in [h.hypothesis_id for h in all_hypotheses]

        # Revisions are preserved
        assert len(hyp.revisions) == 1

        # Audit event recorded
        events = repo.list_audit_events_for_run(run.run_id)
        assert any(e.event_type == "HYPOTHESIS_EXCLUDED" for e in events)

    def test_restore_returns_candidate_to_active_search(self, db_session):
        """Restoring an excluded candidate returns it to active candidate selection."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        exclude_hypothesis_service(db_session, hyp.hypothesis_id)

        # Restore
        restored = restore_hypothesis_service(db_session, hyp.hypothesis_id)
        assert restored.is_excluded is False

        repo = ClarityRepository(db_session)
        active_candidates = repo.list_hypotheses_for_run(run.run_id, include_excluded=False)
        assert hyp.hypothesis_id in [h.hypothesis_id for h in active_candidates]

        events = repo.list_audit_events_for_run(run.run_id)
        assert any(e.event_type == "HYPOTHESIS_RESTORED" for e in events)


# =============================================================================
# 6. Re-Evaluation and History Preservation
# =============================================================================

class TestReevaluationAndHistory:
    def test_reevaluate_creates_new_record_preserving_history(self, db_session):
        """Each re-evaluation creates a distinct EvaluationResultModel record without overwriting past results."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")

        # 1. Initial 3 observations
        _add_observations(db_session, run.run_id, count=3)
        eval1, _ = reevaluate_hypothesis_service(db_session, hyp.hypothesis_id)
        assert eval1.status == "completed"
        assert eval1.sample_count == 3
        first_eval_id = eval1.evaluation_id

        # 2. Add 2 more observations (total 5)
        _add_observations(db_session, run.run_id, count=2)
        eval2, _ = reevaluate_hypothesis_service(db_session, hyp.hypothesis_id)
        assert eval2.status == "completed"
        assert eval2.sample_count == 5
        second_eval_id = eval2.evaluation_id

        # Two distinct records must exist in DB
        assert first_eval_id != second_eval_id
        repo = ClarityRepository(db_session)
        evaluations = repo.list_evaluations_for_revision(hyp.revisions[0].revision_id)
        assert len(evaluations) == 2
        eval_ids = [e.evaluation_id for e in evaluations]
        assert first_eval_id in eval_ids
        assert second_eval_id in eval_ids

    def test_reevaluate_excluded_hypothesis_rejected(self, db_session):
        """Re-evaluating an excluded hypothesis is rejected with clear error requiring restoration first."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        exclude_hypothesis_service(db_session, hyp.hypothesis_id)

        with pytest.raises(HypothesisValidationError) as exc_info:
            reevaluate_hypothesis_service(db_session, hyp.hypothesis_id)
        assert "Cannot re-evaluate excluded hypothesis" in str(exc_info.value)
        assert "Restore it first" in str(exc_info.value)

    def test_reevaluate_excluded_hypothesis_via_api_returns_422(self, client):
        """API returns 422 when attempting to re-evaluate an excluded hypothesis."""
        res_run = client.post("/api/v1/runs", json={"name": "Exclude Reeval Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res_hyp = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "polynomial", "formula": "y = 2*x + 1"},
        )
        hyp_id = res_hyp.get_json()["hypothesis"]["hypothesis_id"]

        # Exclude
        client.patch(f"/api/v1/hypotheses/{hyp_id}", json={"is_excluded": True})

        # Attempt re-evaluate
        res = client.post(f"/api/v1/hypotheses/{hyp_id}/reevaluate")
        assert res.status_code == 422
        body = res.get_json()
        assert "excluded" in body.get("message", "").lower()


# =============================================================================
# 7. Concurrency & Optimistic Locking Safeguards
# =============================================================================

class TestConcurrencyAndConflicts:
    def test_optimistic_locking_prevents_stale_update(self, db_session):
        """Updating flags with an outdated expected_revision raises HypothesisConflictError."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        assert hyp.current_revision == 1

        # Matching expected_revision succeeds
        update_hypothesis_flags_service(
            db_session,
            hyp.hypothesis_id,
            is_pinned=True,
            expected_revision=1,
        )

        # Stale expected_revision raises ConflictError
        with pytest.raises(HypothesisConflictError) as exc_info:
            update_hypothesis_flags_service(
                db_session,
                hyp.hypothesis_id,
                is_pinned=False,
                expected_revision=99,
            )
        assert "revision conflict" in str(exc_info.value)

    def test_optimistic_locking_via_api_returns_409(self, client):
        """PATCH with stale expected_revision returns HTTP 409 Conflict."""
        res_run = client.post("/api/v1/runs", json={"name": "Locking Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res_hyp = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "polynomial", "formula": "y = 2*x + 1"},
        )
        hyp_id = res_hyp.get_json()["hypothesis"]["hypothesis_id"]

        res = client.patch(
            f"/api/v1/hypotheses/{hyp_id}",
            json={"is_pinned": True, "expected_revision": 42},
        )
        assert res.status_code == 409
        assert "conflict" in res.get_json()["error"].lower()

    def test_duplicate_hypothesis_id_rejected_with_conflict(self, db_session):
        """Proposing hypothesis with existing ID raises HypothesisConflictError."""
        run = _create_test_run(db_session)
        hyp_id = f"hyp-fixed-{uuid.uuid4().hex[:6]}"
        propose_hypothesis_service(
            db_session,
            run.run_id,
            "polynomial",
            "y = x + 1",
            hypothesis_id=hyp_id,
        )

        with pytest.raises(HypothesisConflictError):
            propose_hypothesis_service(
                db_session,
                run.run_id,
                "polynomial",
                "y = x + 2",
                hypothesis_id=hyp_id,
            )
