"""Benchmarks v1 routes for CLARITY.

Exposes REST API endpoints to:
- List registered benchmark scenarios with sanitized metadata (no ground-truth secrets).
- Trigger reproducible active vs random selection comparison runs.
- Retrieve structured comparison summaries and individual trial results.
"""

from typing import Any, Dict, List, Optional
from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...adapters.persistence.repository import ClarityRepository
from ...evaluator.benchmark_cases import (
    get_benchmark_case,
    list_benchmark_scenarios,
)
from ...evaluator.benchmark_runner import ActiveRandomBenchmarkRunner
from ...domain.contracts import verify_no_ground_truth_leakage
from .shared import error_response, log_safe_error

benchmarks_v1_bp = Blueprint("benchmarks_v1", __name__)


def _get_db():
    db_uri = current_app.config.get("SQLALCHEMY_DATABASE_URI")
    return get_db_manager(db_uri)


@benchmarks_v1_bp.route("/benchmarks/scenarios", methods=["GET"])
def get_benchmark_scenarios():
    """GET /api/v1/benchmarks/scenarios — List versioned benchmark scenarios.

    Returns sanitized scenario descriptors safe for public/UI consumption.
    Evaluator ground-truth formulas and secret seeds are strictly omitted.
    """
    scenarios = list_benchmark_scenarios()
    return jsonify({
        "scenarios": scenarios,
        "count": len(scenarios),
    }), 200


@benchmarks_v1_bp.route("/benchmarks/compare", methods=["POST"])
def trigger_benchmark_comparison():
    """POST /api/v1/benchmarks/compare — Run reproducible active vs random comparison.

    Payload:
      scenario_id      (str, required): Registered benchmark scenario identifier
      seeds            (list[int], optional): Random seeds for repeated trials (default [42])
      max_query_budget (int, optional): Override query budget per trial (1-100)
    """
    payload = request.get_json(silent=True) or {}

    # Reject caller-injected ground-truth keys
    forbidden_keys = {
        "target_label",
        "target_formula",
        "target_parameters",
        "target_coefficients",
        "secret_seed",
        "coefficients",
    }
    injected_keys = sorted(list(forbidden_keys.intersection(payload.keys())))
    if injected_keys:
        return error_response(
            f"Client-supplied ground truth ({', '.join(injected_keys)}) is prohibited. "
            "Comparisons must reference a registered 'scenario_id'.",
            400,
        )

    scenario_id = payload.get("scenario_id") or payload.get("benchmark_case")
    if not scenario_id or not isinstance(scenario_id, str) or not scenario_id.strip():
        return error_response(
            "Field 'scenario_id' is required and must reference a registered benchmark scenario.",
            400,
        )

    scenario = get_benchmark_case(scenario_id.strip())
    if not scenario:
        return error_response(f"Unknown benchmark scenario '{scenario_id}'.", 400)

    # Validate seeds
    seeds_input = payload.get("seeds", [42])
    if not isinstance(seeds_input, list) or not seeds_input:
        return error_response("Field 'seeds' must be a non-empty list of integers.", 400)

    seeds: List[int] = []
    for s in seeds_input:
        if not isinstance(s, int) or isinstance(s, bool):
            return error_response(f"Invalid seed '{s}'; all seeds must be integers.", 400)
        seeds.append(s)

    if len(seeds) > 20:
        return error_response("Maximum of 20 seeds permitted per comparison request.", 400)

    # Validate budget
    max_budget = payload.get("max_query_budget")
    if max_budget is not None:
        if not isinstance(max_budget, int) or isinstance(max_budget, bool) or max_budget < 1 or max_budget > 100:
            return error_response("Field 'max_query_budget' must be an integer between 1 and 100.", 400)

    db = _get_db()
    runner = ActiveRandomBenchmarkRunner()

    try:
        with db.session_scope() as session:
            run_rec, bm_rec, summary = runner.run_and_persist(
                session=session,
                scenario_name=scenario.benchmark_name,
                seeds=seeds,
                max_budget=max_budget,
            )
            comparison_dict = summary.to_dict()
            run_id = run_rec.run_id
            benchmark_id = bm_rec.benchmark_id
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("trigger_benchmark_comparison error", exc)
        return error_response("An internal error occurred while executing benchmark comparison.", 500)

    return jsonify({
        "run_id": run_id,
        "benchmark_id": benchmark_id,
        "comparison": comparison_dict,
    }), 201


@benchmarks_v1_bp.route("/benchmarks/compare/<comparison_id>", methods=["GET"])
def get_benchmark_comparison(comparison_id: str):
    """GET /api/v1/benchmarks/compare/<comparison_id> — Retrieve comparison results by ID."""
    db = _get_db()
    benchmark_id = f"bm-{comparison_id}" if not comparison_id.startswith("bm-") else comparison_id

    try:
        with db.session_scope() as session:
            repo = ClarityRepository(session)
            # Find benchmark by benchmark_id
            from ...adapters.persistence.models import BenchmarkResultModel
            from sqlalchemy import select
            stmt = select(BenchmarkResultModel).where(BenchmarkResultModel.benchmark_id == benchmark_id)
            bm = session.scalars(stmt).first()
            if not bm:
                return error_response(f"Benchmark comparison '{comparison_id}' not found.", 404)

            metadata = bm.execution_metadata or {}
            verify_no_ground_truth_leakage(metadata)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_benchmark_comparison error", exc)
        return error_response("An internal error occurred while retrieving benchmark comparison.", 500)

    return jsonify({
        "comparison_id": comparison_id,
        "benchmark_id": bm.benchmark_id,
        "run_id": bm.run_id,
        "scenario_id": bm.benchmark_name,
        "comparison": metadata,
    }), 200


@benchmarks_v1_bp.route("/benchmarks/measurements", methods=["POST"])
def trigger_performance_measurement():
    """POST /api/v1/benchmarks/measurements — Record or execute reproducible performance measurements.

    Payload Options:
    1. Measure an existing run:
       {"run_id": "run-xyz", "scenario_id": "linear_polynomial"}
    2. Execute and measure active vs random comparison:
       {"scenario_id": "linear_polynomial", "seeds": [42], "max_query_budget": 10}
    """
    from ...services.performance_measurement_service import PerformanceMeasurementService

    payload = request.get_json(silent=True) or {}

    # Reject caller-injected ground-truth keys
    forbidden_keys = {
        "target_label",
        "target_formula",
        "target_parameters",
        "target_coefficients",
        "secret_seed",
        "coefficients",
    }
    injected_keys = sorted(list(forbidden_keys.intersection(payload.keys())))
    if injected_keys:
        return error_response(
            f"Client-supplied ground truth ({', '.join(injected_keys)}) is prohibited. "
            "Measurements must reference registered scenarios or existing runs.",
            400,
        )

    db = _get_db()
    service = PerformanceMeasurementService()

    # Option 1: Measure existing discovery run
    if "run_id" in payload:
        run_id = payload["run_id"]
        if not isinstance(run_id, str) or not run_id.strip():
            return error_response("Field 'run_id' must be a non-empty string.", 400)

        scenario_id = payload.get("scenario_id")
        try:
            with db.session_scope() as session:
                measurement = service.measure_run(
                    session=session,
                    run_id=run_id.strip(),
                    scenario_id=scenario_id.strip() if scenario_id else None,
                )
                measurement_dict = measurement.to_dict()
                verify_no_ground_truth_leakage(measurement_dict)
                return jsonify({
                    "run_id": run_id,
                    "measurement_id": measurement.measurement_id,
                    "measurement": measurement_dict,
                }), 201
        except ValueError as exc:
            return error_response(str(exc), 404 if "not found" in str(exc).lower() else 400)
        except Exception as exc:  # noqa: BLE001
            log_safe_error("trigger_performance_measurement run error", exc)
            return error_response("An internal error occurred while measuring run performance.", 500)

    # Option 2: Execute measured benchmark comparison
    scenario_id = payload.get("scenario_id") or payload.get("benchmark_case")
    if not scenario_id or not isinstance(scenario_id, str) or not scenario_id.strip():
        return error_response(
            "Either 'run_id' or 'scenario_id' is required for performance measurement.",
            400,
        )

    scenario = get_benchmark_case(scenario_id.strip())
    if not scenario:
        return error_response(f"Unknown benchmark scenario '{scenario_id}'.", 400)

    seeds_input = payload.get("seeds", [42])
    if not isinstance(seeds_input, list) or not seeds_input:
        return error_response("Field 'seeds' must be a non-empty list of integers.", 400)

    seeds: List[int] = []
    for s in seeds_input:
        if not isinstance(s, int) or isinstance(s, bool):
            return error_response(f"Invalid seed '{s}'; all seeds must be integers.", 400)
        seeds.append(s)

    if len(seeds) > 20:
        return error_response("Maximum of 20 seeds permitted per measurement request.", 400)

    max_budget = payload.get("max_query_budget")
    if max_budget is not None:
        if not isinstance(max_budget, int) or isinstance(max_budget, bool) or max_budget < 1 or max_budget > 100:
            return error_response("Field 'max_query_budget' must be an integer between 1 and 100.", 400)

    try:
        with db.session_scope() as session:
            run_rec, bm_rec, report = service.run_and_measure_benchmark(
                session=session,
                scenario_name=scenario.benchmark_name,
                seeds=seeds,
                max_budget=max_budget,
            )
            report_dict = report.to_dict()
            verify_no_ground_truth_leakage(report_dict)
            return jsonify({
                "run_id": run_rec.run_id,
                "benchmark_id": bm_rec.benchmark_id,
                "measurement_id": f"pm-{report.comparison_id}",
                "report": report_dict,
            }), 201
    except ValueError as exc:
        return error_response(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("trigger_performance_measurement benchmark error", exc)
        return error_response("An internal error occurred while executing benchmark measurement.", 500)


@benchmarks_v1_bp.route("/benchmarks/runs/<run_id>/measurements", methods=["GET"])
def get_run_performance_measurements(run_id: str):
    """GET /api/v1/benchmarks/runs/<run_id>/measurements — Extract performance measurements for a specific run."""
    from ...services.performance_measurement_service import PerformanceMeasurementService

    db = _get_db()
    service = PerformanceMeasurementService()

    try:
        with db.session_scope() as session:
            measurement = service.measure_run(session=session, run_id=run_id)
            measurement_dict = measurement.to_dict()
            verify_no_ground_truth_leakage(measurement_dict)
            return jsonify({
                "run_id": run_id,
                "measurement": measurement_dict,
            }), 200
    except ValueError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_run_performance_measurements error", exc)
        return error_response("An internal error occurred while retrieving run performance measurements.", 500)


@benchmarks_v1_bp.route("/benchmarks/measurements/<measurement_id>", methods=["GET"])
def get_performance_measurement_by_id(measurement_id: str):
    """GET /api/v1/benchmarks/measurements/<measurement_id> — Retrieve performance measurement record by ID."""
    db = _get_db()
    # Normalize ID matching
    target_bm_id = measurement_id
    if measurement_id.startswith("pm-"):
        target_bm_id = f"bm-{measurement_id}"
    elif not measurement_id.startswith("bm-"):
        target_bm_id = f"bm-{measurement_id}"

    try:
        with db.session_scope() as session:
            from ...adapters.persistence.models import BenchmarkResultModel
            from sqlalchemy import select, or_

            stmt = select(BenchmarkResultModel).where(
                or_(
                    BenchmarkResultModel.benchmark_id == measurement_id,
                    BenchmarkResultModel.benchmark_id == target_bm_id,
                    BenchmarkResultModel.benchmark_id == f"bm-pm-{measurement_id.removeprefix('pm-')}",
                    BenchmarkResultModel.run_id == measurement_id,
                )
            )
            bm = session.scalars(stmt).first()
            if not bm:
                return error_response(f"Performance measurement '{measurement_id}' not found.", 404)

            metadata = bm.execution_metadata or {}
            verify_no_ground_truth_leakage(metadata)
            return jsonify({
                "measurement_id": measurement_id,
                "benchmark_id": bm.benchmark_id,
                "run_id": bm.run_id,
                "scenario_id": bm.benchmark_name,
                "measurements": metadata,
            }), 200
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get_performance_measurement_by_id error", exc)
        return error_response("An internal error occurred while retrieving performance measurement.", 500)


@benchmarks_v1_bp.route("/benchmarks/measurements", methods=["GET"])
def list_performance_measurements():
    """GET /api/v1/benchmarks/measurements — List historical performance measurement summaries."""
    db = _get_db()
    scenario_id = request.args.get("scenario_id")
    limit = min(max(1, request.args.get("limit", 20, type=int)), 100)
    offset = max(0, request.args.get("offset", 0, type=int))

    try:
        with db.session_scope() as session:
            from ...adapters.persistence.models import BenchmarkResultModel
            from sqlalchemy import select, func

            stmt = select(BenchmarkResultModel).order_by(BenchmarkResultModel.executed_at.desc())
            if scenario_id:
                stmt = stmt.where(BenchmarkResultModel.benchmark_name == scenario_id)

            total_count = session.scalar(select(func.count(BenchmarkResultModel.benchmark_id))) or 0
            records = list(session.scalars(stmt.limit(limit).offset(offset)).all())

            items = []
            for r in records:
                meta = r.execution_metadata or {}
                verify_no_ground_truth_leakage(meta)
                items.append({
                    "benchmark_id": r.benchmark_id,
                    "run_id": r.run_id,
                    "scenario_id": r.benchmark_name,
                    "target_label": r.target_label,
                    "hit_at_1": r.hit_at_1,
                    "reciprocal_rank": r.reciprocal_rank,
                    "held_out_mae": r.held_out_mae,
                    "efficiency_gain": r.efficiency_gain,
                    "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                    "has_measurements": "metric_comparisons" in meta or "active_aggregates" in meta or "accuracy" in meta,
                })

            return jsonify({
                "measurements": items,
                "count": len(items),
                "total": total_count,
                "limit": limit,
                "offset": offset,
            }), 200
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list_performance_measurements error", exc)
        return error_response("An internal error occurred while listing performance measurements.", 500)

