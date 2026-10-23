"""Flask API routes for discovery runs, hypotheses, observations, and metrics.

Endpoints:
- POST   /api/runs                                  - Create a new discovery run
- GET    /api/runs                                  - List discovery runs with pagination
- GET    /api/runs/<run_id>                         - Retrieve a specific discovery run
- POST   /api/runs/<run_id>/cancel                  - Cancel an active discovery run
- GET    /api/runs/<run_id>/hypotheses              - List hypotheses with revisions and evaluations
- POST   /api/runs/<run_id>/hypotheses              - Propose a researcher candidate hypothesis
- PATCH  /api/runs/<run_id>/hypotheses/<hyp_id>     - Update researcher flags (pin/exclude)
- GET    /api/runs/<run_id>/observations            - List observations for a run
- GET    /api/runs/<run_id>/metrics                 - Retrieve usage, budget, and cost metrics
- GET    /api/runs/<run_id>/report                  - Retrieve scientific summary report
- GET    /api/runs/<run_id>/benchmarks              - Retrieve benchmark results for a run
"""

import uuid
from typing import Any, Dict, List, Optional
from flask import Blueprint, Response, current_app, jsonify, request

from ..services.report_service import export_report_service, generate_report_service

from ..adapters.persistence.database import get_db_manager
from ..adapters.persistence.models import (
    AuditEventModel,
    BenchmarkResultModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from ..adapters.persistence.repository import ClarityRepository

runs_bp = Blueprint("runs", __name__)


# ==============================================================================
# Helper & Serialization Functions
# ==============================================================================

def error_response(message: str, status_code: int = 400, error: Optional[str] = None):
    """Return a consistent structured JSON error response."""
    if error is None:
        if status_code == 400:
            error = "Bad Request"
        elif status_code == 404:
            error = "Not Found"
        elif status_code == 409:
            error = "Conflict"
        elif status_code == 422:
            error = "Unprocessable Entity"
        else:
            error = "Error"
    return jsonify({
        "error": error,
        "message": message,
        "status_code": status_code,
    }), status_code


def serialize_run(run: RunModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for RunModel."""
    return {
        "run_id": run.run_id,
        "name": run.name,
        "domain": run.domain,
        "status": run.status,
        "execution_mode": run.execution_mode,
        "sampling_strategy": run.sampling_strategy,
        "stop_reason": run.stop_reason,
        "max_queries": run.max_queries,
        "max_evaluations": run.max_evaluations,
        "max_runtime_seconds": run.max_runtime_seconds,
        "spend_ceiling_usd": run.spend_ceiling_usd,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def serialize_revision(rev: HypothesisRevisionModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for HypothesisRevisionModel."""
    return {
        "revision_id": rev.revision_id,
        "revision_number": rev.revision_number,
        "model_family": getattr(rev, "model_family", None),
        "formula": rev.formula,
        "parameters": rev.parameters,
        "complexity": rev.complexity,
        "creation_source": rev.creation_source,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


def serialize_evaluation(eval_res: EvaluationResultModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for EvaluationResultModel."""
    return {
        "evaluation_id": eval_res.evaluation_id,
        "status": eval_res.status,
        "sample_count": eval_res.sample_count,
        "dataset_version": eval_res.dataset_version,
        "metrics": eval_res.metrics,
        "held_out_metrics": eval_res.held_out_metrics,
        "error_message": eval_res.error_message,
        "evaluated_at": eval_res.evaluated_at.isoformat() if eval_res.evaluated_at else None,
    }


def serialize_hypothesis(
    hyp: HypothesisModel,
    repo: Optional[ClarityRepository] = None,
) -> Dict[str, Any]:
    """Explicit allowlist serialization for HypothesisModel including revisions and latest evaluation."""
    revisions = sorted(hyp.revisions, key=lambda r: r.revision_number) if hyp.revisions else []
    latest_rev = revisions[-1] if revisions else None

    latest_eval = None
    if latest_rev and repo:
        evals = repo.list_evaluations_for_revision(latest_rev.revision_id)
        if evals:
            latest_eval = serialize_evaluation(evals[0])

    return {
        "hypothesis_id": hyp.hypothesis_id,
        "run_id": hyp.run_id,
        "model_family": hyp.model_family,
        "current_revision": hyp.current_revision,
        "is_pinned": hyp.is_pinned,
        "is_excluded": hyp.is_excluded,
        "created_at": hyp.created_at.isoformat() if hyp.created_at else None,
        "updated_at": hyp.updated_at.isoformat() if hyp.updated_at else None,
        "latest_revision": serialize_revision(latest_rev) if latest_rev else None,
        "latest_evaluation": latest_eval,
        "revisions": [serialize_revision(r) for r in revisions],
    }


def serialize_observation(obs: ObservationModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for ObservationModel."""
    return {
        "observation_id": obs.observation_id,
        "run_id": obs.run_id,
        "input_id": obs.input_id,
        "input_values": obs.input_values,
        "output_values": obs.output_values,
        "status": obs.status,
        "sequence_number": obs.sequence_number,
        "is_held_out": obs.is_held_out,
        "error_code": obs.error_code,
        "error_message": obs.error_message,
        "noise_metadata": obs.noise_metadata,
        "created_at": obs.created_at.isoformat() if obs.created_at else None,
    }


def serialize_report(report: ReportModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for ReportModel."""
    return {
        "report_id": report.report_id,
        "run_id": report.run_id,
        "status": report.status,
        "ranked_candidates": report.ranked_candidates,
        "stop_reason": report.stop_reason,
        "limitations": report.limitations,
        "artifact_uri": report.artifact_uri,
        "generated_at": report.generated_at.isoformat() if report.generated_at else None,
    }


def serialize_benchmark(b: BenchmarkResultModel) -> Dict[str, Any]:
    """Explicit allowlist serialization for BenchmarkResultModel."""
    return {
        "benchmark_id": b.benchmark_id,
        "run_id": b.run_id,
        "benchmark_name": b.benchmark_name,
        "target_label": b.target_label,
        "hit_at_1": b.hit_at_1,
        "hit_at_3": b.hit_at_3,
        "hit_at_5": b.hit_at_5,
        "reciprocal_rank": b.reciprocal_rank,
        "best_candidate_id": b.best_candidate_id,
        "queries_to_target": b.queries_to_target,
        "held_out_mae": b.held_out_mae,
        "held_out_rmse": b.held_out_rmse,
        "baseline_type": b.baseline_type,
        "efficiency_gain": b.efficiency_gain,
        "executed_at": b.executed_at.isoformat() if b.executed_at else None,
    }


def get_db():
    """Retrieve the database manager for the current application configuration."""
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


# ==============================================================================
# Run Routes
# ==============================================================================

@runs_bp.route("/runs", methods=["POST"])
def create_run_endpoint():
    """Create a new discovery run."""
    if not request.is_json:
        return error_response("Payload must be valid JSON with Content-Type application/json.", 400)

    data = request.get_json()
    if not isinstance(data, dict):
        return error_response("JSON body must be an object.", 400)

    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return error_response("Field 'name' is required and must be a non-empty string.", 400)

    domain = data.get("domain", "default")
    if not isinstance(domain, str) or not domain.strip():
        return error_response("Field 'domain' must be a non-empty string.", 400)

    execution_mode = data.get("execution_mode", "local")
    if execution_mode not in {"local", "nodus_remote", "mock"}:
        return error_response("Field 'execution_mode' must be one of: local, nodus_remote, mock.", 400)

    sampling_strategy = data.get("sampling_strategy", "active_disagreement")
    if not isinstance(sampling_strategy, str) or not sampling_strategy.strip():
        return error_response("Field 'sampling_strategy' must be a non-empty string.", 400)

    # Validate numeric limits
    max_queries = data.get("max_queries")
    if max_queries is not None:
        if not isinstance(max_queries, int) or isinstance(max_queries, bool) or max_queries <= 0:
            return error_response("Field 'max_queries' must be a positive integer.", 400)

    max_evaluations = data.get("max_evaluations")
    if max_evaluations is not None:
        if not isinstance(max_evaluations, int) or isinstance(max_evaluations, bool) or max_evaluations <= 0:
            return error_response("Field 'max_evaluations' must be a positive integer.", 400)

    max_runtime_seconds = data.get("max_runtime_seconds")
    if max_runtime_seconds is not None:
        if not isinstance(max_runtime_seconds, int) or isinstance(max_runtime_seconds, bool) or max_runtime_seconds <= 0:
            return error_response("Field 'max_runtime_seconds' must be a positive integer.", 400)

    spend_ceiling_usd = data.get("spend_ceiling_usd")
    if spend_ceiling_usd is not None:
        if not isinstance(spend_ceiling_usd, (int, float)) or isinstance(spend_ceiling_usd, bool) or spend_ceiling_usd < 0:
            return error_response("Field 'spend_ceiling_usd' must be a non-negative number.", 400)
        spend_ceiling_usd = float(spend_ceiling_usd)

    run_id = data.get("run_id")
    if run_id:
        if not isinstance(run_id, str) or not run_id.strip():
            return error_response("Field 'run_id' must be a non-empty string if provided.", 400)
        run_id = run_id.strip()
    else:
        run_id = f"run-{uuid.uuid4().hex[:12]}"

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        if repo.get_run(run_id):
            return error_response(f"Run with ID '{run_id}' already exists.", 409)

        run = RunModel(
            run_id=run_id,
            name=name.strip(),
            domain=domain.strip(),
            status="pending",
            execution_mode=execution_mode,
            sampling_strategy=sampling_strategy.strip(),
            max_queries=max_queries,
            max_evaluations=max_evaluations,
            max_runtime_seconds=max_runtime_seconds,
            spend_ceiling_usd=spend_ceiling_usd,
        )
        repo.create_run(run)

        # Initialize base usage tracking record
        usage = UsageRecordModel(
            usage_id=f"use-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            queries_executed=0,
            evaluations_scheduled=0,
            evaluations_completed=0,
            estimated_cost_usd=0.0,
            live_accruing_cost_usd=0.0,
            settled_cost_usd=None,
            is_budget_exhausted=False,
        )
        repo.record_usage(usage)

        # Log audit event for run creation
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                event_type="RUN_CREATED",
                actor="researcher",
                metadata_json={"name": run.name, "domain": run.domain},
            )
        )

        serialized = serialize_run(run)

    return jsonify({"run": serialized}), 201


@runs_bp.route("/runs", methods=["GET"])
def list_runs_endpoint():
    """List discovery runs with pagination."""
    limit_str = request.args.get("limit", "50")
    offset_str = request.args.get("offset", "0")

    try:
        limit = int(limit_str)
        offset = int(offset_str)
        if limit <= 0 or offset < 0:
            raise ValueError()
        limit = min(limit, 100)
    except ValueError:
        return error_response("Query parameters 'limit' and 'offset' must be valid non-negative integers.", 400)

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        runs = repo.list_runs(limit=limit, offset=offset)
        serialized_runs = [serialize_run(r) for r in runs]

    return jsonify({
        "runs": serialized_runs,
        "count": len(serialized_runs),
        "limit": limit,
        "offset": offset,
    }), 200


@runs_bp.route("/runs/<run_id>", methods=["GET"])
def get_run_endpoint(run_id: str):
    """Retrieve details of a specific discovery run."""
    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)
        serialized = serialize_run(run)

    return jsonify({"run": serialized}), 200


@runs_bp.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run_endpoint(run_id: str):
    """Cancel an active or pending discovery run."""
    reason = "Cancelled by user"
    if request.is_json and request.get_json():
        reason = request.get_json().get("reason", reason)
        if not isinstance(reason, str):
            reason = "Cancelled by user"

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        terminal_states = {"completed", "cancelled", "failed", "budget_exhausted"}
        if run.status in terminal_states:
            return error_response(
                f"Run '{run_id}' cannot be cancelled because it is already in terminal state '{run.status}'.",
                409,
            )

        run.status = "cancelled"
        run.stop_reason = reason.strip()

        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                event_type="RUN_CANCELLED",
                actor="researcher",
                metadata_json={"stop_reason": run.stop_reason},
            )
        )

        serialized = serialize_run(run)

    return jsonify({"run": serialized, "message": "Run cancelled successfully."}), 200


# ==============================================================================
# Hypothesis Routes
# ==============================================================================

@runs_bp.route("/runs/<run_id>/hypotheses", methods=["GET"])
def list_hypotheses_endpoint(run_id: str):
    """Retrieve candidate hypotheses for a run, with revisions and latest evaluation."""
    include_excluded_str = request.args.get("include_excluded", "true").lower()
    include_excluded = include_excluded_str not in {"false", "0", "no"}

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        hypotheses = repo.list_hypotheses_for_run(run_id, include_excluded=include_excluded)
        serialized_hypotheses = [serialize_hypothesis(h, repo) for h in hypotheses]

    return jsonify({
        "hypotheses": serialized_hypotheses,
        "count": len(serialized_hypotheses),
    }), 200


@runs_bp.route("/runs/<run_id>/hypotheses", methods=["POST"])
def create_hypothesis_endpoint(run_id: str):
    """Propose a researcher hypothesis for a discovery run."""
    if not request.is_json:
        return error_response("Payload must be valid JSON with Content-Type application/json.", 400)

    data = request.get_json()
    if not isinstance(data, dict):
        return error_response("JSON body must be an object.", 400)

    model_family = data.get("model_family")
    if not model_family or not isinstance(model_family, str) or not model_family.strip():
        return error_response("Field 'model_family' is required and must be a non-empty string.", 400)

    formula = data.get("formula")
    if not formula or not isinstance(formula, str) or not formula.strip():
        return error_response("Field 'formula' is required and must be a non-empty string.", 400)

    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        return error_response("Field 'parameters' must be a dictionary of coefficients/weights.", 400)

    complexity = data.get("complexity", 1)
    if not isinstance(complexity, int) or isinstance(complexity, bool) or complexity < 1:
        return error_response("Field 'complexity' must be an integer >= 1.", 400)

    hypothesis_id = data.get("hypothesis_id")
    if hypothesis_id:
        if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
            return error_response("Field 'hypothesis_id' must be a non-empty string if provided.", 400)
        hypothesis_id = hypothesis_id.strip()
    else:
        hypothesis_id = f"hyp-{uuid.uuid4().hex[:12]}"

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        if repo.get_hypothesis(hypothesis_id):
            return error_response(f"Hypothesis '{hypothesis_id}' already exists.", 409)

        hyp = HypothesisModel(
            hypothesis_id=hypothesis_id,
            run_id=run_id,
            model_family=model_family.strip(),
            current_revision=1,
            is_pinned=False,
            is_excluded=False,
        )
        rev = HypothesisRevisionModel(
            revision_id=f"rev-{uuid.uuid4().hex[:12]}",
            revision_number=1,
            formula=formula.strip(),
            parameters=parameters,
            complexity=complexity,
            # This route does not canonicalise the family; record what it is
            # actually evaluated with so the revision stays self-describing.
            model_family=model_family.strip(),
            creation_source="researcher",
        )
        repo.create_hypothesis_with_initial_revision(hyp, rev)

        # Log audit event for researcher proposal
        repo.record_audit_event(
            AuditEventModel(
                event_id=f"evt-{uuid.uuid4().hex[:12]}",
                run_id=run_id,
                event_type="HYPOTHESIS_PROPOSED",
                actor="researcher",
                metadata_json={
                    "hypothesis_id": hyp.hypothesis_id,
                    "formula": rev.formula,
                    "model_family": hyp.model_family,
                },
            )
        )

        serialized = serialize_hypothesis(hyp, repo)

    return jsonify({"hypothesis": serialized}), 201


@runs_bp.route("/runs/<run_id>/hypotheses/<hypothesis_id>", methods=["PATCH"])
def update_hypothesis_endpoint(run_id: str, hypothesis_id: str):
    """Update researcher interaction flags (is_pinned, is_excluded) non-destructively."""
    if not request.is_json:
        return error_response("Payload must be valid JSON with Content-Type application/json.", 400)

    data = request.get_json()
    if not isinstance(data, dict):
        return error_response("JSON body must be an object.", 400)

    has_pin = "is_pinned" in data
    has_exclude = "is_excluded" in data

    if not has_pin and not has_exclude:
        return error_response("Payload must specify at least one of 'is_pinned' or 'is_excluded'.", 400)

    is_pinned = data.get("is_pinned")
    if has_pin and not isinstance(is_pinned, bool):
        return error_response("Field 'is_pinned' must be a boolean.", 400)

    is_excluded = data.get("is_excluded")
    if has_exclude and not isinstance(is_excluded, bool):
        return error_response("Field 'is_excluded' must be a boolean.", 400)

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        hyp = repo.get_hypothesis(hypothesis_id)
        if not hyp or hyp.run_id != run_id:
            return error_response(f"Hypothesis '{hypothesis_id}' not found in run '{run_id}'.", 404)

        # Record audit events for changed flags
        if has_pin and hyp.is_pinned != is_pinned:
            event_type = "HYPOTHESIS_PINNED" if is_pinned else "HYPOTHESIS_UNPINNED"
            repo.record_audit_event(
                AuditEventModel(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    event_type=event_type,
                    actor="researcher",
                    metadata_json={"hypothesis_id": hypothesis_id, "is_pinned": is_pinned},
                )
            )

        if has_exclude and hyp.is_excluded != is_excluded:
            event_type = "HYPOTHESIS_EXCLUDED" if is_excluded else "HYPOTHESIS_RESTORED"
            repo.record_audit_event(
                AuditEventModel(
                    event_id=f"evt-{uuid.uuid4().hex[:12]}",
                    run_id=run_id,
                    event_type=event_type,
                    actor="researcher",
                    metadata_json={"hypothesis_id": hypothesis_id, "is_excluded": is_excluded},
                )
            )

        repo.update_hypothesis_flags(
            hypothesis_id,
            is_pinned=is_pinned if has_pin else None,
            is_excluded=is_excluded if has_exclude else None,
        )

        serialized = serialize_hypothesis(hyp, repo)

    return jsonify({"hypothesis": serialized}), 200


# ==============================================================================
# Observation Routes
# ==============================================================================

@runs_bp.route("/runs/<run_id>/observations", methods=["GET"])
def list_observations_endpoint(run_id: str):
    """Retrieve observations recorded for a run."""
    only_training_str = request.args.get("only_training", "false").lower()
    only_training = only_training_str in {"true", "1", "yes"}

    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        limit_arg = request.args.get("limit")
        offset_arg = request.args.get("offset")
        try:
            limit = int(limit_arg) if limit_arg is not None else None
            offset = int(offset_arg) if offset_arg is not None else None
        except (ValueError, TypeError):
            return error_response("Query parameters 'limit' and 'offset' must be integers.", 400)

        observations = repo.list_observations_for_run(
            run_id, only_training=only_training, limit=limit, offset=offset
        )
        serialized_observations = [serialize_observation(o) for o in observations]

    return jsonify({
        "observations": serialized_observations,
        "count": len(serialized_observations),
    }), 200


# ==============================================================================
# Metrics & Budget Routes
# ==============================================================================

@runs_bp.route("/runs/<run_id>/metrics", methods=["GET"])
def get_metrics_endpoint(run_id: str):
    """Retrieve execution metrics, budget progression, and costs for a discovery run."""
    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        latest_usage = repo.get_latest_usage_for_run(run_id)

        metrics_payload = {
            "run_id": run.run_id,
            "status": run.status,
            "stop_reason": run.stop_reason,
            "limits": {
                "max_queries": run.max_queries,
                "max_evaluations": run.max_evaluations,
                "max_runtime_seconds": run.max_runtime_seconds,
                "spend_ceiling_usd": run.spend_ceiling_usd,
            },
            "usage": {
                "queries_executed": latest_usage.queries_executed if latest_usage else 0,
                "evaluations_scheduled": latest_usage.evaluations_scheduled if latest_usage else 0,
                "evaluations_completed": latest_usage.evaluations_completed if latest_usage else 0,
                "estimated_cost_usd": latest_usage.estimated_cost_usd if latest_usage else 0.0,
                "live_accruing_cost_usd": latest_usage.live_accruing_cost_usd if latest_usage else 0.0,
                "settled_cost_usd": latest_usage.settled_cost_usd if latest_usage else None,
                "is_budget_exhausted": latest_usage.is_budget_exhausted if latest_usage else False,
                "exhaustion_reason": latest_usage.exhaustion_reason if latest_usage else None,
                "recorded_at": latest_usage.recorded_at.isoformat() if (latest_usage and latest_usage.recorded_at) else None,
            },
        }

    return jsonify(metrics_payload), 200


# ==============================================================================
# Report & Benchmark Routes
# ==============================================================================

@runs_bp.route("/runs/<run_id>/report", methods=["GET"])
def get_report_endpoint(run_id: str):
    """Retrieve the scientific summary report for a run."""
    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        report = repo.get_latest_report_for_run(run_id)
        if not report:
            return jsonify({
                "run_id": run_id,
                "report": None,
                "status": "pending",
                "message": "Report has not yet been generated for this run.",
            }), 200

        serialized = serialize_report(report)

    return jsonify({"report": serialized}), 200


@runs_bp.route("/runs/<run_id>/report", methods=["POST"])
def generate_report_endpoint(run_id: str):
    """Generate or retrieve a scientific report for the run."""
    force_recompute = False
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict):
            force_recompute = bool(data.get("force_recompute", False))

    db = get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            report_model, _ = generate_report_service(
                session=session,
                run_id=run_id,
                force_recompute=force_recompute,
            )
            serialized = serialize_report(report_model)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("generate_report_endpoint db error", exc)
        return error_response("An internal error occurred while generating the report.", 500)

    return jsonify({
        "report": serialized,
        "message": "Report generated successfully.",
    }), 201


@runs_bp.route("/runs/<run_id>/report/export", methods=["GET"])
def export_report_endpoint(run_id: str):
    """Export scientific report as JSON or Markdown."""
    format_param = request.args.get("format", "").strip()
    if not format_param:
        accept_hdr = request.headers.get("Accept", "")
        if "text/markdown" in accept_hdr:
            format_param = "markdown"
        else:
            format_param = "json"

    db = get_db()
    try:
        with db.session_scope() as session:
            content, mime_type = export_report_service(
                session=session,
                run_id=run_id,
                format_type=format_param,
            )
    except Exception as exc:  # noqa: BLE001
        log_safe_error("export_report_endpoint db error", exc)
        return error_response("An internal error occurred while exporting the report.", 500)

    ext = "md" if "markdown" in mime_type else "json"
    filename = f"report_{run_id}.{ext}"
    return Response(
        content,
        mimetype=mime_type,
        headers={"Content-Disposition": f"inline; filename={filename}"},
    )


@runs_bp.route("/runs/<run_id>/benchmarks", methods=["GET"])
def get_benchmarks_endpoint(run_id: str):
    """Retrieve benchmark results for a run."""
    db = get_db()
    with db.session_scope() as session:
        repo = ClarityRepository(session)
        run = repo.get_run(run_id)
        if not run:
            return error_response(f"Run '{run_id}' not found.", 404)

        benchmarks = repo.get_benchmark_results_for_run(run_id)
        serialized_benchmarks = [serialize_benchmark(b) for b in benchmarks]

    return jsonify({
        "benchmarks": serialized_benchmarks,
        "count": len(serialized_benchmarks),
    }), 200
