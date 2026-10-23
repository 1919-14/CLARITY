"""Comprehensive unit and integration tests for Scientific Report Generation and Export (Issue #23).

Tests:
1. Completed run produces a report with ranked hypotheses.
2. Sparse run (< 3 eligible candidates) reports only available ones without fabricating candidates.
3. Every reported metric is traceable to an evaluation result and dataset version.
4. Missing metrics remain unavailable (null), never defaulted to zero.
5. Incomplete, cancelled, or budget-exhausted runs are clearly labelled as partial.
6. Failed evaluations are explicitly identified and not misrepresented as valid scientific results.
7. Ranking criteria are explained without fabricated confidence percentages (e.g. "92% sure").
8. Evidence and provenance: supporting and contradicting observations reference real stored observations.
9. JSON and Markdown exports preserve content, metadata, and format appropriately.
10. Historical integrity & idempotency: re-evaluations do not silently rewrite existing reports.
11. REST API endpoints (GET report, POST report generation, GET report export).
"""

from datetime import datetime, timezone
import json
import pytest

from app.adapters.persistence.database import get_db_manager
from app.adapters.persistence.models import (
    CandidateExperimentModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from app.adapters.persistence.repository import ClarityRepository
from app.domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    HypothesisStatus,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.report_contracts import (
    ReportStatus,
    ScientificReport,
)
from app.domain.report_exporters import (
    export_report_as_json,
    export_report_as_markdown,
)
import os
from unittest.mock import patch

from app.domain.report_generator import generate_scientific_report
from app.services.report_service import (
    ReportNotAvailableError,
    _load_report_artifact,
    _save_report_artifact,
    export_report_service,
    generate_report_service,
    get_report_service,
)
from app.services.run_service import create_run_service


@pytest.fixture
def db_session(app):
    """Provide an isolated database session for testing."""
    db_uri = app.config.get("SQLALCHEMY_DATABASE_URI")
    db = get_db_manager(db_uri)
    with db.session_scope() as session:
        yield session


def _populate_completed_run(session: db_session, run_id: str = "run-comp-01") -> RunModel:
    """Helper creating a completed run with hypotheses, revisions, evaluations, and observations."""
    run = RunModel(
        run_id=run_id,
        name="Completed Polynomial Discovery",
        domain="1d_polynomial",
        status="completed",
        execution_mode="local",
        sampling_strategy="active_disagreement",
        stop_reason="target_accuracy_reached",
        max_queries=10,
        max_evaluations=50,
        max_runtime_seconds=3600,
        spend_ceiling_usd=10.0,
        started_at=datetime(2026, 10, 23, 10, 0, 0, tzinfo=timezone.utc),
        completed_at=datetime(2026, 10, 23, 10, 15, 0, tzinfo=timezone.utc),
    )
    session.add(run)

    # Add 5 observations (4 training, 1 held-out)
    for i in range(1, 6):
        is_held_out = (i == 5)
        obs = ObservationModel(
            observation_id=f"obs-{run_id}-{i}",
            run_id=run_id,
            input_id=f"inp-{i}",
            input_values={"x": float(i)},
            output_values={"y": float(2 * i + 1)},  # Ground truth y = 2x + 1
            status="success",
            sequence_number=i,
            is_held_out=is_held_out,
        )
        session.add(obs)

    # Candidate 1: y = 2x + 1 (optimal)
    h1 = HypothesisModel(
        hypothesis_id=f"hyp-{run_id}-1",
        run_id=run_id,
        model_family="polynomial",
        current_revision=1,
    )
    session.add(h1)
    r1 = HypothesisRevisionModel(
        revision_id=f"rev-{run_id}-1",
        hypothesis_id=h1.hypothesis_id,
        revision_number=1,
        formula="y = 2.0*x + 1.0",
        parameters={"coefficients": [1.0, 2.0], "degree": 1},
        complexity=2,
    )
    session.add(r1)
    ev1 = EvaluationResultModel(
        evaluation_id=f"ev-{run_id}-1",
        revision_id=r1.revision_id,
        run_id=run_id,
        status="completed",
        sample_count=4,
        dataset_version=5,
        metrics={"mae": 0.001, "rmse": 0.001, "r2": 0.999},
        held_out_metrics={"mae": 0.002, "rmse": 0.002, "r2": 0.999},
    )
    session.add(ev1)

    # Candidate 2: y = 2.5x + 0.5 (near-optimal)
    h2 = HypothesisModel(
        hypothesis_id=f"hyp-{run_id}-2",
        run_id=run_id,
        model_family="polynomial",
        current_revision=1,
    )
    session.add(h2)
    r2 = HypothesisRevisionModel(
        revision_id=f"rev-{run_id}-2",
        hypothesis_id=h2.hypothesis_id,
        revision_number=1,
        formula="y = 2.5*x + 0.5",
        parameters={"coefficients": [0.5, 2.5], "degree": 1},
        complexity=2,
    )
    session.add(r2)
    ev2 = EvaluationResultModel(
        evaluation_id=f"ev-{run_id}-2",
        revision_id=r2.revision_id,
        run_id=run_id,
        status="completed",
        sample_count=4,
        dataset_version=5,
        metrics={"mae": 1.25, "rmse": 1.45, "r2": 0.85},
        held_out_metrics={"mae": 2.0, "rmse": 2.0, "r2": 0.80},
    )
    session.add(ev2)

    # Candidate 3: y = 0.5x^2 (poor fit)
    h3 = HypothesisModel(
        hypothesis_id=f"hyp-{run_id}-3",
        run_id=run_id,
        model_family="polynomial",
        current_revision=1,
    )
    session.add(h3)
    r3 = HypothesisRevisionModel(
        revision_id=f"rev-{run_id}-3",
        hypothesis_id=h3.hypothesis_id,
        revision_number=1,
        formula="y = 0.5*x^2",
        parameters={"coefficients": [0.0, 0.0, 0.5], "degree": 2},
        complexity=3,
    )
    session.add(r3)
    ev3 = EvaluationResultModel(
        evaluation_id=f"ev-{run_id}-3",
        revision_id=r3.revision_id,
        run_id=run_id,
        status="completed",
        sample_count=4,
        dataset_version=5,
        metrics={"mae": 4.5, "rmse": 5.2, "r2": 0.10},
        held_out_metrics={"mae": 6.0, "rmse": 6.0, "r2": -0.5},
    )
    session.add(ev3)

    # Usage
    usage = UsageRecordModel(
        usage_id=f"use-{run_id}",
        run_id=run_id,
        queries_executed=5,
        evaluations_scheduled=3,
        evaluations_completed=3,
        estimated_cost_usd=0.0,
        live_accruing_cost_usd=None,
        settled_cost_usd=None,
        is_budget_exhausted=False,
    )
    session.add(usage)
    session.flush()
    return run


class TestScientificReportGeneration:
    def test_completed_run_produces_ranked_report(self, db_session):
        """A completed run produces a structured report ranking hypotheses by primary metric."""
        run = _populate_completed_run(db_session, "run-test-01")

        report_model, report = generate_report_service(db_session, run.run_id)

        assert report_model.status == "generated"
        assert report.run_summary.run_id == run.run_id
        assert report.run_summary.status == "completed"
        assert report.run_summary.is_partial is False
        assert len(report.ranked_hypotheses) == 3

        # Candidate 1 should be rank 1 (lowest held-out MAE)
        top = report.ranked_hypotheses[0]
        assert top.hypothesis_id == f"hyp-{run.run_id}-1"
        assert top.rank == 1
        assert top.held_out_score == 0.002
        assert top.status == "plausible"

        # Evidence should reference real stored observations
        assert len(top.supporting_evidence) > 0
        first_ev = top.supporting_evidence[0]
        assert first_ev.observation_id.startswith(f"obs-{run.run_id}")
        assert first_ev.observed_value is not None
        assert first_ev.predicted_value is not None

        # Next experiment recommendation
        assert report.next_experiment.recommended is True
        assert report.next_experiment.input_values is not None
        assert "Candidate query distinguishes" in report.next_experiment.justification

    def test_sparse_run_reports_only_available_candidates_without_fabrication(self, db_session):
        """If fewer than 3 valid hypotheses exist, report available candidates without fabricating."""
        run = RunModel(
            run_id="run-sparse-01",
            name="Sparse Run",
            domain="1d_polynomial",
            status="completed",
            execution_mode="local",
            sampling_strategy="active_disagreement",
        )
        db_session.add(run)

        # Only 1 hypothesis
        h1 = HypothesisModel(
            hypothesis_id="hyp-sparse-1",
            run_id=run.run_id,
            model_family="polynomial",
            current_revision=1,
        )
        db_session.add(h1)
        r1 = HypothesisRevisionModel(
            revision_id="rev-sparse-1",
            hypothesis_id=h1.hypothesis_id,
            revision_number=1,
            formula="y = 3.0*x",
            parameters={"coefficients": [0.0, 3.0]},
            complexity=2,
        )
        db_session.add(r1)
        ev1 = EvaluationResultModel(
            evaluation_id="ev-sparse-1",
            revision_id=r1.revision_id,
            run_id=run.run_id,
            status="completed",
            sample_count=2,
            dataset_version=2,
            metrics={"mae": 0.5},
            held_out_metrics=None,
        )
        db_session.add(ev1)
        db_session.flush()

        _, report = generate_report_service(db_session, run.run_id)

        # Must report exactly 1 candidate, never fabricates up to 3
        assert len(report.ranked_hypotheses) == 1
        assert report.ranked_hypotheses[0].hypothesis_id == "hyp-sparse-1"
        assert any("Sparse Candidate Pool" in lim for lim in report.limitations)
        # Cannot plan next resolving experiment with only 1 candidate
        assert report.next_experiment.recommended is False

    def test_missing_metrics_remain_explicitly_unavailable(self, db_session):
        """Missing metrics remain null / unavailable and are never converted to zero."""
        run = RunModel(
            run_id="run-missing-01",
            name="Missing Metrics Run",
            domain="1d_polynomial",
            status="completed",
            execution_mode="nodus",
        )
        db_session.add(run)
        h1 = HypothesisModel(
            hypothesis_id="hyp-missing-1",
            run_id=run.run_id,
            model_family="polynomial",
        )
        db_session.add(h1)
        r1 = HypothesisRevisionModel(
            revision_id="rev-missing-1",
            hypothesis_id=h1.hypothesis_id,
            revision_number=1,
            formula="y = 1.0",
            parameters={"coefficients": [1.0]},
        )
        db_session.add(r1)
        # Evaluation with NO held-out metrics
        ev1 = EvaluationResultModel(
            evaluation_id="ev-missing-1",
            revision_id=r1.revision_id,
            run_id=run.run_id,
            status="completed",
            sample_count=2,
            dataset_version=2,
            metrics={"mae": 1.5},
            held_out_metrics=None,
        )
        db_session.add(ev1)
        # Usage with missing provider cost
        usage = UsageRecordModel(
            usage_id="use-missing-1",
            run_id=run.run_id,
            queries_executed=2,
            evaluations_scheduled=1,
            evaluations_completed=1,
            estimated_cost_usd=None,
            live_accruing_cost_usd=None,
            settled_cost_usd=None,
        )
        db_session.add(usage)
        db_session.flush()

        _, report = generate_report_service(db_session, run.run_id)

        cand = report.ranked_hypotheses[0]
        # Held-out score is None, not 0.0
        assert cand.held_out_score is None

        # Cost state is UNAVAILABLE, not $0.00
        cost = report.budget_and_execution.cost
        assert cost["state"] == "unavailable"
        assert cost["settled_cost_usd"] is None
        assert cost["estimated_cost_usd"] is None

    def test_budget_exhausted_or_stopped_run_is_labelled_partial(self, db_session):
        """Incomplete or budget-exhausted runs are clearly flagged as partial discovery."""
        run = RunModel(
            run_id="run-budget-stop-01",
            name="Budget Stopped Run",
            domain="1d_polynomial",
            status="budget_exhausted",
            execution_mode="local",
            stop_reason="Oracle query budget exhausted (limit: 5)",
        )
        db_session.add(run)
        db_session.flush()

        _, report = generate_report_service(db_session, run.run_id)

        assert report.run_summary.is_partial is True
        assert report.run_summary.status == "budget_exhausted"
        assert any("Partial Run Notice" in lim for lim in report.limitations)

    def test_failed_evaluation_is_identified_and_not_presented_as_valid(self, db_session):
        """A failed evaluation is categorized into evaluation_failed tier and not presented as favorable."""
        run = RunModel(
            run_id="run-failed-eval-01",
            name="Failed Eval Run",
            domain="1d_polynomial",
            status="completed",
            execution_mode="local",
        )
        db_session.add(run)

        h1 = HypothesisModel(
            hypothesis_id="hyp-fail-1",
            run_id=run.run_id,
            model_family="polynomial",
        )
        db_session.add(h1)
        r1 = HypothesisRevisionModel(
            revision_id="rev-fail-1",
            hypothesis_id=h1.hypothesis_id,
            revision_number=1,
            formula="y = invalid",
            parameters={},
        )
        db_session.add(r1)
        ev1 = EvaluationResultModel(
            evaluation_id="ev-fail-1",
            revision_id=r1.revision_id,
            run_id=run.run_id,
            status="failed",
            error_message="Matrix inversion singular: numerical divergence",
            metrics={},
            held_out_metrics=None,
        )
        db_session.add(ev1)
        db_session.flush()

        _, report = generate_report_service(db_session, run.run_id)

        assert len(report.ranked_hypotheses) == 1
        failed_cand = report.ranked_hypotheses[0]
        assert failed_cand.evaluation_tier == "evaluation_failed"
        assert failed_cand.status == "unevaluable"
        assert failed_cand.held_out_score is None
        assert any("Evaluation failed" in lim for lim in failed_cand.uncertainty_and_limitations)


class TestReportExports:
    def test_json_and_markdown_exports_preserve_content_and_metadata(self, db_session):
        """JSON and Markdown exports contain all required sections and safe values."""
        run = _populate_completed_run(db_session, "run-export-01")

        _, report = generate_report_service(db_session, run.run_id)

        # 1. JSON Export
        json_str = export_report_as_json(report)
        parsed = json.loads(json_str)
        assert parsed["run_id"] == run.run_id
        assert parsed["schema_version"] == "1.0.0"
        assert "run_summary" in parsed
        assert "ranked_hypotheses" in parsed
        assert "evaluation_methodology" in parsed
        assert "budget_and_execution" in parsed
        assert "next_experiment" in parsed
        assert len(parsed["ranked_hypotheses"]) == 3

        # 2. Markdown Export
        md_str = export_report_as_markdown(report)
        assert f"# Scientific Discovery Report: {run.name}" in md_str
        assert "## 1. Run Summary" in md_str
        assert "## 2. Discovery Process" in md_str
        assert "## 3. Evaluation Methodology" in md_str
        assert "## 4. Top Ranked Hypotheses" in md_str
        assert "## 5. Evidence & Provenance" in md_str
        assert "## 7. Budget & Resource Usage" in md_str
        assert "## 8. Next Experiment Recommendation" in md_str
        assert "## 10. Conclusion" in md_str
        # Verify no confidence percentage claim was fabricated
        assert "% certain" not in md_str
        assert "% confidence" not in md_str


class TestReportHistoricalIntegrityAndIdempotency:
    def test_idempotent_report_generation_preserves_historical_report(self, db_session):
        """Subsequent generate_report_service call returns existing generated report without overwriting."""
        run = _populate_completed_run(db_session, "run-idemp-01")

        model_1, report_1 = generate_report_service(db_session, run.run_id)
        original_id = model_1.report_id

        # Subsequent call without force_recompute returns existing report
        model_2, report_2 = generate_report_service(db_session, run.run_id, force_recompute=False)
        assert model_2.report_id == original_id
        assert report_2.report_id == original_id

        # Verify only 1 report row exists in DB
        repo = ClarityRepository(db_session)
        reports = list(repo.session.query(ReportModel).filter_by(run_id=run.run_id).all())
        assert len(reports) == 1


class TestReportApiEndpoints:
    def test_get_report_pending_then_post_generates_and_exports(self, client, app):
        """Integration test for GET report, POST report generation, and GET report/export."""
        res_run = client.post("/api/v1/runs", json={"name": "API Report Run"})
        run_id = res_run.get_json()["run"]["run_id"]

        # 1. GET /report before generation returns status pending
        res_get_pending = client.get(f"/api/v1/runs/{run_id}/report")
        assert res_get_pending.status_code == 200
        data_pending = res_get_pending.get_json()
        assert data_pending["status"] == "pending"
        assert data_pending["report"] is None

        # 2. POST /report triggers report generation
        res_post = client.post(f"/api/v1/runs/{run_id}/report", json={})
        assert res_post.status_code == 201
        data_post = res_post.get_json()
        assert "report" in data_post
        rep_id = data_post["report"]["report_id"]

        # 3. GET /report now returns the generated report
        res_get = client.get(f"/api/v1/runs/{run_id}/report")
        assert res_get.status_code == 200
        assert res_get.get_json()["report"]["report_id"] == rep_id
        assert res_get.get_json()["report"]["status"] == "generated"

        # 4. GET /report/export?format=json returns machine-readable JSON
        res_exp_json = client.get(f"/api/v1/runs/{run_id}/report/export?format=json")
        assert res_exp_json.status_code == 200
        assert "application/json" in res_exp_json.headers["Content-Type"]
        json_data = json.loads(res_exp_json.data.decode("utf-8"))
        assert json_data["run_id"] == run_id

        # 5. GET /report/export?format=markdown returns Markdown
        res_exp_md = client.get(f"/api/v1/runs/{run_id}/report/export?format=markdown")
        assert res_exp_md.status_code == 200
        assert "text/markdown" in res_exp_md.headers["Content-Type"]
        md_text = res_exp_md.data.decode("utf-8")
        assert "# Scientific Discovery Report" in md_text

    def test_report_endpoints_return_404_for_unknown_run(self, client):
        """Endpoints return 404 when requested for non-existent run."""
        res_get = client.get("/api/v1/runs/non-existent-run/report")
        assert res_get.status_code == 404

        res_post = client.post("/api/v1/runs/non-existent-run/report", json={})
        assert res_post.status_code == 404

        res_exp = client.get("/api/v1/runs/non-existent-run/report/export")
        assert res_exp.status_code == 404


class TestNextExperimentCalculation:
    """Item 1: Next-experiment recommendation is calculated rather than fabricated."""

    def test_candidates_agree_at_x5_yields_no_recommendation(self):
        """When leading candidates agree at x=5, no distinguishing recommendation is available."""
        # Candidate A: y = 2x + 1 -> at x=5, y=11
        # Candidate B: y = x + 6  -> at x=5, y=11
        cand_a = HypothesisRepresentation(
            hypothesis_id="hyp-agree-a",
            model_family="polynomial",
            formula="y = 2.0*x + 1.0",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [1.0, 2.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        cand_b = HypothesisRepresentation(
            hypothesis_id="hyp-agree-b",
            model_family="polynomial",
            formula="y = 1.0*x + 6.0",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [6.0, 1.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        eval_a = EvaluationResult(
            evaluation_id="ev-agree-a",
            hypothesis_id=cand_a.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.05, "rmse": 0.05, "r2": 0.95},
            held_out_metrics=None,
            sample_count=5,
            model_family="polynomial",
            fitted_specification=cand_a.parameters,
        )
        eval_b = EvaluationResult(
            evaluation_id="ev-agree-b",
            hypothesis_id=cand_b.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.06, "rmse": 0.06, "r2": 0.94},
            held_out_metrics=None,
            sample_count=5,
            model_family="polynomial",
            fitted_specification=cand_b.parameters,
        )
        obs = [
            Observation(
                observation_id="obs-1",
                input_id="inp-1",
                input_values={"x": 1.0},
                output_values={"y": 3.0},
                status=ObservationStatus.SUCCESS,
                sequence_number=1,
                is_held_out=False,
            )
        ]

        report = generate_scientific_report(
            run_id="run-agree-x5",
            run_name="Candidates Agree at x=5",
            domain="1d_polynomial",
            execution_mode="local",
            sampling_strategy="active_disagreement",
            run_status="completed",
            stop_reason=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            budget_limits=None,
            usage_data=None,
            candidates=[cand_a, cand_b],
            evaluations_by_hypothesis={cand_a.hypothesis_id: eval_a, cand_b.hypothesis_id: eval_b},
            observations=obs,
        )

        assert report.next_experiment.recommended is False
        assert report.next_experiment.predicted_disagreement is None
        assert report.next_experiment.input_values is None
        assert "do not exhibit significant predicted disagreement" in report.next_experiment.justification

    def test_no_observations_yields_no_recommendation(self):
        """When no observations or candidate inputs exist, no recommendation is made."""
        cand_a = HypothesisRepresentation(
            hypothesis_id="hyp-no-obs-a",
            model_family="polynomial",
            formula="y = 2.0*x + 1.0",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [1.0, 2.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        cand_b = HypothesisRepresentation(
            hypothesis_id="hyp-no-obs-b",
            model_family="polynomial",
            formula="y = 3.0*x + 1.0",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [1.0, 3.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        eval_a = EvaluationResult(
            evaluation_id="ev-no-obs-a",
            hypothesis_id=cand_a.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.05, "rmse": 0.05, "r2": 0.95},
            held_out_metrics=None,
            sample_count=0,
            model_family="polynomial",
            fitted_specification=cand_a.parameters,
        )
        eval_b = EvaluationResult(
            evaluation_id="ev-no-obs-b",
            hypothesis_id=cand_b.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.06, "rmse": 0.06, "r2": 0.94},
            held_out_metrics=None,
            sample_count=0,
            model_family="polynomial",
            fitted_specification=cand_b.parameters,
        )

        report = generate_scientific_report(
            run_id="run-no-obs",
            run_name="No Observations Run",
            domain="1d_polynomial",
            execution_mode="local",
            sampling_strategy="active_disagreement",
            run_status="completed",
            stop_reason=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            budget_limits=None,
            usage_data=None,
            candidates=[cand_a, cand_b],
            evaluations_by_hypothesis={cand_a.hypothesis_id: eval_a, cand_b.hypothesis_id: eval_b},
            observations=[],
            candidate_experiments=None,
        )

        assert report.next_experiment.recommended is False
        assert report.next_experiment.predicted_disagreement is None
        assert report.next_experiment.input_values is None
        assert "no observations or candidate inputs exist" in report.next_experiment.justification

    def test_candidate_experiment_verified_predicted_disagreement(self):
        """Candidate experiments are scored and chosen by verified calculated disagreement."""
        cand_a = HypothesisRepresentation(
            hypothesis_id="hyp-exp-a",
            model_family="polynomial",
            formula="y = 2.0*x",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [0.0, 2.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        cand_b = HypothesisRepresentation(
            hypothesis_id="hyp-exp-b",
            model_family="polynomial",
            formula="y = 1.0*x",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [0.0, 1.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        eval_a = EvaluationResult(
            evaluation_id="ev-exp-a",
            hypothesis_id=cand_a.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.05, "rmse": 0.05, "r2": 0.95},
            held_out_metrics=None,
            sample_count=2,
            model_family="polynomial",
            fitted_specification=cand_a.parameters,
        )
        eval_b = EvaluationResult(
            evaluation_id="ev-exp-b",
            hypothesis_id=cand_b.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.06, "rmse": 0.06, "r2": 0.94},
            held_out_metrics=None,
            sample_count=2,
            model_family="polynomial",
            fitted_specification=cand_b.parameters,
        )
        candidate_exps = [
            {
                "experiment_id": "exp-calc-10",
                "proposed_input": {"x": 10.0},
                "selection_rationale": {"strategy": "active_disagreement"},
                "execution_status": "proposed",
            }
        ]

        report = generate_scientific_report(
            run_id="run-exp-calc",
            run_name="Calculated Disagreement Run",
            domain="1d_polynomial",
            execution_mode="local",
            sampling_strategy="active_disagreement",
            run_status="completed",
            stop_reason=None,
            created_at=None,
            started_at=None,
            completed_at=None,
            budget_limits=None,
            usage_data=None,
            candidates=[cand_a, cand_b],
            evaluations_by_hypothesis={cand_a.hypothesis_id: eval_a, cand_b.hypothesis_id: eval_b},
            observations=[],
            candidate_experiments=candidate_exps,
        )

        assert report.next_experiment.recommended is True
        # At x=10, cand_a predicts 20, cand_b predicts 10 -> disagreement is 10.0
        assert report.next_experiment.predicted_disagreement == pytest.approx(10.0)
        assert report.next_experiment.input_values == {"x": 10.0}
        assert report.next_experiment.experiment_id == "exp-calc-10"


class TestReportEvaluationProvenanceBinding:
    """Item 2: Deterministic revision-to-evaluation provenance binding."""

    def test_evaluation_provenance_strictly_matches_evaluated_revision(self, db_session):
        """Selected evaluation must bind to the exact revision that was evaluated."""
        run = RunModel(
            run_id="run-prov-01",
            name="Provenance Test Run",
            domain="1d_polynomial",
            status="completed",
            execution_mode="local",
            sampling_strategy="active_disagreement",
            stop_reason="target_accuracy_reached",
        )
        db_session.add(run)

        # Hypothesis with 2 revisions:
        # Revision 1: y = 1.0*x + 1.0 (older, dataset_version=2, mae=0.5)
        # Revision 2: y = 3.0*x + 2.0 (latest, dataset_version=5, mae=0.01)
        hyp = HypothesisModel(
            hypothesis_id="hyp-prov-1",
            run_id=run.run_id,
            model_family="polynomial",
            current_revision=2,
        )
        db_session.add(hyp)

        r1 = HypothesisRevisionModel(
            revision_id="rev-prov-1",
            hypothesis_id=hyp.hypothesis_id,
            revision_number=1,
            formula="y = 1.0*x + 1.0",
            parameters={"coefficients": [1.0, 1.0], "degree": 1},
            complexity=2,
        )
        db_session.add(r1)
        ev1 = EvaluationResultModel(
            evaluation_id="ev-prov-rev1",
            revision_id=r1.revision_id,
            run_id=run.run_id,
            status="completed",
            sample_count=2,
            dataset_version=2,
            metrics={"mae": 0.5, "rmse": 0.5, "r2": 0.5},
            held_out_metrics=None,
        )
        db_session.add(ev1)

        r2 = HypothesisRevisionModel(
            revision_id="rev-prov-2",
            hypothesis_id=hyp.hypothesis_id,
            revision_number=2,
            formula="y = 3.0*x + 2.0",
            parameters={"coefficients": [2.0, 3.0], "degree": 1},
            complexity=2,
        )
        db_session.add(r2)
        ev2 = EvaluationResultModel(
            evaluation_id="ev-prov-rev2",
            revision_id=r2.revision_id,
            run_id=run.run_id,
            status="completed",
            sample_count=5,
            dataset_version=5,
            metrics={"mae": 0.01, "rmse": 0.01, "r2": 0.99},
            held_out_metrics=None,
        )
        db_session.add(ev2)
        db_session.flush()

        model, report = generate_report_service(db_session, run.run_id)
        assert len(report.ranked_hypotheses) == 1
        rep_hyp = report.ranked_hypotheses[0]

        # Verify that revision, formula, parameters, evaluation_id, and dataset_version all agree
        assert rep_hyp.revision_number == 2
        assert rep_hyp.formula == "y = 3.0*x + 2.0"
        assert rep_hyp.evaluation_id == "ev-prov-rev2"
        assert list(rep_hyp.parameters["coefficients"]) == [2.0, 3.0]
        assert rep_hyp.dataset_version == 5
        assert rep_hyp.training_score == pytest.approx(0.01)


class TestEvidenceCollectionErrorHandling:
    """Item 3: Evidence collection failure is not silently swallowed."""

    def test_evidence_collection_failure_records_explicit_limitation(self):
        """When collect_evidence raises an error, an explicit limitation is recorded and logged safely."""
        cand = HypothesisRepresentation(
            hypothesis_id="hyp-ev-err",
            model_family="polynomial",
            formula="y = 2.0*x + 1.0",
            parameters={"family": "polynomial", "degree": 1, "coefficients": [1.0, 2.0]},
            complexity=2,
            revision=1,
            status=HypothesisStatus.EVALUATED,
        )
        eval_res = EvaluationResult(
            evaluation_id="ev-err-1",
            hypothesis_id=cand.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics={"mae": 0.05, "rmse": 0.05, "r2": 0.95},
            held_out_metrics=None,
            sample_count=2,
            model_family="polynomial",
            fitted_specification=cand.parameters,
        )
        obs = [
            Observation(
                observation_id="obs-err-1",
                input_id="inp-1",
                input_values={"x": 1.0},
                output_values={"y": 3.0},
                status=ObservationStatus.SUCCESS,
                sequence_number=1,
                is_held_out=False,
            )
        ]

        with patch("app.domain.report_generator.collect_evidence", side_effect=RuntimeError("Residual stream auth_token=SECRET123 failed")):
            report = generate_scientific_report(
                run_id="run-ev-err",
                run_name="Evidence Error Run",
                domain="1d_polynomial",
                execution_mode="local",
                sampling_strategy="active_disagreement",
                run_status="completed",
                stop_reason=None,
                created_at=None,
                started_at=None,
                completed_at=None,
                budget_limits=None,
                usage_data=None,
                candidates=[cand],
                evaluations_by_hypothesis={cand.hypothesis_id: eval_res},
                observations=obs,
            )

        rep_hyp = report.ranked_hypotheses[0]
        # Must record sanitized limitation
        assert any("Evidence generation failed" in lim for lim in rep_hyp.uncertainty_and_limitations)
        assert any("Evidence Generation Notice" in lim for lim in report.limitations)
        # Verify secret was sanitized
        for lim in report.limitations:
            assert "SECRET123" not in lim


class TestReportIdempotencyAndDiskArtifactResilience:
    """Item 4: Historical report idempotency and database persistence."""

    def test_missing_disk_artifact_uses_database_canonical_report(self, db_session):
        """When disk artifact is deleted, report is reconstituted from database without replacing history."""
        run = _populate_completed_run(db_session, "run-resilient-01")
        model_1, rep_1 = generate_report_service(db_session, run.run_id)
        initial_report_id = model_1.report_id
        initial_timestamp = model_1.generated_at

        # Verify artifact file exists on disk
        artifact_path = os.path.join("reports", f"{run.run_id}_{initial_report_id}.json")
        assert os.path.exists(artifact_path)
        # Delete disk artifact to simulate missing/unreadable file
        os.remove(artifact_path)
        assert not os.path.exists(artifact_path)

        # Call generate_report_service again - must NOT replace history!
        model_2, rep_2 = generate_report_service(db_session, run.run_id)
        assert model_2.report_id == initial_report_id
        assert model_2.generated_at == initial_timestamp
        assert rep_2.report_id == initial_report_id

    def test_missing_disk_artifact_and_db_json_fails_explicitly(self, db_session):
        """When neither disk artifact nor database payload is available, fail explicitly."""
        run = _populate_completed_run(db_session, "run-corrupted-01")
        # Save a corrupted report model with no JSON and non-existent artifact
        corrupt_report = ReportModel(
            report_id="rep-corrupt-01",
            run_id=run.run_id,
            status="generated",
            ranked_candidates=[],
            artifact_uri="reports/does_not_exist_9999.json",
            report_json=None,
        )
        db_session.add(corrupt_report)
        db_session.flush()

        with pytest.raises(ReportNotAvailableError, match="cannot be loaded"):
            generate_report_service(db_session, run.run_id)


class TestReportArtifactPathSafety:
    """Item 5: Path safety and path traversal rejection."""

    def test_save_report_artifact_rejects_path_traversal(self):
        """_save_report_artifact rejects attempts to traverse out of the report storage directory."""
        with pytest.raises(ValueError, match="Path traversal detected|Invalid run_id"):
            _save_report_artifact("../../etc/passwd", "rep-evil-1", "{}")

        with pytest.raises(ValueError, match="Path traversal detected|Invalid run_id"):
            _save_report_artifact("run-1/sub", "rep-evil-1", "{}")

        with pytest.raises(ValueError, match="Path traversal detected|Invalid run_id"):
            _save_report_artifact("run-1\\sub", "rep-evil-1", "{}")

        with pytest.raises(ValueError, match="Path traversal detected|Invalid report_id"):
            _save_report_artifact("run-safe-01", "../evil-rep", "{}")

    def test_load_report_artifact_safely_rejects_traversal(self):
        """_load_report_artifact returns None on path traversal inputs."""
        assert _load_report_artifact("../../etc/passwd") is None
        assert _load_report_artifact("reports/../../../etc/passwd") is None

