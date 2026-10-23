"""API v1 discovery-run endpoints.

Endpoints:
  POST   /api/v1/runs                      Create a new discovery run
  GET    /api/v1/runs                      List runs with pagination and status filter
  GET    /api/v1/runs/<run_id>             Retrieve a specific discovery run
  GET    /api/v1/runs/<run_id>/metrics     Usage, budget, and cost metrics
  GET    /api/v1/runs/<run_id>/report      Scientific summary report
  GET    /api/v1/runs/<run_id>/benchmarks  Benchmark comparison results
  GET    /api/v1/runs/<run_id>/observations Observations with pagination
  GET    /api/v1/runs/<run_id>/hypotheses  Hypotheses for a run
  POST   /api/v1/runs/<run_id>/hypotheses  Propose a researcher hypothesis
  POST   /api/v1/runs/<run_id>/cancel      Cancel an active run

Route handlers are intentionally thin. Persistence operations are
delegated to ClarityRepository; no business logic lives here.
"""

import uuid
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
from ...services.hypothesis_service import (
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    propose_hypothesis_service,
)
from ...services.run_service import (
    RunConflictError,
    RunNotFoundError,
    cancel_run_service,
    create_run_service,
)
from .shared import (
    error_response,
    log_safe_error,
    serialize_benchmark,
    serialize_hypothesis,
    serialize_observation,
    serialize_report,
    serialize_run,
    serialize_usage,
)

runs_v1_bp = Blueprint("runs_v1", __name__)

# Valid values enforced at this boundary
_VALID_EXECUTION_MODES = {"local", "nodus_remote", "mock"}
_VALID_RUN_STATUSES = {"pending", "running", "completed", "cancelled", "failed", "budget_exhausted"}
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


def _parse_pagination(request_args):
    """Return (limit, offset) or raise ValueError with a user-facing message."""
    try:
        limit = int(request_args.get("limit", _DEFAULT_PAGE_SIZE))
        offset = int(request_args.get("offset", 0))
    except (TypeError, ValueError):
        raise ValueError("Query parameters 'limit' and 'offset' must be integers.")
    if limit <= 0 or offset < 0:
        raise ValueError("'limit' must be > 0 and 'offset' must be >= 0.")
    return min(limit, _MAX_PAGE_SIZE), offset


# ---------------------------------------------------------------------------
# Run endpoints
# ---------------------------------------------------------------------------

@runs_v1_bp.route("/runs", methods=["POST"])
def create_run():
    """POST /api/v1/runs — Validate configuration and create a discovery run.

    Successful creation returns 201 with the new run resource.
    Run creation does not imply scientific discovery has started.
    """
    if not request.is_json:
        return error_response("Payload must be JSON with Content-Type application/json.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response("Request body must be a JSON object.", 400)

    # --- Required fields ---
    name = data.get("name")
    if not name or not isinstance(name, str) or not name.strip():
        return error_response("Field 'name' is required and must be a non-empty string.", 400)

    # --- Optional fields with validated defaults ---
    domain = data.get("domain", "default")
    if not isinstance(domain, str) or not domain.strip():
        return error_response("Field 'domain' must be a non-empty string.", 400)

    execution_mode = data.get("execution_mode", "local")
    if execution_mode not in _VALID_EXECUTION_MODES:
        return error_response(
            f"Field 'execution_mode' must be one of: {', '.join(sorted(_VALID_EXECUTION_MODES))}.", 400
        )

    sampling_strategy = data.get("sampling_strategy", "active_disagreement")
    if not isinstance(sampling_strategy, str) or not sampling_strategy.strip():
        return error_response("Field 'sampling_strategy' must be a non-empty string.", 400)

    # --- Numeric limit validation ---
    def _int_limit(field: str) -> Optional[int]:
        val = data.get(field)
        if val is None:
            return None
        if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
            raise ValueError(f"Field '{field}' must be a positive integer.")
        return val

    def _float_limit(field: str) -> Optional[float]:
        val = data.get(field)
        if val is None:
            return None
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
            raise ValueError(f"Field '{field}' must be a non-negative number.")
        return float(val)

    try:
        max_queries = _int_limit("max_queries")
        max_evaluations = _int_limit("max_evaluations")
        max_runtime_seconds = _int_limit("max_runtime_seconds")
        spend_ceiling_usd = _float_limit("spend_ceiling_usd")
    except ValueError as exc:
        return error_response(str(exc), 400)

    # --- Optional caller-provided run_id ---
    run_id = data.get("run_id")
    if run_id is not None:
        if not isinstance(run_id, str) or not run_id.strip():
            return error_response("Field 'run_id' must be a non-empty string when provided.", 400)
        run_id = run_id.strip()
    else:
        run_id = f"run-{uuid.uuid4().hex[:12]}"

    db = _get_db()
    try:
        with db.session_scope() as session:
            run = create_run_service(
                session=session,
                name=name.strip(),
                domain=domain.strip(),
                execution_mode=execution_mode,
                sampling_strategy=sampling_strategy.strip(),
                max_queries=max_queries,
                max_evaluations=max_evaluations,
                max_runtime_seconds=max_runtime_seconds,
                spend_ceiling_usd=spend_ceiling_usd,
                run_id=run_id,
            )
            serialized = serialize_run(run)
    except RunConflictError as exc:
        return error_response(str(exc), 409)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("create_run db error", exc)
        return error_response("An internal error occurred while creating the run.", 500)

    return jsonify({"run": serialized}), 201


@runs_v1_bp.route("/runs", methods=["GET"])
def list_runs():
    """GET /api/v1/runs — List discovery runs with pagination.

    Query params:
      limit  (int, 1–200, default 50)
      offset (int, >= 0, default 0)
      status (str, optional filter)
    """
    try:
        limit, offset = _parse_pagination(request.args)
    except ValueError as exc:
        return error_response(str(exc), 400)

    status_filter = request.args.get("status")
    if status_filter and status_filter not in _VALID_RUN_STATUSES:
        return error_response(
            f"'status' filter must be one of: {', '.join(sorted(_VALID_RUN_STATUSES))}.", 400
        )

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            runs = repo.list_runs(limit=limit, offset=offset, status_filter=status_filter)
            serialized = [serialize_run(r) for r in runs]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_runs db error", exc)
        return error_response("An internal error occurred while listing runs.", 500)

    return jsonify({
        "runs": serialized,
        "count": len(serialized),
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter,
    }), 200


@runs_v1_bp.route("/runs/<run_id>", methods=["GET"])
def get_run(run_id: str):
    """GET /api/v1/runs/<run_id> — Retrieve run status and lifecycle information."""
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)
            serialized = serialize_run(run)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run db error", exc)
        return error_response("An internal error occurred while retrieving the run.", 500)

    return jsonify({"run": serialized}), 200


@runs_v1_bp.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id: str):
    """POST /api/v1/runs/<run_id>/cancel — Cancel a pending or running discovery run.

    Returns 409 if the run is already in a terminal state.
    """
    reason = "Cancelled by researcher"
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if isinstance(body, dict):
            r = body.get("reason")
            if isinstance(r, str) and r.strip():
                reason = r.strip()

    db = _get_db()
    try:
        with db.session_scope() as session:
            run = cancel_run_service(
                session=session,
                run_id=run_id,
                stop_reason=reason,
            )
            serialized = serialize_run(run)
    except RunNotFoundError as exc:
        return error_response(str(exc), 404)
    except RunConflictError as exc:
        return error_response(str(exc), 409)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("cancel_run db error", exc)
        return error_response("An internal error occurred while cancelling the run.", 500)

    return jsonify({"run": serialized, "message": "Run cancelled successfully."}), 200


@runs_v1_bp.route("/runs/<run_id>/metrics", methods=["GET"])
def get_run_metrics(run_id: str):
    """GET /api/v1/runs/<run_id>/metrics — Execution metrics, budget, and cost state.

    Distinguishes measured 0 from unavailable (None). Separates estimated,
    live-accruing, and settled cost per context.md §5.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            latest_usage = repo.get_latest_usage_for_run(run_id)
            payload = {
                "run_id": run.run_id,
                "status": run.status,
                "stop_reason": run.stop_reason,
                "limits": {
                    "max_queries": run.max_queries,
                    "max_evaluations": run.max_evaluations,
                    "max_runtime_seconds": run.max_runtime_seconds,
                    "spend_ceiling_usd": run.spend_ceiling_usd,
                },
                "usage": serialize_usage(latest_usage),
            }
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_metrics db error", exc)
        return error_response("An internal error occurred while retrieving metrics.", 500)

    return jsonify(payload), 200


@runs_v1_bp.route("/runs/<run_id>/report", methods=["GET"])
def get_run_report(run_id: str):
    """GET /api/v1/runs/<run_id>/report — Generated scientific report.

    Returns report: null with status 'pending' when the report has not been
    generated, rather than fabricating content.
    """
    db = _get_db()
    try:
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
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_report db error", exc)
        return error_response("An internal error occurred while retrieving the report.", 500)

    return jsonify({"report": serialized}), 200


@runs_v1_bp.route("/runs/<run_id>/benchmarks", methods=["GET"])
def get_run_benchmarks(run_id: str):
    """GET /api/v1/runs/<run_id>/benchmarks — Benchmark comparison results."""
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            benchmarks = repo.get_benchmark_results_for_run(run_id)
            serialized = [serialize_benchmark(b) for b in benchmarks]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_benchmarks db error", exc)
        return error_response("An internal error occurred while retrieving benchmarks.", 500)

    return jsonify({
        "benchmarks": serialized,
        "count": len(serialized),
    }), 200


@runs_v1_bp.route("/runs/<run_id>/observations", methods=["GET"])
def list_run_observations(run_id: str):
    """GET /api/v1/runs/<run_id>/observations — Observations with pagination.

    Query params:
      limit         (int, 1–200, default 50)
      offset        (int, >= 0, default 0)
      only_training (bool, default false) — exclude held-out set
    """
    try:
        limit, offset = _parse_pagination(request.args)
    except ValueError as exc:
        return error_response(str(exc), 400)

    only_training_raw = request.args.get("only_training", "false").lower()
    only_training = only_training_raw in {"true", "1", "yes"}

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            total = repo.count_observations_for_run(run_id, only_training=only_training)
            page = repo.list_observations_for_run(
                run_id,
                only_training=only_training,
                limit=limit,
                offset=offset,
            )
            serialized = [serialize_observation(o) for o in page]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_run_observations db error", exc)
        return error_response("An internal error occurred while retrieving observations.", 500)

    return jsonify({
        "observations": serialized,
        "count": len(serialized),
        "total": total,
        "limit": limit,
        "offset": offset,
        "only_training": only_training,
    }), 200


@runs_v1_bp.route("/runs/<run_id>/hypotheses", methods=["GET"])
def list_run_hypotheses(run_id: str):
    """GET /api/v1/runs/<run_id>/hypotheses — Hypotheses associated with a run.

    Query params:
      include_excluded (bool, default true)
    """
    include_excluded_raw = request.args.get("include_excluded", "true").lower()
    include_excluded = include_excluded_raw not in {"false", "0", "no"}

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            hypotheses = repo.list_hypotheses_for_run(run_id, include_excluded=include_excluded)
            serialized = [serialize_hypothesis(h, repo) for h in hypotheses]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_run_hypotheses db error", exc)
        return error_response("An internal error occurred while retrieving hypotheses.", 500)

    return jsonify({
        "hypotheses": serialized,
        "count": len(serialized),
    }), 200


@runs_v1_bp.route("/runs/<run_id>/hypotheses", methods=["POST"])
def create_run_hypothesis(run_id: str):
    """POST /api/v1/runs/<run_id>/hypotheses — Submit a researcher-proposed hypothesis.

    The hypothesis is recorded as a candidate. It is not automatically validated
    against the oracle or marked correct.
    """
    if not request.is_json:
        return error_response("Payload must be JSON with Content-Type application/json.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response("Request body must be a JSON object.", 400)

    model_family = data.get("model_family")
    if not model_family or not isinstance(model_family, str) or not model_family.strip():
        return error_response("Field 'model_family' is required and must be a non-empty string.", 400)

    formula = data.get("formula")
    if not formula or not isinstance(formula, str) or not formula.strip():
        return error_response("Field 'formula' is required and must be a non-empty string.", 400)

    parameters = data.get("parameters", {})
    if not isinstance(parameters, dict):
        return error_response("Field 'parameters' must be a JSON object of coefficients/weights.", 400)

    complexity = data.get("complexity", 1)
    if isinstance(complexity, bool) or not isinstance(complexity, int) or complexity < 1:
        return error_response("Field 'complexity' must be an integer >= 1.", 400)

    hypothesis_id = data.get("hypothesis_id")
    if hypothesis_id is not None:
        if not isinstance(hypothesis_id, str) or not hypothesis_id.strip():
            return error_response("Field 'hypothesis_id' must be a non-empty string when provided.", 400)
        hypothesis_id = hypothesis_id.strip()
    else:
        hypothesis_id = f"hyp-{uuid.uuid4().hex[:12]}"

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            if hypothesis_id and repo.get_hypothesis(hypothesis_id):
                return error_response(f"Hypothesis '{hypothesis_id}' already exists.", 409)

            hyp = propose_hypothesis_service(
                session=session,
                run_id=run_id,
                model_family=model_family.strip(),
                formula=formula.strip(),
                parameters=parameters,
                complexity=complexity,
                creation_source="researcher",
                hypothesis_id=hypothesis_id,
            )
            serialized = serialize_hypothesis(hyp, repo)
    except HypothesisNotFoundError as exc:
        return error_response(str(exc), 404)
    except HypothesisConflictError as exc:
        return error_response(str(exc), 409)
    except HypothesisValidationError as exc:
        return error_response(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("create_run_hypothesis db error", exc)
        return error_response("An internal error occurred while creating the hypothesis.", 500)

    return jsonify({"hypothesis": serialized}), 201
