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

    def test_propose_hypothesis_without_parameters_derives_exact_coefficients_and_predictions(self, db_session):
        """Proposing y = 2*x + 1 without parameters derives c0=1, c1=2; predictions and metrics match formula."""
        from app.domain.hypothesis_models import default_registry

        run = _create_test_run(db_session)
        # 1. Propose without parameters
        hyp = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="polynomial",
            formula="y = 2*x + 1",
            parameters=None,
        )

        rev = hyp.revisions[0]
        # Canonical polynomial specification must be populated with exact coefficients c0=1.0, c1=2.0
        assert rev.parameters["family"] == "polynomial"
        assert rev.parameters["degree"] == 1
        assert rev.parameters["coefficients"]["c0"] == 1.0
        assert rev.parameters["coefficients"]["c1"] == 2.0

        # 2. Verify model instantiation and predictions correspond to y = 2*x + 1
        family = default_registry().get("polynomial")
        model, err = family.parse_specification(rev.parameters)
        assert err is None
        assert model.degree == 1
        assert model.coefficient(0) == 1.0
        for x_val, expected_y in [(0.0, 1.0), (1.0, 3.0), (2.0, 5.0), (-2.0, -3.0)]:
            pred, p_err = family.predict(rev.parameters, {"x": x_val})
            assert p_err is None
            assert pred["y"] == pytest.approx(expected_y)

        # 3. Add observations adhering to y = 2*x + 1 and re-evaluate
        _add_observations(db_session, run.run_id, count=3)
        eval_result, _ = reevaluate_hypothesis_service(db_session, hyp.hypothesis_id)
        assert eval_result.status == "completed"
        # Actual values are y = 2*x + 1, predictions are 2*x + 1 -> MAE and RMSE must be 0.0
        assert eval_result.metrics["mae"] == pytest.approx(0.0, abs=1e-6)
        assert eval_result.metrics["rmse"] == pytest.approx(0.0, abs=1e-6)

    def test_propose_hypothesis_with_conflicting_parameters_rejected(self, db_session):
        """Formulas whose supplied parameters do not match formula coefficients are rejected."""
        run = _create_test_run(db_session)
        with pytest.raises(HypothesisValidationError) as exc_info:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = 2*x + 1",
                parameters={"c0": 99.0, "c1": 99.0},
            )
    def test_formula_with_symbolic_parameters_consistent_values(self, db_session):
        """Formulas referencing arbitrary parameter names resolve exact canonical coefficients and predict accurately."""
        from app.domain.hypothesis_models import default_registry

        run = _create_test_run(db_session)
        # y = a*x + b with a=3.0, b=-1.5
        hyp = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="linear",
            formula="y = a*x + b",
            parameters={"a": 3.0, "b": -1.5},
        )
        rev = hyp.revisions[0]
        assert rev.parameters["family"] == "polynomial"
        assert rev.parameters["degree"] == 1
        assert rev.parameters["coefficients"]["c0"] == -1.5
        assert rev.parameters["coefficients"]["c1"] == 3.0

        family = default_registry().get("polynomial")
        for x_val, expected_y in [(0.0, -1.5), (1.0, 1.5), (2.0, 4.5), (-1.0, -4.5)]:
            pred, p_err = family.predict(rev.parameters, {"x": x_val})
            assert p_err is None
            assert pred["y"] == pytest.approx(expected_y)

        # Arbitrary parameter names: y = m*x + c
        hyp2 = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="polynomial",
            formula="y = m*x + c",
            parameters={"m": 2.0, "c": 1.0},
        )
        rev2 = hyp2.revisions[0]
        assert rev2.parameters["coefficients"]["c0"] == 1.0
        assert rev2.parameters["coefficients"]["c1"] == 2.0

        # Arbitrary quadratic parameters: y = alpha*x^2 + beta*x + gamma
        hyp3 = propose_hypothesis_service(
            session=db_session,
            run_id=run.run_id,
            model_family="polynomial",
            formula="y = alpha*x^2 + beta*x + gamma",
            parameters={"alpha": 1.0, "beta": -2.0, "gamma": 3.0},
        )
        rev3 = hyp3.revisions[0]
        assert rev3.parameters["degree"] == 2
        assert rev3.parameters["coefficients"]["c0"] == 3.0
        assert rev3.parameters["coefficients"]["c1"] == -2.0
        assert rev3.parameters["coefficients"]["c2"] == 1.0

    def test_formula_with_symbolic_parameters_inconsistent_values_rejected(self, db_session):
        """Inconsistent parameter values for formulas with symbolic parameter names are rejected."""
        run = _create_test_run(db_session)

        # 1. Supplied canonical coefficient c1 contradicts symbolic parameter a
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters={"a": 2.0, "b": 1.0, "c1": 99.0},
            )
        assert "does not match formula coefficient" in str(exc.value)

        # 2. Supplied canonical coefficient c0 contradicts symbolic parameter b
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters={"a": 2.0, "b": 1.0, "c0": 42.0},
            )
        assert "does not match formula coefficient" in str(exc.value)

        # 3. Non-numeric parameter value for symbolic parameter
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="linear",
                formula="y = a*x + b",
                parameters={"a": "invalid", "b": 1.0},
            )
        assert "must be a numeric value" in str(exc.value)

        # 4. Non-finite parameter value (NaN)
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="linear",
                formula="y = a*x + b",
                parameters={"a": float("nan"), "b": 1.0},
            )
        assert "must be finite" in str(exc.value)

        # 4. Inconsistent degree supplied in parameters
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters={"a": 2.0, "b": 1.0, "degree": 4},
            )
        assert "Supplied degree (4) does not match formula degree (1)" in str(exc.value)

        # 5. Missing referenced parameter (b is omitted)
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters={"a": 2.0},
            )
        assert "Formula references parameter(s) ['b'] which were not supplied" in str(exc.value)

        # 6. Entire parameters mapping omitted when formula has symbolic variables
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters=None,
            )
        assert "Formula references parameter(s) ['a', 'b'] which were not supplied" in str(exc.value)

    def test_parameter_names_supplied_but_not_referenced_rejected(self, db_session):
        """Supplied parameters not actually referenced in the formula are strictly rejected."""
        run = _create_test_run(db_session)

        # 1. Unreferenced parameter passed with y = a*x + b
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = a*x + b",
                parameters={"a": 2.0, "b": 1.0, "unused_param": 42.0},
            )
        assert "Supplied parameter(s) ['unused_param'] are not referenced in the formula" in str(exc.value)

        # 2. Parameters a and b passed for concrete formula y = 2*x + 1 (where they are not referenced)
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = 2*x + 1",
                parameters={"a": 2.0, "b": 1.0},
            )
        assert "Supplied parameter(s) ['a', 'b'] are not referenced in the formula" in str(exc.value)

        # 3. Arbitrary extra parameter for concrete formula
        with pytest.raises(HypothesisValidationError) as exc:
            propose_hypothesis_service(
                session=db_session,
                run_id=run.run_id,
                model_family="polynomial",
                formula="y = 2*x + 1",
                parameters={"extra": 10.0},
            )
        assert "Supplied parameter(s) ['extra'] are not referenced in the formula" in str(exc.value)

    def test_symbolic_and_unreferenced_parameters_via_api(self, client):
        """API properly validates symbolic parameter resolution and rejects unreferenced parameters."""
        res_run = client.post("/api/v1/runs", json={"name": "Symbolic Param Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        # Valid y = a*x + b -> 201 Created
        res_ok = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={
                "model_family": "linear",
                "formula": "y = a*x + b",
                "parameters": {"a": 2.0, "b": 1.0},
            },
        )
        assert res_ok.status_code == 201
        hyp = res_ok.get_json()["hypothesis"]
        rev_params = hyp["latest_revision"]["parameters"]
        assert rev_params["coefficients"]["c1"] == 2.0
        assert rev_params["coefficients"]["c0"] == 1.0

        # Unreferenced parameter -> 422 Unprocessable Entity
        res_unref = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={
                "model_family": "linear",
                "formula": "y = a*x + b",
                "parameters": {"a": 2.0, "b": 1.0, "discarded_extra": 99.0},
            },
        )
        assert res_unref.status_code == 422
        assert "not referenced in the formula" in res_unref.get_json()["message"]

        # Inconsistent parameter (c1 contradicts a) -> 422 Unprocessable Entity
        res_incons = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={
                "model_family": "linear",
                "formula": "y = a*x + b",
                "parameters": {"a": 2.0, "b": 1.0, "c1": 99.0},
            },
        )
        assert res_incons.status_code == 422
        assert "does not match formula coefficient" in res_incons.get_json()["message"]



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

    def test_competing_flag_updates_rejected_by_version_token(self, db_session):
        """Two competing updates starting from the same version token cannot overwrite each other."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        initial_token = hyp.version_token
        initial_version = hyp.version
        assert initial_token is not None
        assert initial_version == 1

        # Client A updates with initial_token -> succeeds
        updated_a = update_hypothesis_flags_service(
            db_session,
            hyp.hypothesis_id,
            is_pinned=True,
            expected_version_token=initial_token,
        )
        assert updated_a.is_pinned is True
        assert updated_a.version == 2
        assert updated_a.version_token != initial_token

        # Client B submits competing update using same initial_token -> rejected with conflict
        with pytest.raises(HypothesisConflictError) as exc_info:
            update_hypothesis_flags_service(
                db_session,
                hyp.hypothesis_id,
                is_excluded=True,
                expected_version_token=initial_token,
            )
        assert "conflict" in str(exc_info.value).lower()

        # Client B re-reads latest state and submits with new token -> succeeds
        updated_b = update_hypothesis_flags_service(
            db_session,
            hyp.hypothesis_id,
            is_excluded=True,
            expected_version_token=updated_a.version_token,
        )
        assert updated_b.is_excluded is True
        assert updated_b.version == 3

    def test_competing_flag_updates_rejected_by_expected_revision(self, db_session):
        """Two competing updates starting from the same expected_revision cannot overwrite each other."""
        run = _create_test_run(db_session)
        hyp = propose_hypothesis_service(db_session, run.run_id, "polynomial", "y = 2*x + 1")
        assert hyp.current_revision == 1
        assert hyp.version == 1

        # Client 1 updates is_pinned=True with expected_revision=1 -> succeeds, version advances
        updated_1 = update_hypothesis_flags_service(
            db_session,
            hyp.hypothesis_id,
            is_pinned=True,
            expected_revision=1,
        )
        assert updated_1.is_pinned is True
        assert updated_1.version == 2

        # Client 2 also submitting expected_revision=1 -> rejected because version advanced
        with pytest.raises(HypothesisConflictError) as exc_info:
            update_hypothesis_flags_service(
                db_session,
                hyp.hypothesis_id,
                is_excluded=True,
                expected_revision=1,
            )
        assert "conflict" in str(exc_info.value).lower()

    def test_competing_flag_updates_via_api(self, client):
        """Two clients submitting competing PATCH requests: second client receives 409 Conflict."""
        res_run = client.post("/api/v1/runs", json={"name": "Competing Lock Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        res_hyp = client.post(
            f"/api/v1/runs/{run_id}/hypotheses",
            json={"model_family": "polynomial", "formula": "y = 2*x + 1"},
        )
        hyp_data = res_hyp.get_json()["hypothesis"]
        hyp_id = hyp_data["hypothesis_id"]
        v_token = hyp_data["version_token"]
        assert v_token is not None

        # Client A PATCH succeeds
        res_a = client.patch(
            f"/api/v1/hypotheses/{hyp_id}",
            json={"is_pinned": True, "expected_version_token": v_token},
        )
        assert res_a.status_code == 200
        new_token = res_a.get_json()["hypothesis"]["version_token"]
        assert new_token != v_token

        # Client B PATCH with stale v_token receives 409 Conflict
        res_b = client.patch(
            f"/api/v1/hypotheses/{hyp_id}",
            json={"is_excluded": True, "expected_version_token": v_token},
        )
        assert res_b.status_code == 409
        assert "conflict" in res_b.get_json()["error"].lower()

        # Client B retries with new_token -> succeeds
        res_b_retry = client.patch(
            f"/api/v1/hypotheses/{hyp_id}",
            json={"is_excluded": True, "expected_version_token": new_token},
        )
        assert res_b_retry.status_code == 200
        assert res_b_retry.get_json()["hypothesis"]["is_excluded"] is True

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

    def test_get_hypothesis_evidence_returns_classified_observations(self, client, db_session):
        """GET /api/v1/hypotheses/<id>/evidence returns classified observations with residuals and tolerances."""
        run = _create_test_run(db_session)
        # Create observations
        repo = ClarityRepository(db_session)
        for i in range(3):
            obs = ObservationModel(
                observation_id=f"obs-ev-{i}",
                run_id=run.run_id,
                input_id=f"in-{i}",
                input_values={"x": float(i)},
                output_values={"y": float(2 * i + 1)},
                status="success",
                sequence_number=i + 1,
                is_held_out=(i == 2),
            )
            repo.add_observation(obs)
        db_session.commit()

        # Propose hypothesis y = 2x + 1
        hyp = propose_hypothesis_service(
            db_session,
            run.run_id,
            "1d_polynomial",
            "y = 2*x + 1",
        )
        # Request evaluation
        res_eval = client.post(f"/api/v1/hypotheses/{hyp.hypothesis_id}/reevaluate")
        assert res_eval.status_code in (200, 202)

        # Retrieve evidence
        res = client.get(f"/api/v1/hypotheses/{hyp.hypothesis_id}/evidence")
        assert res.status_code == 200
        data = res.get_json()
        assert data["hypothesis_id"] == hyp.hypothesis_id
        assert "records" in data
        assert "summary" in data
        assert len(data["records"]) == 3
        # Invariant: y = 2x + 1 perfectly matches y = 2x + 1 so all 3 should be supporting
        assert data["summary"]["supporting_count"] == 3
        assert data["summary"]["contradicting_count"] == 0
        first_rec = data["records"][0]
        assert first_rec["classification"] == "supporting"
        assert first_rec["residual"] == pytest.approx(0.0)
        assert first_rec["observed_value"] == 1.0
        assert first_rec["predicted_value"] == 1.0

    def test_list_run_hypotheses_attaches_scientific_ranking(self, client, db_session):
        """GET /api/v1/runs/<run_id>/hypotheses attaches within_cohort_rank without conflating flags."""
        run = _create_test_run(db_session)
        repo = ClarityRepository(db_session)
        for i in range(4):
            obs = ObservationModel(
                observation_id=f"obs-rk-{i}",
                run_id=run.run_id,
                input_id=f"in-{i}",
                input_values={"x": float(i)},
                output_values={"y": float(2 * i + 1)},
                status="success",
                sequence_number=i + 1,
                is_held_out=(i >= 2),
            )
            repo.add_observation(obs)
        db_session.commit()

        # Hyp 1: exact match y = 2x + 1
        h1 = propose_hypothesis_service(
            db_session,
            run.run_id,
            "1d_polynomial",
            "y = 2*x + 1",
        )
        client.post(f"/api/v1/hypotheses/{h1.hypothesis_id}/reevaluate")

        # Hyp 2: poor fit constant y = 3
        h2 = propose_hypothesis_service(
            db_session,
            run.run_id,
            "1d_polynomial",
            "y = 3",
        )
        client.post(f"/api/v1/hypotheses/{h2.hypothesis_id}/reevaluate")

        # Hyp 3: pinned, but poor fit
        client.patch(f"/api/v1/hypotheses/{h2.hypothesis_id}", json={"is_pinned": True})

        res = client.get(f"/api/v1/runs/{run.run_id}/hypotheses")
        assert res.status_code == 200
        data = res.get_json()
        assert len(data["hypotheses"]) == 2

        h1_data = next(h for h in data["hypotheses"] if h["hypothesis_id"] == h1.hypothesis_id)
        h2_data = next(h for h in data["hypotheses"] if h["hypothesis_id"] == h2.hypothesis_id)

        assert h1_data["ranking"] is not None
        assert h1_data["scientific_ranking"] is not None
        assert h1_data["ranking"]["within_cohort_rank"] == 1
        assert h1_data["scientific_ranking"]["within_cohort_rank"] == 1
        assert h2_data["ranking"] is not None
        assert h2_data["scientific_ranking"] is not None
        assert h2_data["ranking"]["within_cohort_rank"] == 2
        assert h2_data["scientific_ranking"]["within_cohort_rank"] == 2
        # Invariant: pinning h2 does NOT make it rank #1 scientifically
        assert h2_data["is_pinned"] is True
        assert h1_data["is_pinned"] is False

    def test_post_evaluation_observation_is_unverified_in_older_snapshot(self, client, db_session):
        """Observations added after an evaluation cannot silently become verified members of an older snapshot."""
        run = _create_test_run(db_session)
        repo = ClarityRepository(db_session)
        obs1 = ObservationModel(
            observation_id="obs-snap-1",
            run_id=run.run_id,
            input_id="in-snap-1",
            input_values={"x": 1.0},
            output_values={"y": 3.0},
            status="success",
            sequence_number=1,
            is_held_out=False,
        )
        repo.add_observation(obs1)
        db_session.commit()

        hyp = propose_hypothesis_service(db_session, run.run_id, "1d_polynomial", "y = 2*x + 1")
        reeval_res = client.post(f"/api/v1/hypotheses/{hyp.hypothesis_id}/reevaluate")
        assert reeval_res.status_code in {200, 202}

        # Evidence before new observation
        ev_res1 = client.get(f"/api/v1/hypotheses/{hyp.hypothesis_id}/evidence")
        assert ev_res1.status_code == 200
        ev1 = ev_res1.get_json()
        assert ev1["count"] == 1
        assert ev1["summary"]["unverified_membership_count"] == 0
        assert ev1["records"][0]["membership_verified"] is True
        assert ev1["dataset_snapshot_id"] is not None

        # Add a new observation post-evaluation
        obs2 = ObservationModel(
            observation_id="obs-post-eval-2",
            run_id=run.run_id,
            input_id="in-snap-2",
            input_values={"x": 2.0},
            output_values={"y": 5.0},
            status="success",
            sequence_number=2,
            is_held_out=False,
        )
        repo.add_observation(obs2)
        db_session.commit()

        # Evidence after new observation: new observation must have membership_verified=False
        ev_res2 = client.get(f"/api/v1/hypotheses/{hyp.hypothesis_id}/evidence")
        assert ev_res2.status_code == 200
        ev2 = ev_res2.get_json()
        assert ev2["count"] == 2
        assert ev2["summary"]["unverified_membership_count"] == 1

        rec1 = next(r for r in ev2["records"] if r["observation_id"] == "obs-snap-1")
        rec2 = next(r for r in ev2["records"] if r["observation_id"] == "obs-post-eval-2")
        assert rec1["membership_verified"] is True
        assert rec1["post_evaluation"] is False
        assert rec2["membership_verified"] is False
        assert rec2["post_evaluation"] is True

    def test_failed_evaluation_surfaced_as_failed_and_unevaluable(self, client, db_session):
        """Failed evaluation is mapped to failed state and surfaces as unevaluable, not pending."""
        run = _create_test_run(db_session)
        repo = ClarityRepository(db_session)
        obs = ObservationModel(
            observation_id="obs-fail-1",
            run_id=run.run_id,
            input_id="in-fail-1",
            input_values={"x": 1.0},
            output_values={"y": 2.0},
            status="success",
            sequence_number=1,
            is_held_out=False,
        )
        repo.add_observation(obs)
        db_session.commit()

        hyp = propose_hypothesis_service(db_session, run.run_id, "1d_polynomial", "y = 2*x")
        rev_id = hyp.revisions[0].revision_id

        # Insert an explicitly failed evaluation
        eval_failed = EvaluationResultModel(
            evaluation_id="eval-failed-1",
            revision_id=rev_id,
            run_id=run.run_id,
            status="failed",
            sample_count=1,
            dataset_version=1,
            metrics={},
            held_out_metrics=None,
            error_message="Matrix singularity during fit",
        )
        repo.record_evaluation(eval_failed)
        db_session.commit()

        res = client.get(f"/api/v1/hypotheses/{hyp.hypothesis_id}/evidence")
        assert res.status_code == 200
        data = res.get_json()
        assert data["summary"]["evaluation_status"] == "failed"
        assert data["summary"]["plausibility_status"] == "unevaluable"


