"""Shared serializers, error helpers, and utilities for the v1 API.

All serialization uses explicit allowlists to prevent ORM internals,
oracle ground truth, or private evaluator details from leaking into responses.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from flask import jsonify

from ...adapters.persistence.models import (
    AuditEventModel,
    BenchmarkResultModel,
    ComputeTaskModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from ...adapters.persistence.repository import ClarityRepository
from ...services.budget_service import resolve_budget_config


# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

def error_response(
    message: str,
    status_code: int = 400,
    error: Optional[str] = None,
    details: Optional[Any] = None,
):
    """Return a consistent structured JSON error body."""
    if error is None:
        _map = {
            400: "Bad Request",
            404: "Not Found",
            409: "Conflict",
            422: "Unprocessable Entity",
            500: "Internal Server Error",
        }
        error = _map.get(status_code, "Error")
    body: Dict[str, Any] = {
        "error": error,
        "message": message,
        "status_code": status_code,
    }
    if details is not None:
        body["details"] = details
    return jsonify(body), status_code


# ---------------------------------------------------------------------------
# Serializers (explicit allowlists — no ORM internals)
# ---------------------------------------------------------------------------

def serialize_run(run: RunModel) -> Dict[str, Any]:
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
        "max_concurrent_remote_tasks": run.max_concurrent_remote_tasks,
        "max_generated_hypotheses": run.max_generated_hypotheses,
        "budget": resolve_budget_config(run).to_dict(),
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
    }


def iso_utc(value: Optional[datetime]) -> Optional[str]:
    """Serialize a timestamp as timezone-aware UTC ISO-8601.

    Timestamps are stored in UTC. Some backends (notably SQLite) return them
    without an offset, which would make API output ambiguous; those values are
    explicitly interpreted as UTC rather than left naive.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_revision(rev: HypothesisRevisionModel) -> Dict[str, Any]:
    """Serialize an immutable revision, including its provenance chain."""
    return {
        "revision_id": rev.revision_id,
        "revision_number": rev.revision_number,
        # Effective validated family recorded on the revision itself.
        "model_family": getattr(rev, "model_family", None),
        "formula": rev.formula,
        "parameters": rev.parameters,
        "complexity": rev.complexity,
        "creation_source": rev.creation_source,
        "previous_revision_id": getattr(rev, "previous_revision_id", None),
        "change_reason": getattr(rev, "change_reason", None),
        "actor": getattr(rev, "actor", None),
        "actor_id": getattr(rev, "actor_id", None),
        "created_at": iso_utc(rev.created_at),
    }


def serialize_evaluation(eval_res: EvaluationResultModel) -> Dict[str, Any]:
    """Serialize an evaluation result. Held-out metrics are included only when present.
    The oracle's hidden formula or ground-truth parameters are never included."""
    return {
        "evaluation_id": eval_res.evaluation_id,
        "revision_id": eval_res.revision_id,
        "status": eval_res.status,
        "sample_count": eval_res.sample_count,
        "dataset_version": eval_res.dataset_version,
        # Exact scientific input identity (Issue #17), distinct from the count.
        "dataset_snapshot_id": getattr(eval_res, "dataset_snapshot_id", None),
        "metrics": eval_res.metrics,
        # held_out_metrics may be None when not yet computed
        "held_out_metrics": eval_res.held_out_metrics,
        "error_message": eval_res.error_message,
        "evaluated_at": iso_utc(eval_res.evaluated_at),
    }


def serialize_hypothesis(
    hyp: HypothesisModel,
    repo: Optional[ClarityRepository] = None,
    include_revisions: bool = True,
) -> Dict[str, Any]:
    """Serialize a hypothesis with optional revision history and latest evaluation."""
    revisions = (
        sorted(hyp.revisions, key=lambda r: r.revision_number)
        if hyp.revisions
        else []
    )
    latest_rev = revisions[-1] if revisions else None

    latest_eval = None
    if latest_rev and repo:
        evals = repo.list_evaluations_for_revision(latest_rev.revision_id)
        if evals:
            latest_eval = serialize_evaluation(evals[0])

    result: Dict[str, Any] = {
        "hypothesis_id": hyp.hypothesis_id,
        "run_id": hyp.run_id,
        "model_family": hyp.model_family,
        "current_revision": hyp.current_revision,
        "version": getattr(hyp, "version", 1) or 1,
        "version_token": getattr(hyp, "version_token", None) or f"vt-{hyp.hypothesis_id}-1",
        "is_pinned": hyp.is_pinned,
        "is_excluded": hyp.is_excluded,
        "created_at": hyp.created_at.isoformat() if hyp.created_at else None,
        "updated_at": hyp.updated_at.isoformat() if hyp.updated_at else None,
        "latest_revision": serialize_revision(latest_rev) if latest_rev else None,
        "latest_evaluation": latest_eval,
    }

    if include_revisions:
        result["revisions"] = [serialize_revision(r) for r in revisions]
    return result


def serialize_observation(obs: ObservationModel) -> Dict[str, Any]:
    """Serialize an observation. The oracle's hidden formula is never included."""
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


def serialize_usage(usage: Optional[UsageRecordModel]) -> Dict[str, Any]:
    """Return a usage snapshot distinguishing measured zeros from unavailable values."""
    if usage is None:
        return {
            "queries_executed": None,
            "evaluations_scheduled": None,
            "evaluations_completed": None,
            "estimated_cost_usd": None,
            "live_accruing_cost_usd": None,
            "settled_cost_usd": None,
            "is_budget_exhausted": False,
            "exhaustion_reason": None,
            "recorded_at": None,
            "note": "No usage record available for this run.",
        }
    return {
        "queries_executed": usage.queries_executed,
        "evaluations_scheduled": usage.evaluations_scheduled,
        "evaluations_completed": usage.evaluations_completed,
        "estimated_cost_usd": usage.estimated_cost_usd,
        "live_accruing_cost_usd": usage.live_accruing_cost_usd,
        "settled_cost_usd": usage.settled_cost_usd,
        "is_budget_exhausted": usage.is_budget_exhausted,
        "exhaustion_reason": usage.exhaustion_reason,
        "recorded_at": usage.recorded_at.isoformat() if usage.recorded_at else None,
    }


def serialize_audit_event(event: AuditEventModel) -> Dict[str, Any]:
    """Serialize an immutable audit event with its structured provenance.

    ``before`` and ``after`` are ``None`` when the action has no relevant state
    snapshot of that side, which is distinct from an empty object ``{}``.
    """
    return {
        "event_id": event.event_id,
        "run_id": event.run_id,
        "event_type": event.event_type,
        "actor": event.actor,
        "actor_id": getattr(event, "actor_id", None),
        "hypothesis_id": getattr(event, "hypothesis_id", None),
        "revision_id": getattr(event, "revision_id", None),
        "correlation_id": getattr(event, "correlation_id", None),
        "before": getattr(event, "before_json", None),
        "after": getattr(event, "after_json", None),
        "reason": getattr(event, "reason", None),
        "corrects_event_id": getattr(event, "corrects_event_id", None),
        "metadata": event.metadata_json or {},
        "timestamp": iso_utc(event.timestamp),
    }


def serialize_task(task: ComputeTaskModel) -> Dict[str, Any]:
    """Serialize a compute task record."""
    return {
        "task_id": task.task_id,
        "run_id": task.run_id,
        "candidate_id": task.candidate_id,
        "execution_mode": task.execution_mode,
        "provider_task_id": task.provider_task_id,
        "status": task.status,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "duration_ms": task.duration_ms,
        "error_metadata": sanitize_metadata(task.error_metadata) if task.error_metadata else None,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


# ---------------------------------------------------------------------------
# Safe structured logging (redacts DSN credentials, secrets, passwords)
# ---------------------------------------------------------------------------

from ...domain.sanitization import sanitize_exception_text, sanitize_metadata


def log_safe_error(context: str, exc: Exception, logger=None) -> None:
    """Log an exception safely using structured logging with redacted credentials and parameters."""
    if logger is None:
        from flask import current_app
        logger = current_app.logger

    exc_type = type(exc).__name__
    safe_msg = sanitize_exception_text(str(exc))

    if len(safe_msg) > 500:
        safe_msg = safe_msg[:500] + "... [truncated]"

    logger.error(
        "%s | exception_type=%s | details=%s",
        context,
        exc_type,
        safe_msg,
    )
