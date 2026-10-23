"""Shared serializers, error helpers, and utilities for the v1 API.

All serialization uses explicit allowlists to prevent ORM internals,
oracle ground truth, or private evaluator details from leaking into responses.
"""

from typing import Any, Dict, Optional
from flask import jsonify

from ...adapters.persistence.models import (
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


def serialize_revision(rev: HypothesisRevisionModel) -> Dict[str, Any]:
    return {
        "revision_id": rev.revision_id,
        "revision_number": rev.revision_number,
        "formula": rev.formula,
        "parameters": rev.parameters,
        "complexity": rev.complexity,
        "creation_source": rev.creation_source,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


def serialize_evaluation(eval_res: EvaluationResultModel) -> Dict[str, Any]:
    """Serialize an evaluation result. Held-out metrics are included only when present.
    The oracle's hidden formula or ground-truth parameters are never included."""
    return {
        "evaluation_id": eval_res.evaluation_id,
        "status": eval_res.status,
        "sample_count": eval_res.sample_count,
        "dataset_version": eval_res.dataset_version,
        "metrics": eval_res.metrics,
        # held_out_metrics may be None when not yet computed
        "held_out_metrics": eval_res.held_out_metrics,
        "error_message": eval_res.error_message,
        "evaluated_at": eval_res.evaluated_at.isoformat() if eval_res.evaluated_at else None,
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


# ---------------------------------------------------------------------------
# Safe structured logging (redacts DSN credentials, secrets, passwords)
# ---------------------------------------------------------------------------

import re

_REDACTION_PATTERNS = [
    # Redact URI credentials: ://user:password@ -> ://user:***@
    (re.compile(r"://([^:@/\s]+):([^@/\s]+)@", re.IGNORECASE), r"://\1:***@"),
    # Redact password/token/key in query strings: ?password=secret or &token=secret
    (re.compile(r"([?&](?:password|token|api_key|secret)=)[^&\s]+", re.IGNORECASE), r"\1***"),
    # Redact password / secret assignments with quotes or whitespace: password='xyz' or password="xyz"
    (re.compile(r"""(password|passwd|secret|token|api_key|ground_truth)['"]?\s*[:=]\s*(?:'[^']*'|"[^"]*"|[^\s,;)]+)""", re.IGNORECASE), r"\1=***"),
    # Redact parameters / DSN key-value pairs: pwd=..., secret=...
    (re.compile(r"\b(pwd|password)\s*=\s*[^\s,)]+", re.IGNORECASE), r"\1=***"),
]


def sanitize_exception_text(text: str) -> str:
    """Redact connection strings, credentials, and sensitive parameters from exception messages."""
    sanitized = str(text)
    for pattern, replacement in _REDACTION_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


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
