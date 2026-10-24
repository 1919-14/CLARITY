"""API v1 discovery-run endpoints.

Endpoints:
  POST   /api/v1/runs                      Create a new discovery run
  GET    /api/v1/runs                      List runs with pagination and status filter
  GET    /api/v1/runs/<run_id>             Retrieve a specific discovery run
  GET    /api/v1/runs/<run_id>/status      Live lifecycle progress snapshot
  GET    /api/v1/runs/<run_id>/metrics     Usage, budget, and cost metrics
  GET    /api/v1/runs/<run_id>/budget      Per-run budget status contract
  GET    /api/v1/runs/<run_id>/report      Scientific summary report
  GET    /api/v1/runs/<run_id>/benchmarks  Benchmark comparison results
  GET    /api/v1/runs/<run_id>/observations Observations with pagination
  GET    /api/v1/runs/<run_id>/hypotheses  Hypotheses for a run
  POST   /api/v1/runs/<run_id>/hypotheses  Propose a researcher hypothesis
  POST   /api/v1/runs/<run_id>/start       Start execution of a queued run
  POST   /api/v1/runs/<run_id>/cancel      Cancel an active or queued run
  GET    /api/v1/runs/<run_id>/history     Auditable researcher action history

Route handlers are intentionally thin. Persistence operations are
delegated to ClarityRepository; lifecycle transitions are delegated to
RunLifecycleManager. No business logic lives here.
"""

import uuid
from typing import Any, Dict, List, Optional

from flask import Blueprint, Response, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.models import HypothesisModel
from ...adapters.persistence.repository import ClarityRepository
from ...domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisRepresentation,
    HypothesisStatus,
    Observation,
    ObservationStatus,
)
from ...domain.ranking_contracts import RankingPolicyConfig
from ...domain.ranking_engine import HypothesisRanker
from ...domain.budget_contracts import (
    BudgetValidationError,
    RunBudgetConfig,
)
from ...domain.run_lifecycle import (
    DuplicateExecutionError,
    InvalidStateTransitionError,
    RunLifecycleManager,
    RunLifecycleState,
    RunNotFoundError,
    default_lifecycle_manager,
)
from ...services.budget_service import (
    get_budget_status,
    persist_exhaustion,
)
from ...services.hypothesis_service import (
    HypothesisAlreadyAdmittedError,
    HypothesisBudgetExhaustedError,
    HypothesisConflictError,
    HypothesisNotFoundError,
    HypothesisValidationError,
    propose_hypothesis_service,
)
from ...domain.history_contracts import InvalidHistoryCursorError
from ...services.report_service import (
    export_report_service,
    generate_report_service,
)
from ...services.audit_service import (
    list_audit_events_service,
    record_audit_event_service,
)
from ...services.history_service import (
    HistoryNotFoundError,
    list_run_history,
)
from ...domain.usage_contracts import (
    AuditEventConflictError,
    TaskRunConflictError,
)
from ...services.run_execution import RunBudgetUnavailable, execute_persisted_run
from ...services.run_service import (
    RunConflictError,
    create_run_service,
)
from ...services.benchmark_service import trigger_benchmark_service
from ...services.usage_service import (
    derive_run_usage_summary,
    reconcile_run_usage,
    record_task_usage_service,
)
from .shared import (
    error_response,
    log_safe_error,
    serialize_audit_event,
    serialize_benchmark,
    serialize_hypothesis,
    serialize_observation,
    serialize_report,
    serialize_run,
    serialize_task,
    serialize_usage,
)

runs_v1_bp = Blueprint("runs_v1", __name__)

# Valid values enforced at this boundary
_VALID_EXECUTION_MODES = {"local", "nodus_remote", "mock"}
_VALID_RUN_STATUSES = {
    "pending", "queued", "running", "cancelling",
    "completed", "cancelled", "failed",
    "budget_exhausted", "stopped_by_budget",
}
_MAX_PAGE_SIZE = 200
_DEFAULT_PAGE_SIZE = 50


def _lifecycle_manager() -> RunLifecycleManager:
    """Return the application-scoped lifecycle manager.

    Allows tests to substitute a custom instance via Flask app config.
    """
    return current_app.config.get("LIFECYCLE_MANAGER", default_lifecycle_manager)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


def _request_correlation_id() -> Optional[str]:
    """Request tracing identifier supplied by the caller, when present."""
    for header in ("X-Request-ID", "X-Correlation-ID"):
        value = request.headers.get(header)
        if value and value.strip():
            return value.strip()[:128]
    return None


def _request_actor_id() -> Optional[str]:
    """Authenticated actor identity from a trusted gateway header, if any.

    CLARITY has no authentication layer yet; this is recorded as an audit
    attribute only and never used as an authorization decision.
    """
    value = request.headers.get("X-Actor-ID")
    if value and value.strip():
        return value.strip()[:128]
    return None


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

    # --- Budget validation ---
    # Canonical nested ``budget`` object, or the legacy flat v1 field names.
    # Both forms are accepted, but mixing them is rejected as ambiguous.
    budget_payload = data.get("budget")
    if budget_payload is not None and not isinstance(budget_payload, dict):
        return error_response("Field 'budget' must be a JSON object.", 400)

    legacy_payload = {}
    for field in (
        "max_queries",
        "max_evaluations",
        "max_runtime_seconds",
        "spend_ceiling_usd",
        "max_concurrent_remote_tasks",
        "max_generated_hypotheses",
    ):
        if field in data and data[field] is not None:
            legacy_payload[field] = data[field]

    if budget_payload is not None and legacy_payload:
        return error_response(
            "Provide the budget either as a 'budget' object or as legacy flat limit fields, "
            "not both.",
            400,
            error="Bad Request",
            details=[
                {
                    "field": "budget",
                    "code": "CONFLICTING_SETTING",
                    "message": "Conflicting budget payloads were supplied.",
                }
            ],
        )

    try:
        if budget_payload is not None:
            budget_config = RunBudgetConfig.from_mapping(budget_payload)
        else:
            budget_config = RunBudgetConfig.from_mapping(
                legacy_payload, allow_legacy_aliases=True
            )
    except BudgetValidationError as exc:
        return error_response(
            exc.message,
            400,
            error="Bad Request",
            details=exc.details,
        )

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
                run_id=run_id,
                budget=budget_config,
            )

            # Register the newly persisted run with the lifecycle manager. The
            # manager also normalizes the persisted status to QUEUED. Registration
            # is compensated on failure so a half-initialized run cannot linger in
            # the in-memory registry after the session rolls back.
            mgr = _lifecycle_manager()
            try:
                mgr.initialize_run(
                    run_id=run_id,
                    name=name.strip(),
                    max_queries=run.max_queries,
                    max_evaluations=run.max_evaluations,
                    domain=domain.strip(),
                    session=session,
                )
            except Exception:
                mgr.deregister_run(run_id)
                raise

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


@runs_v1_bp.route("/runs/<run_id>/status", methods=["GET"])
def get_run_status(run_id: str):
    """GET /api/v1/runs/<run_id>/status — Live lifecycle progress snapshot.

    Returns real counters (queries used, evaluations completed, elapsed time).
    Never fabricates percentage completion when total work is unknown.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            # Hydrate from DB if not in memory (e.g. after restart)
            progress = _lifecycle_manager().get_progress(run_id, session=session)
    except RunNotFoundError:
        return error_response(f"Run '{run_id}' not found.", 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_status error", exc)
        return error_response("An internal error occurred while retrieving run status.", 500)

    return jsonify({"status": progress.to_dict()}), 200


@runs_v1_bp.route("/runs/<run_id>/cancel", methods=["POST"])
def cancel_run(run_id: str):
    """POST /api/v1/runs/<run_id>/cancel — Cancel a pending or running discovery run.

    Delegates state validation to RunLifecycleManager so that only
    valid state transitions are allowed. Returns 409 for terminal runs.
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
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            # Lifecycle manager enforces terminal-state guard and state machine
            progress = _lifecycle_manager().request_cancellation(
                run_id, reason=reason, session=session
            )

            # Reload serialized view after lifecycle manager updated the model
            run = repo.get_run(run_id)
            serialized = serialize_run(run)
    except InvalidStateTransitionError:
        return error_response(
            f"Run '{run_id}' is already in a terminal state and cannot be cancelled.",
            409,
        )
    except RunNotFoundError:
        return error_response(f"Run '{run_id}' not found.", 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("cancel_run db error", exc)
        return error_response("An internal error occurred while cancelling the run.", 500)

    if progress.status == RunLifecycleState.CANCELLING:
        return jsonify({
            "run": serialized,
            "status": progress.to_dict(),
            "message": "Run cancellation requested. Shutdown of in-flight work is in progress.",
        }), 202

    return jsonify({
        "run": serialized,
        "status": progress.to_dict(),
        "message": "Run cancelled successfully.",
    }), 200


@runs_v1_bp.route("/runs/<run_id>/start", methods=["POST"])
def start_run(run_id: str):
    """POST /api/v1/runs/<run_id>/start — Start execution of a queued discovery run.

    Transitions the run to RUNNING and, when a ``RUN_ENGINE_FACTORY`` is
    configured, executes it through ``execute_persisted_run`` so the persisted
    per-run budget is always attached. Without a configured factory the run
    stays RUNNING with no work dispatched; this endpoint never starts unbudgeted
    execution. Returns 409 if already running or terminal, and 503 when the run
    cannot be composed with its persisted budget.
    """
    db = _get_db()
    engine_factory = current_app.config.get("RUN_ENGINE_FACTORY")
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            progress = _lifecycle_manager().start_run(run_id, session=session)
            run = repo.get_run(run_id)
            serialized = serialize_run(run)
    except (DuplicateExecutionError, InvalidStateTransitionError) as exc:
        return error_response(str(exc), 409)
    except RunNotFoundError:
        return error_response(f"Run '{run_id}' not found.", 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("start_run db error", exc)
        return error_response("An internal error occurred while starting the run.", 500)

    execution: Optional[dict] = None
    if callable(engine_factory):
        try:
            result = execute_persisted_run(
                run_id,
                db,
                engine_factory,
                orchestrator=current_app.config.get("RUN_ORCHESTRATOR"),
            )
            execution = {
                "dispatched": True,
                "status": getattr(result, "status", None).value
                if getattr(result, "status", None) is not None
                else None,
            }
        except RunBudgetUnavailable as exc:
            # Fail closed: report the run cannot execute budgeted work rather
            # than letting it proceed unbudgeted.
            return error_response(str(exc), 503, error="Service Unavailable")
        except Exception as exc:  # noqa: BLE001
            log_safe_error("start_run execution error", exc)
            return error_response(
                "The run was started but its execution failed.", 500
            )

    if execution is None:
        execution = {
            "dispatched": False,
            "reason": "no_engine_factory_configured",
        }

    return jsonify({
        "run": serialized,
        "status": progress.to_dict(),
        "execution": execution,
        "message": "Run execution started successfully."
        if execution.get("dispatched")
        else "Run marked running; no execution provider is configured.",
    }), 200


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
                    "max_concurrent_remote_tasks": run.max_concurrent_remote_tasks,
                    "max_generated_hypotheses": run.max_generated_hypotheses,
                },
                "usage": serialize_usage(latest_usage),
                "budget": get_budget_status(session, run_id).to_dict(),
            }
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_metrics db error", exc)
        return error_response("An internal error occurred while retrieving metrics.", 500)

    return jsonify(payload), 200


@runs_v1_bp.route("/runs/<run_id>/budget", methods=["GET"])
def get_run_budget(run_id: str):
    """GET /api/v1/runs/<run_id>/budget — Per-run budget status contract.

    Returns configured limits, admitted/remaining capacity, concurrency,
    runtime, cost states, and the structured exhaustion reason. Unavailable
    monetary data is null, never zero.
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)
            status = get_budget_status(session, run_id)
            payload = status.to_dict()
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_budget db error", exc)
        return error_response(
            "An internal error occurred while retrieving the run budget.", 500
        )

    return jsonify({"run_id": run_id, "budget": payload}), 200


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


@runs_v1_bp.route("/runs/<run_id>/report", methods=["POST"])
def generate_run_report_endpoint(run_id: str):
    """POST /api/v1/runs/<run_id>/report — Generate or update a scientific report for the run."""
    force_recompute = False
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict):
            force_recompute = bool(data.get("force_recompute", False))

    db = _get_db()
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
    except RunNotFoundError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("generate_run_report db error", exc)
        return error_response("An internal error occurred while generating the report.", 500)

    return jsonify({
        "report": serialized,
        "message": "Report generated successfully.",
    }), 201


@runs_v1_bp.route("/runs/<run_id>/report/export", methods=["GET"])
def export_run_report_endpoint(run_id: str):
    """GET /api/v1/runs/<run_id>/report/export — Export scientific report as JSON or Markdown."""
    format_param = request.args.get("format", "").strip()
    if not format_param:
        accept_hdr = request.headers.get("Accept", "")
        if "text/markdown" in accept_hdr:
            format_param = "markdown"
        else:
            format_param = "json"

    db = _get_db()
    try:
        with db.session_scope() as session:
            content, mime_type = export_report_service(
                session=session,
                run_id=run_id,
                format_type=format_param,
            )
    except RunNotFoundError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("export_run_report db error", exc)
        return error_response("An internal error occurred while exporting the report.", 500)

    ext = "md" if "markdown" in mime_type else "json"
    filename = f"report_{run_id}.{ext}"
    return Response(
        content,
        mimetype=mime_type,
        headers={"Content-Disposition": f"inline; filename={filename}"},
    )


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


@runs_v1_bp.route("/runs/<run_id>/benchmarks", methods=["POST"])
def trigger_run_benchmark(run_id: str):
    """POST /api/v1/runs/<run_id>/benchmarks — Execute a ground-truth benchmark evaluation."""
    payload = request.get_json(silent=True) or {}

    # Reject caller-injected ground-truth keys (Requirement 4)
    forbidden_target_keys = {
        "target_label",
        "target_formula",
        "target_parameters",
        "target_coefficients",
        "secret_seed",
        "coefficients",
    }
    injected_keys = sorted(list(forbidden_target_keys.intersection(payload.keys())))
    if injected_keys:
        return error_response(
            f"Client-supplied ground truth ({', '.join(injected_keys)}) is prohibited. "
            "Evaluations must reference an independently held registered 'benchmark_case'.",
            400,
        )

    benchmark_case = payload.get("benchmark_case")
    if not benchmark_case or not isinstance(benchmark_case, str) or not benchmark_case.strip():
        return error_response(
            "Field 'benchmark_case' is required and must reference a registered benchmark case.",
            400,
        )

    baseline_type = payload.get("baseline_type", "random_sampling")
    expected_baseline_queries = payload.get("expected_baseline_queries")
    custom_metadata = payload.get("metadata")
    tolerance = float(payload.get("tolerance", 0.05))

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            result = trigger_benchmark_service(
                session=session,
                run_id=run_id,
                benchmark_case=benchmark_case.strip(),
                baseline_type=baseline_type,
                expected_baseline_queries=expected_baseline_queries,
                custom_metadata=custom_metadata,
                tolerance=tolerance,
            )
            serialized = serialize_benchmark(result)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("trigger_run_benchmark error", exc)
        return error_response("An internal error occurred while executing benchmark evaluation.", 500)

    return jsonify({"benchmark": serialized}), 201


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


def _attach_scientific_rankings(
    hypotheses: List[HypothesisModel],
    serialized: List[Dict[str, Any]],
    repo: ClarityRepository,
    run_id: str,
) -> None:
    """Compute and attach within-cohort scientific rankings without conflating pinning or exclusion."""
    try:
        raw_obs = repo.list_observations_for_run(run_id)
        domain_obs: List[Observation] = []
        training_ids: List[str] = []
        held_out_ids: List[str] = []
        for o in raw_obs:
            obs_status = ObservationStatus.SUCCESS if o.status == "success" else ObservationStatus.ORACLE_FAILURE
            if o.is_held_out:
                held_out_ids.append(o.observation_id)
            else:
                training_ids.append(o.observation_id)
            domain_obs.append(
                Observation(
                    observation_id=o.observation_id,
                    input_id=o.input_id or o.observation_id,
                    input_values=o.input_values or {},
                    output_values=o.output_values,
                    status=obs_status,
                    sequence_number=o.sequence_number,
                    is_held_out=bool(o.is_held_out),
                )
            )

        candidate_reprs: List[HypothesisRepresentation] = []
        evals_by_hyp: Dict[str, EvaluationResult] = {}

        for h in hypotheses:
            revisions = sorted(h.revisions, key=lambda r: r.revision_number) if h.revisions else []
            if not revisions:
                continue
            latest_rev = revisions[-1]
            evals = repo.list_evaluations_for_revision(latest_rev.revision_id)
            latest_ev = evals[0] if evals else None

            cand_repr = HypothesisRepresentation(
                model_family=h.model_family,
                formula=latest_rev.formula or f"Candidate {h.hypothesis_id}",
                parameters=latest_rev.parameters or {},
                complexity=latest_rev.complexity or 1,
                hypothesis_id=h.hypothesis_id,
                revision=latest_rev.revision_number,
                status=HypothesisStatus.EVALUATED if latest_ev else HypothesisStatus.CANDIDATE,
            )
            candidate_reprs.append(cand_repr)

            if latest_ev:
                status_enum = EvaluationStatus.COMPLETED if latest_ev.status == "completed" else EvaluationStatus.FAILED
                eval_res = EvaluationResult(
                    evaluation_id=latest_ev.evaluation_id,
                    hypothesis_id=h.hypothesis_id,
                    status=status_enum,
                    metrics=latest_ev.metrics or {},
                    held_out_metrics=latest_ev.held_out_metrics,
                    sample_count=latest_ev.sample_count or len(training_ids),
                    held_out_sample_count=len(held_out_ids) if latest_ev.held_out_metrics else None,
                    error=None,
                    model_family=h.model_family,
                    fitted_specification=latest_rev.parameters or {},
                    metric_version="1.0.0",
                    metric_definitions={
                        "mae": "Mean Absolute Error",
                        "rmse": "Root Mean Squared Error",
                    },
                    dataset_ids={
                        "dataset_id": run_id,
                        "dataset_version": latest_ev.dataset_version or 1,
                        "observation_ids": training_ids,
                        "training_observation_ids": training_ids,
                        "held_out_observation_ids": held_out_ids,
                    },
                )
                evals_by_hyp[h.hypothesis_id] = eval_res

        ranker = HypothesisRanker(RankingPolicyConfig())
        obs_map = {c.hypothesis_id: domain_obs for c in candidate_reprs}
        ranking_result = ranker.rank_candidates(
            candidates=candidate_reprs,
            evaluation_results=evals_by_hyp,
            observations_by_hypothesis=obs_map,
        )

        ranked_map = {r.hypothesis_id: r for r in ranking_result.ranked_candidates}

        for s in serialized:
            rc = ranked_map.get(s["hypothesis_id"])
            if rc:
                s["ranking"] = {
                    "rank": rc.rank,
                    "within_cohort_rank": rc.within_cohort_rank,
                    "evaluation_tier": rc.evaluation_tier.value,
                    "plausibility_status": rc.status.value,
                    "primary_metric_name": rc.primary_metric_name,
                    "held_out_score": rc.held_out_score,
                    "training_score": rc.training_score,
                    "secondary_held_out_score": rc.secondary_held_out_score,
                    "complexity": rc.complexity,
                    "limitations": list(rc.limitations),
                }
            else:
                s["ranking"] = None
    except Exception as exc:
        log_safe_error("_attach_scientific_rankings error", exc)
        for s in serialized:
            if "ranking" not in s:
                s["ranking"] = None


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
            _attach_scientific_rankings(hypotheses, serialized, repo, run_id)
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
                actor_id=_request_actor_id(),
                correlation_id=_request_correlation_id(),
            )
            serialized = serialize_hypothesis(hyp, repo)
    except HypothesisBudgetExhaustedError as exc:
        # The failed transaction was rolled back; record the terminal decision
        # in its own transaction so it survives the error response.
        try:
            with db.session_scope() as session:
                persist_exhaustion(session, run_id, exc.reason, details=exc.to_dict())
        except Exception as persist_exc:  # noqa: BLE001
            log_safe_error("persist hypothesis budget exhaustion error", persist_exc)
        return error_response(
            "The run's hypothesis generation limit has been reached.",
            409,
            error="Conflict",
            details=exc.to_dict(),
        )
    except HypothesisAlreadyAdmittedError as exc:
        return error_response(
            "This hypothesis was already admitted against the run's budget.",
            409,
            error="Conflict",
            details=exc.to_dict(),
        )
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


@runs_v1_bp.route("/runs/<run_id>/usage", methods=["GET"])
def get_run_usage(run_id: str):
    """GET /api/v1/runs/<run_id>/usage — Comprehensive compute-usage summary.

    Reconciles task-level compute with run-level usage, breaks down local vs remote
    execution, and separates estimated, accruing, and settled costs (Issue #15).
    """
    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            summary = derive_run_usage_summary(session, run_id)
            payload = summary.to_dict()
    except ValueError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_usage db error", exc)
        return error_response("An internal error occurred while retrieving run usage.", 500)

    return jsonify({"run_id": run_id, "usage": payload}), 200


@runs_v1_bp.route("/runs/<run_id>/tasks", methods=["GET"])
def list_run_tasks(run_id: str):
    """GET /api/v1/runs/<run_id>/tasks — Task-level compute execution records.

    Query params:
      status (str, optional)
      execution_mode (str, optional: local, nodus_remote, mock)
      limit (int, 1–200, default 50)
      offset (int, >= 0, default 0)
    """
    try:
        limit, offset = _parse_pagination(request.args)
    except ValueError as exc:
        return error_response(str(exc), 400)

    status_filter = request.args.get("status")
    mode_filter = request.args.get("execution_mode")
    if mode_filter and mode_filter not in _VALID_EXECUTION_MODES:
        return error_response(
            f"'execution_mode' filter must be one of: {', '.join(sorted(_VALID_EXECUTION_MODES))}.",
            400,
        )

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            tasks = repo.list_compute_tasks_for_run(
                run_id=run_id,
                status=status_filter,
                execution_mode=mode_filter,
                limit=limit,
                offset=offset,
            )
            total_count = repo.count_compute_tasks_for_run(
                run_id=run_id,
                status=status_filter,
                execution_mode=mode_filter,
            )
            serialized = [serialize_task(t) for t in tasks]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_run_tasks db error", exc)
        return error_response("An internal error occurred while listing tasks.", 500)

    return jsonify({
        "run_id": run_id,
        "tasks": serialized,
        "count": len(serialized),
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }), 200


@runs_v1_bp.route("/runs/<run_id>/history", methods=["GET"])
def get_run_history(run_id: str):
    """GET /api/v1/runs/<run_id>/history — Chronological researcher action history.

    This is the Issue #17 read model over the same append-only audit store that
    backs ``/audit-events``. It adds structured provenance (``before``/``after``,
    ``reason``, ``hypothesis_id``, ``revision_id``, ``correlation_id``), actor and
    hypothesis filtering, and cursor pagination.

    Query params:
      event_type    (str, optional)
      actor         (str, optional)
      hypothesis_id (str, optional)
      revision_id   (str, optional)
      limit         (int, 1-200, default 50)
      cursor        (str, optional — returned as ``next_cursor`` on the previous page)
      offset        (int, optional — offset pagination for compatibility)

    Ordering is ``(timestamp, event_id)`` ascending. Cursor traversal is stable
    when new events are appended between requests; offset traversal is not.
    """
    cursor = request.args.get("cursor")
    offset_param = request.args.get("offset")
    try:
        limit = int(request.args.get("limit", _DEFAULT_PAGE_SIZE))
        offset = int(offset_param) if offset_param is not None else None
    except (TypeError, ValueError):
        return error_response("Query parameters 'limit' and 'offset' must be integers.", 400)
    if limit <= 0 or (offset is not None and offset < 0):
        return error_response("'limit' must be > 0 and 'offset' must be >= 0.", 400)

    event_type = request.args.get("event_type")
    actor = request.args.get("actor")
    hypothesis_id = request.args.get("hypothesis_id")
    revision_id = request.args.get("revision_id")

    db = _get_db()
    try:
        with db.session_scope() as session:
            page = list_run_history(
                session,
                run_id,
                event_type=event_type,
                actor=actor,
                hypothesis_id=hypothesis_id,
                revision_id=revision_id,
                limit=limit,
                offset=offset if cursor is None else None,
                cursor=cursor,
            )
            serialized = [serialize_audit_event(e) for e in page.events]
            pagination = page.to_dict()
    except HistoryNotFoundError as exc:
        return error_response(str(exc), 404)
    except InvalidHistoryCursorError as exc:
        return error_response(str(exc), 400)
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_history db error", exc)
        return error_response("An internal error occurred while retrieving history.", 500)

    return jsonify({
        "run_id": run_id,
        "events": serialized,
        "filters": {
            "event_type": event_type,
            "actor": actor,
            "hypothesis_id": hypothesis_id,
            "revision_id": revision_id,
        },
        "pagination": pagination,
    }), 200


@runs_v1_bp.route("/runs/<run_id>/audit-events", methods=["GET"])
def list_run_audit_events(run_id: str):
    """GET /api/v1/runs/<run_id>/audit-events — Chronological append-only audit trail.

    Query params:
      event_type (str, optional filter)
      limit (int, 1–200, default 50)
      offset (int, >= 0, default 0)
    """
    try:
        limit, offset = _parse_pagination(request.args)
    except ValueError as exc:
        return error_response(str(exc), 400)

    event_type = request.args.get("event_type")

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            events, total_count = list_audit_events_service(
                session=session,
                run_id=run_id,
                event_type=event_type,
                limit=limit,
                offset=offset,
            )
            serialized = [serialize_audit_event(e) for e in events]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_run_audit_events db error", exc)
        return error_response("An internal error occurred while retrieving audit events.", 500)

    return jsonify({
        "run_id": run_id,
        "audit_events": serialized,
        "count": len(serialized),
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "event_type": event_type,
    }), 200


@runs_v1_bp.route("/runs/<run_id>/audit-events", methods=["POST"])
def record_run_audit_event(run_id: str):
    """POST /api/v1/runs/<run_id>/audit-events — Append a new audit event for a run.

    Body:
      event_type (str, required)
      actor (str, optional, default "researcher")
      metadata (dict, optional)
    """
    if not request.is_json:
        return error_response("Payload must be JSON with Content-Type application/json.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return error_response("Request body must be a JSON object.", 400)

    event_type = data.get("event_type")
    if not event_type or not isinstance(event_type, str) or not event_type.strip():
        return error_response("Field 'event_type' is required and must be a non-empty string.", 400)

    actor = data.get("actor", "researcher")
    if not isinstance(actor, str) or not actor.strip():
        actor = "researcher"

    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        return error_response("Field 'metadata' must be a JSON object when provided.", 400)

    event_id = data.get("event_id")
    if event_id is not None:
        if not isinstance(event_id, str) or not event_id.strip():
            return error_response("Field 'event_id' must be a non-empty string when provided.", 400)
        event_id = event_id.strip()

    db = _get_db()
    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            run = repo.get_run(run_id)
            if not run:
                return error_response(f"Run '{run_id}' not found.", 404)

            event = record_audit_event_service(
                session=session,
                run_id=run_id,
                event_type=event_type.strip(),
                actor=actor.strip(),
                metadata=metadata,
                event_id=event_id,
            )
            serialized = serialize_audit_event(event)
    except AuditEventConflictError as exc:
        return error_response(str(exc), 409)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("record_run_audit_event db error", exc)
        return error_response("An internal error occurred while recording audit event.", 500)

    return jsonify({"audit_event": serialized}), 201
