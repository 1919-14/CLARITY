"""Tests for Scientific Benchmark Metrics and Ground-Truth Evaluation (Issue #24).

Verifies:
- Isolated BenchmarkEvaluator metrics (Hit@1, Hit@3, Hit@5, MRR, held-out MAE/RMSE).
- Hardened matching logic rejecting false positives on domain or degree alone.
- Isolated benchmark cases (constant, linear, quadratic, cubic) with exact evaluation.
- Benchmark service orchestration, baseline query efficiency calculation, and DB persistence.
- REST API endpoints (GET /runs/<run_id>/benchmarks, POST /runs/<run_id>/benchmarks).
- Strict trust boundary enforcement and zero leakage of ground-truth secrets.
"""

import json
import pytest
from datetime import datetime, timezone

from app import create_app
from app.adapters.persistence.database import get_db_manager, reset_db_manager
from app.adapters.persistence.models import (
    RunModel,
    HypothesisModel,
    HypothesisRevisionModel,
    EvaluationResultModel,
    UsageRecordModel,
    BenchmarkResultModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.contracts import (
    GroundTruthRule,
    HypothesisRepresentation,
    BenchmarkReport,
    verify_no_ground_truth_leakage,
)
from app.evaluator.benchmark_evaluator import BenchmarkEvaluator
from app.evaluator.benchmark_cases import (
    PolynomialGroundTruthRule,
    CONSTANT_BENCHMARK,
    LINEAR_BENCHMARK,
    QUADRATIC_BENCHMARK,
    CUBIC_BENCHMARK,
    get_benchmark_case,
    list_benchmark_cases,
)
from app.services.benchmark_service import (
    run_benchmark_evaluation,
    trigger_benchmark_service,
)
from app.routes.v1.shared import serialize_benchmark


# ==============================================================================
# 1. BenchmarkEvaluator Unit Tests
# ==============================================================================

class TestBenchmarkEvaluator:
    """Unit tests for BenchmarkEvaluator matching and Hit@K/MRR computation."""

    @pytest.fixture
    def linear_gt(self):
        return PolynomialGroundTruthRule(
            domain="polynomial",
            target_formula="y = 1.0 + 2.0*x",
            target_parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
            evaluator_label="target_linear_2x_plus_1",
            secret_seed=42,
        )

    def test_hit_at_1_and_mrr(self, linear_gt):
        evaluator = BenchmarkEvaluator(linear_gt)
        candidates = [
            HypothesisRepresentation(
                hypothesis_id="hyp-target",
                model_family="polynomial",
                formula="y = 1.0 + 2.0*x",
                parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
            ),
            HypothesisRepresentation(
                hypothesis_id="hyp-other",
                model_family="polynomial",
                formula="y = 5.0",
                parameters={"degree": 0, "coefficients": {"c0": 5.0}},
            ),
        ]

        report = evaluator.evaluate_candidates(
            run_id="run-01",
            ranked_candidates=candidates,
            queries_executed=10,
            held_out_mae=0.01,
            held_out_rmse=0.015,
        )

        assert report.hit_at_1 is True
        assert report.hit_at_3 is True
        assert report.hit_at_5 is True
        assert report.reciprocal_rank == 1.0
        assert report.best_candidate_id == "hyp-target"
        assert report.queries_to_target == 10
        assert report.held_out_mae == 0.01
        assert report.held_out_rmse == 0.015

    def test_hit_at_3_and_mrr(self, linear_gt):
        evaluator = BenchmarkEvaluator(linear_gt)
        candidates = [
            HypothesisRepresentation(
                hypothesis_id="hyp-1",
                model_family="polynomial",
                formula="y = 0.5*x",
                parameters={"degree": 1, "coefficients": {"c0": 0.0, "c1": 0.5}},
            ),
            HypothesisRepresentation(
                hypothesis_id="hyp-2",
                model_family="polynomial",
                formula="y = 3.0",
                parameters={"degree": 0, "coefficients": {"c0": 3.0}},
            ),
            HypothesisRepresentation(
                hypothesis_id="hyp-target",
                model_family="polynomial",
                formula="y = 1.0 + 2.0*x",
                parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
            ),
            HypothesisRepresentation(
                hypothesis_id="hyp-4",
                model_family="polynomial",
                formula="y = 0.0",
                parameters={"degree": 0, "coefficients": {"c0": 0.0}},
            ),
        ]

        report = evaluator.evaluate_candidates(run_id="run-02", ranked_candidates=candidates)

        assert report.hit_at_1 is False
        assert report.hit_at_3 is True
        assert report.hit_at_5 is True
        assert pytest.approx(report.reciprocal_rank, 1e-4) == 1.0 / 3.0
        assert report.best_candidate_id == "hyp-target"

    def test_hit_at_5_and_mrr(self, linear_gt):
        evaluator = BenchmarkEvaluator(linear_gt)
        candidates = [
            HypothesisRepresentation(
                hypothesis_id=f"hyp-{i}",
                model_family="polynomial",
                formula=f"y = {float(i)}",
                parameters={"degree": 0, "coefficients": {"c0": float(i)}},
            )
            for i in range(4)
        ]
        # Append target at rank 5
        candidates.append(
            HypothesisRepresentation(
                hypothesis_id="hyp-target",
                model_family="polynomial",
                formula="y = 1.0 + 2.0*x",
                parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
            )
        )

        report = evaluator.evaluate_candidates(run_id="run-03", ranked_candidates=candidates)

        assert report.hit_at_1 is False
        assert report.hit_at_3 is False
        assert report.hit_at_5 is True
        assert pytest.approx(report.reciprocal_rank, 1e-4) == 0.2
        assert report.best_candidate_id == "hyp-target"

    def test_no_match(self, linear_gt):
        evaluator = BenchmarkEvaluator(linear_gt)
        candidates = [
            HypothesisRepresentation(
                hypothesis_id="hyp-1",
                model_family="polynomial",
                formula="y = 99.0",
                parameters={"degree": 0, "coefficients": {"c0": 99.0}},
            )
        ]

        report = evaluator.evaluate_candidates(run_id="run-04", ranked_candidates=candidates)

        assert report.hit_at_1 is False
        assert report.hit_at_3 is False
        assert report.hit_at_5 is False
        assert report.reciprocal_rank == 0.0
        assert report.best_candidate_id == "hyp-1"

    def test_empty_candidates(self, linear_gt):
        evaluator = BenchmarkEvaluator(linear_gt)
        report = evaluator.evaluate_candidates(run_id="run-05", ranked_candidates=[])

        assert report.hit_at_1 is False
        assert report.hit_at_3 is False
        assert report.hit_at_5 is False
        assert report.reciprocal_rank == 0.0
        assert report.best_candidate_id == "none"

    def test_hardened_matching_rejects_loose_false_positives(self, linear_gt):
        """CRITICAL: Ensure candidates are NOT considered a match merely because

        model_family is 'polynomial' or degree is 1.
        """
        evaluator = BenchmarkEvaluator(linear_gt)

        # Same model family, completely different degree and function
        cand_cubic = HypothesisRepresentation(
            hypothesis_id="hyp-cubic",
            model_family="polynomial",
            formula="y = x^3",
            parameters={"degree": 3, "coefficients": {"c0": 0.0, "c1": 0.0, "c2": 0.0, "c3": 1.0}},
        )
        assert evaluator.is_match(cand_cubic) is False

        # Same degree (1), but completely different slope and intercept (y = 50 + 10x vs target y = 1 + 2x)
        cand_wrong_linear = HypothesisRepresentation(
            hypothesis_id="hyp-wrong-linear",
            model_family="polynomial",
            formula="y = 50.0 + 10.0*x",
            parameters={"degree": 1, "coefficients": {"c0": 50.0, "c1": 10.0}},
        )
        assert evaluator.is_match(cand_wrong_linear) is False

        # Close within tolerance -> matches
        cand_close = HypothesisRepresentation(
            hypothesis_id="hyp-close",
            model_family="polynomial",
            formula="y = 1.01 + 1.99*x",
            parameters={"degree": 1, "coefficients": {"c0": 1.01, "c1": 1.99}},
        )
        assert evaluator.is_match(cand_close) is True


# ==============================================================================
# 2. Benchmark Cases Suite Tests
# ==============================================================================

class TestBenchmarkCasesSuite:
    """Verify standard benchmark cases and exact numerical evaluation."""

    def test_constant_benchmark_exact(self):
        rule = CONSTANT_BENCHMARK.ground_truth
        assert rule.evaluate_exact({"x": 0.0})["y"] == 5.0
        assert rule.evaluate_exact({"x": 10.0})["y"] == 5.0

    def test_linear_benchmark_exact(self):
        rule = LINEAR_BENCHMARK.ground_truth
        # y = 1.0 + 2.0*x
        assert rule.evaluate_exact({"x": 0.0})["y"] == 1.0
        assert rule.evaluate_exact({"x": 2.0})["y"] == 5.0
        assert rule.evaluate_exact({"x": -1.0})["y"] == -1.0

    def test_quadratic_benchmark_exact(self):
        rule = QUADRATIC_BENCHMARK.ground_truth
        # y = -1.0 + 0.5*x^2
        assert rule.evaluate_exact({"x": 0.0})["y"] == -1.0
        assert rule.evaluate_exact({"x": 2.0})["y"] == 1.0

    def test_cubic_benchmark_exact(self):
        rule = CUBIC_BENCHMARK.ground_truth
        # y = 3.0*x - 0.5*x^3
        assert rule.evaluate_exact({"x": 0.0})["y"] == 0.0
        assert rule.evaluate_exact({"x": 2.0})["y"] == 3.0 * 2.0 - 0.5 * 8.0  # 6 - 4 = 2.0

    def test_list_and_get_benchmark_cases(self):
        names = list_benchmark_cases()
        assert "polynomial_constant_benchmark" in names
        assert "polynomial_linear_benchmark" in names
        assert "polynomial_quadratic_benchmark" in names
        assert "polynomial_cubic_benchmark" in names

        case = get_benchmark_case("polynomial_linear_benchmark")
        assert case is not None
        assert case.benchmark_name == "polynomial_linear_benchmark"
        assert case.ground_truth.evaluator_label == "target_linear_2x_plus_1"


# ==============================================================================
# 3. Benchmark Service Integration Tests
# ==============================================================================

class TestBenchmarkService:
    """Tests for benchmark_service execution and persistence."""

    @pytest.fixture
    def db_session(self):
        reset_db_manager()
        db = get_db_manager("sqlite:///:memory:")
        db.create_all_tables()
        with db.session_scope() as session:
            yield session
        db.drop_all_tables()
        reset_db_manager()

    def _setup_mock_run(self, session, run_id="run-test-01"):
        repo = ClarityRepository(session)
        now = datetime.now(timezone.utc)
        run = RunModel(
            run_id=run_id,
            name="Test Scientific Run",
            domain="polynomial",
            execution_mode="mock",
            sampling_strategy="random",
            status="completed",
            created_at=now,
        )
        repo.create_run(run)

        # Add usage
        usage = UsageRecordModel(
            usage_id="use-01",
            run_id=run_id,
            queries_executed=10,
            evaluations_completed=3,
            recorded_at=now,
        )
        repo.record_usage(usage)

        # Add hypotheses
        # Hypothesis 1: Target linear
        hyp1 = HypothesisModel(
            hypothesis_id="hyp-target-lin",
            run_id=run_id,
            model_family="polynomial",
            current_revision=1,
            is_pinned=False,
            is_excluded=False,
            created_at=now,
        )
        rev1 = HypothesisRevisionModel(
            revision_id="rev-01",
            hypothesis_id="hyp-target-lin",
            revision_number=1,
            formula="y = 1.0 + 2.0*x",
            parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
            complexity=2,
            created_at=now,
        )
        repo.create_hypothesis_with_initial_revision(hyp1, rev1)

        eval1 = EvaluationResultModel(
            evaluation_id="eval-01",
            revision_id="rev-01",
            run_id=run_id,
            status="completed",
            sample_count=8,
            metrics={"mae": 0.02, "rmse": 0.025},
            held_out_metrics={"mae": 0.035, "rmse": 0.042},
            evaluated_at=now,
        )
        repo.record_evaluation(eval1)

        # Hypothesis 2: Worse constant
        hyp2 = HypothesisModel(
            hypothesis_id="hyp-worse-const",
            run_id=run_id,
            model_family="polynomial",
            current_revision=1,
            is_pinned=False,
            is_excluded=False,
            created_at=now,
        )
        rev2 = HypothesisRevisionModel(
            revision_id="rev-02",
            hypothesis_id="hyp-worse-const",
            revision_number=1,
            formula="y = 100.0",
            parameters={"degree": 0, "coefficients": {"c0": 100.0}},
            complexity=1,
            created_at=now,
        )
        repo.create_hypothesis_with_initial_revision(hyp2, rev2)

        eval2 = EvaluationResultModel(
            evaluation_id="eval-02",
            revision_id="rev-02",
            run_id=run_id,
            status="completed",
            sample_count=8,
            metrics={"mae": 90.0, "rmse": 92.0},
            held_out_metrics={"mae": 95.0, "rmse": 97.0},
            evaluated_at=now,
        )
        repo.record_evaluation(eval2)
        return run

    def test_run_benchmark_evaluation_success(self, db_session):
        run = self._setup_mock_run(db_session, "run-svc-01")
        rule = LINEAR_BENCHMARK.ground_truth

        result = run_benchmark_evaluation(
            session=db_session,
            run_id=run.run_id,
            ground_truth=rule,
            benchmark_name="polynomial_linear_benchmark",
            baseline_type="random_sampling",
            expected_baseline_queries=30,
        )

        assert result.benchmark_id is not None
        assert result.run_id == "run-svc-01"
        assert result.hit_at_1 is True
        assert result.hit_at_3 is True
        assert result.hit_at_5 is True
        assert result.reciprocal_rank == 1.0
        assert result.best_candidate_id == "hyp-target-lin"
        assert result.queries_to_target == 10
        assert result.held_out_mae == 0.035
        assert result.held_out_rmse == 0.042
        assert result.baseline_type == "random_sampling"
        # 30 expected / 10 executed = 3.0x
        assert result.efficiency_gain == "3.0x"

        # Verify persisted in database
        repo = ClarityRepository(db_session)
        stored = repo.get_benchmark_results_for_run("run-svc-01")
        assert len(stored) == 1
        assert stored[0].benchmark_id == result.benchmark_id

    def test_trigger_benchmark_service_predefined_case(self, db_session):
        run = self._setup_mock_run(db_session, "run-svc-02")

        result = trigger_benchmark_service(
            session=db_session,
            run_id=run.run_id,
            benchmark_case="polynomial_linear_benchmark",
            baseline_type="random_sampling",
        )

        assert result.target_label == "target_linear_2x_plus_1"
        assert result.hit_at_1 is True
        assert result.queries_to_target == 10
        # Expected baseline for linear is 30 in BENCHMARK_CASES -> 30 / 10 = 3.0x
        assert result.efficiency_gain == "3.0x"

    def test_trigger_benchmark_service_unknown_run(self, db_session):
        with pytest.raises(ValueError, match="not found"):
            trigger_benchmark_service(
                session=db_session,
                run_id="non-existent-run",
                benchmark_case="polynomial_linear_benchmark",
            )


# ==============================================================================
# 4. REST API Endpoint Tests
# ==============================================================================

class TestBenchmarkAPI:
    """Test GET and POST /api/v1/runs/<run_id>/benchmarks."""

    def _create_run_and_data(self, app, run_id="run-api-01"):
        db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
        db = get_db_manager(db_uri)
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            now = datetime.now(timezone.utc)
            run = RunModel(
                run_id=run_id,
                name="API Benchmark Run",
                domain="polynomial",
                execution_mode="mock",
                sampling_strategy="random",
                status="completed",
                created_at=now,
            )
            repo.create_run(run)

            # Target hypothesis
            hyp = HypothesisModel(
                hypothesis_id="hyp-lin-api",
                run_id=run_id,
                model_family="polynomial",
                current_revision=1,
                created_at=now,
            )
            rev = HypothesisRevisionModel(
                revision_id="rev-api-01",
                hypothesis_id="hyp-lin-api",
                revision_number=1,
                formula="y = 1.0 + 2.0*x",
                parameters={"degree": 1, "coefficients": {"c0": 1.0, "c1": 2.0}},
                complexity=2,
                created_at=now,
            )
            repo.create_hypothesis_with_initial_revision(hyp, rev)

            ev = EvaluationResultModel(
                evaluation_id="eval-api-01",
                revision_id="rev-api-01",
                run_id=run_id,
                status="completed",
                sample_count=10,
                metrics={"mae": 0.01, "rmse": 0.015},
                held_out_metrics={"mae": 0.02, "rmse": 0.025},
                evaluated_at=now,
            )
            repo.record_evaluation(ev)

            usage = UsageRecordModel(
                usage_id="use-api-01",
                run_id=run_id,
                queries_executed=8,
                recorded_at=now,
            )
            repo.record_usage(usage)

    def test_get_benchmarks_empty(self, client, app):
        self._create_run_and_data(app, "run-api-empty")
        res = client.get("/api/v1/runs/run-api-empty/benchmarks")
        assert res.status_code == 200
        data = res.get_json()
        assert data["benchmarks"] == []
        assert data["count"] == 0

    def test_get_benchmarks_run_not_found(self, client):
        res = client.get("/api/v1/runs/does-not-exist/benchmarks")
        assert res.status_code == 404

    def test_post_benchmark_success(self, client, app):
        run_id = "run-api-bench-post"
        self._create_run_and_data(app, run_id)

        payload = {
            "benchmark_case": "polynomial_linear_benchmark",
            "baseline_type": "random_sampling",
        }
        res = client.post(f"/api/v1/runs/{run_id}/benchmarks", json=payload)
        assert res.status_code == 201
        data = res.get_json()
        assert "benchmark" in data
        b = data["benchmark"]
        assert b["target_label"] == "target_linear_2x_plus_1"
        assert b["hit_at_1"] is True
        assert b["reciprocal_rank"] == 1.0
        assert b["queries_to_target"] == 8
        assert b["held_out_mae"] == 0.02

        # Verify GET now returns this benchmark
        get_res = client.get(f"/api/v1/runs/{run_id}/benchmarks")
        assert get_res.status_code == 200
        get_data = get_res.get_json()
        assert get_data["count"] == 1
        assert get_data["benchmarks"][0]["benchmark_id"] == b["benchmark_id"]

    def test_post_benchmark_missing_target_or_case(self, client, app):
        run_id = "run-api-missing-target"
        self._create_run_and_data(app, run_id)

        res = client.post(f"/api/v1/runs/{run_id}/benchmarks", json={})
        assert res.status_code == 400
        data = res.get_json()
        assert "Either 'benchmark_case' or 'target_label' must be provided" in data["message"]

    def test_post_benchmark_unknown_case(self, client, app):
        run_id = "run-api-unknown-case"
        self._create_run_and_data(app, run_id)

        res = client.post(
            f"/api/v1/runs/{run_id}/benchmarks",
            json={"benchmark_case": "unknown_case_xyz"},
        )
        assert res.status_code == 400


# ==============================================================================
# 5. Strict Trust Boundary & Anti-Leakage Tests
# ==============================================================================

class TestTrustBoundaryIsolation:
    """Enforce structural isolation of GroundTruthRule and zero leakage into discovery."""

    def test_verify_no_ground_truth_leakage_in_serialized_benchmark(self):
        """Ensure serialize_benchmark() output passes verify_no_ground_truth_leakage."""
        b = BenchmarkResultModel(
            benchmark_id="bench-01",
            run_id="run-01",
            benchmark_name="polynomial_linear_benchmark",
            target_label="target_linear",
            hit_at_1=True,
            hit_at_3=True,
            hit_at_5=True,
            reciprocal_rank=1.0,
            best_candidate_id="hyp-01",
            queries_to_target=5,
            held_out_mae=0.01,
            held_out_rmse=0.02,
            baseline_type="random_sampling",
            efficiency_gain="4.0x",
            executed_at=datetime.now(timezone.utc),
        )
        serialized = serialize_benchmark(b)

        # Must not raise
        verify_no_ground_truth_leakage(serialized)

        # Explicit check that secret seeds or hidden parameters never exist in serialized dict
        assert "secret_seed" not in serialized
        assert "target_formula" not in serialized
        assert "target_parameters" not in serialized

    def test_discovery_modules_do_not_import_ground_truth(self):
        """Structural test: discovery modules and routes must NOT import GroundTruthRule or BenchmarkEvaluator."""
        import importlib

        discovery_modules = [
            "app.domain.discovery_engine",
            "app.domain.candidate_generator",
            "app.domain.llm_scientist",
            "app.domain.experiment_selection",
            "app.domain.fitting",
            "app.domain.ranking_engine",
            "app.routes.v1.runs",
            "app.routes.v1.shared",
        ]

        for mod_name in discovery_modules:
            mod = importlib.import_module(mod_name)
            assert not hasattr(mod, "GroundTruthRule"), f"Module {mod_name} imports GroundTruthRule!"
            assert not hasattr(mod, "BenchmarkEvaluator"), f"Module {mod_name} imports BenchmarkEvaluator!"
