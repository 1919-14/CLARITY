"""Scientific report service for CLARITY (Issue #23).

Coordinates report generation, persistence, retrieval, and export.
Preserves historical reports without silent modification.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
import uuid

from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    CandidateExperimentModel,
    EvaluationResultModel,
    HypothesisModel,
    ObservationModel,
    ReportModel,
    RunModel,
    UsageRecordModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.contracts import (
    EvaluationResult,
    EvaluationStatus,
    HypothesisProvenance,
    HypothesisRepresentation,
    HypothesisStatus,
    Observation,
    ObservationStatus,
    StructuredError,
)
from ..domain.ranking_contracts import RankingPolicyConfig
from ..domain.report_contracts import (
    REPORT_SCHEMA_VERSION,
    BudgetAndExecutionSection,
    ComplexityAndUncertaintySection,
    DiscoveryProcessSection,
    EvaluationMethodologySection,
    EvidenceItem,
    NextExperimentRecommendation,
    ReportHypothesis,
    ReportStatus,
    RunSummarySection,
    ScientificReport,
)
from ..domain.report_exporters import (
    export_report_as_json,
    export_report_as_markdown,
)
from ..domain.report_generator import generate_scientific_report
from ..domain.sanitization import sanitize_metadata
from ..domain.run_lifecycle import RunNotFoundError


class ReportServiceError(Exception):
    """Base exception for report service operations."""


class ReportNotFoundError(ReportServiceError):
    """Raised when a report is requested for a run that has no generated report."""


class ReportNotAvailableError(ReportServiceError):
    """Raised when a run cannot produce a report due to insufficient state."""


REPORTS_STORAGE_DIR = os.environ.get("CLARITY_REPORTS_DIR", "reports")
SAFE_IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z0-9_\-]+$")


def _validate_safe_identifier(name: str, field_name: str = "identifier") -> str:
    """Validate that an identifier contains no path traversal sequences or illegal characters."""
    if not name or not isinstance(name, str):
        raise ValueError(f"Invalid {field_name}: must be a non-empty string.")
    if ".." in name or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError(f"Path traversal detected in {field_name}: '{name}'.")
    if not SAFE_IDENTIFIER_PATTERN.match(name):
        raise ValueError(
            f"Invalid characters in {field_name} '{name}': must be alphanumeric, hyphen, or underscore."
        )
    return name


def _save_report_artifact(run_id: str, report_id: str, content: str) -> str:
    """Save report JSON artifact to disk and return relative artifact URI."""
    _validate_safe_identifier(run_id, "run_id")
    _validate_safe_identifier(report_id, "report_id")

    base_dir = os.path.abspath(REPORTS_STORAGE_DIR)
    filename = f"{run_id}_{report_id}.json"
    filepath = os.path.abspath(os.path.join(base_dir, filename))

    # Path traversal safety check: ensure target filepath is strictly within base_dir
    if not filepath.startswith(base_dir + os.sep) and filepath != base_dir:
        raise ValueError(f"Path traversal detected: path '{filepath}' is outside storage dir '{base_dir}'.")

    try:
        os.makedirs(base_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        return f"reports/{filename}"
    except (OSError, IOError):
        # Fall back to virtual relative URI if filesystem write fails (e.g. read-only env)
        return f"reports/{filename}"


def _load_report_artifact(artifact_uri: str) -> Optional[Dict[str, Any]]:
    """Attempt to load report JSON from disk artifact URI."""
    if not artifact_uri:
        return None
    try:
        norm_path = artifact_uri
        if norm_path.startswith("reports/"):
            rel_name = norm_path[len("reports/"):]
        else:
            rel_name = norm_path

        # Validate that rel_name does not traverse directories
        if ".." in rel_name or "/" in rel_name or "\\" in rel_name or "\x00" in rel_name:
            return None

        base_dir = os.path.abspath(REPORTS_STORAGE_DIR)
        full_path = os.path.abspath(os.path.join(base_dir, rel_name))
        if not full_path.startswith(base_dir + os.sep):
            return None

        if os.path.isfile(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def generate_report_service(
    session: Session,
    run_id: str,
    force_recompute: bool = False,
    actor: str = "researcher",
) -> Tuple[ReportModel, ScientificReport]:
    """Generate and persist a scientific report for a discovery run.

    Idempotency:
    If a generated report already exists and force_recompute is False,
    returns the existing report to preserve historical provenance.
    """
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise RunNotFoundError(f"Run '{run_id}' not found.")

    # Idempotency check: preserve existing generated report unless forced
    existing_report = repo.get_latest_report_for_run(run_id)
    if existing_report and existing_report.status == "generated" and not force_recompute:
        loaded = _load_report_artifact(existing_report.artifact_uri)
        if loaded:
            report_obj = _dict_to_scientific_report(loaded)
            return existing_report, report_obj
        if existing_report.report_json:
            report_obj = _dict_to_scientific_report(existing_report.report_json)
            return existing_report, report_obj
        # Fail explicitly rather than silently replacing historical reports
        raise ReportNotAvailableError(
            f"Historical report '{existing_report.report_id}' exists for run '{run_id}' but its artifact cannot be loaded. Historical reports are immutable and cannot be silently regenerated."
        )

    def _canonical_parameters(family_id: str, raw_params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        params = dict(raw_params or {})
        if family_id == "polynomial":
            params.setdefault("family", "polynomial")
            params.setdefault("input_variable", "x")
            params.setdefault("output_variable", "y")
            params.setdefault("coefficient_order", "ascending")
            if "degree" not in params and "coefficients" in params:
                params["degree"] = max(0, len(params["coefficients"]) - 1)
        return params

    # 1. Load domain observations
    sorted_obs = sorted(run.observations, key=lambda o: (o.sequence_number or 0, o.observation_id))
    domain_observations: List[Observation] = []
    training_ids: List[str] = []
    held_out_ids: List[str] = []
    for o in sorted_obs:
        obs_status = (
            ObservationStatus.SUCCESS
            if o.status == "success"
            else ObservationStatus.ORACLE_FAILURE
        )
        if o.is_held_out:
            held_out_ids.append(o.observation_id)
        else:
            training_ids.append(o.observation_id)
        domain_observations.append(
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

    # 2. Select deterministic latest eligible evaluation per hypothesis and bind exact revision
    hypotheses: List[HypothesisRepresentation] = []
    evaluations_by_hyp: Dict[str, EvaluationResult] = {}

    for hyp in run.hypotheses:
        if hyp.is_excluded:
            continue
        revisions = hyp.revisions
        if not revisions:
            continue

        # Collect all (revision, evaluation) pairs
        eval_pairs = []
        for rev in revisions:
            for ev in rev.evaluations:
                eval_pairs.append((rev, ev))

        # Deterministic ordering:
        # 1. Latest revision number
        # 2. Completed evaluation status preferred
        # 3. Latest dataset version
        # 4. Latest creation timestamp
        # 5. Evaluation ID tie-break
        if eval_pairs:
            eval_pairs.sort(
                key=lambda pair: (
                    pair[0].revision_number,
                    1 if pair[1].status == "completed" else 0,
                    pair[1].dataset_version or 0,
                    pair[1].evaluated_at.timestamp() if getattr(pair[1], "evaluated_at", None) else 0.0,
                    pair[1].evaluation_id,
                ),
                reverse=True,
            )
            selected_rev, selected_ev = eval_pairs[0]
        else:
            selected_rev = max(revisions, key=lambda r: (r.revision_number, r.revision_id))
            selected_ev = None

        # Bind formula, parameters, and complexity to the EXACT selected revision
        selected_params = _canonical_parameters(hyp.model_family, selected_rev.parameters)
        candidate_repr = HypothesisRepresentation(
            model_family=hyp.model_family,
            formula=selected_rev.formula or f"Candidate {hyp.hypothesis_id}",
            parameters=selected_params,
            complexity=selected_rev.complexity or 1,
            hypothesis_id=hyp.hypothesis_id,
            revision=selected_rev.revision_number,
            status=HypothesisStatus.EVALUATED if selected_ev else HypothesisStatus.CANDIDATE,
        )
        hypotheses.append(candidate_repr)

        if selected_ev is not None:
            status_enum = (
                EvaluationStatus.COMPLETED
                if selected_ev.status == "completed"
                else EvaluationStatus.FAILED
            )
            err = (
                StructuredError(code="EVALUATION_FAILED", message=selected_ev.error_message)
                if selected_ev.error_message
                else (
                    StructuredError(code="EVALUATION_FAILED", message="Evaluation failed")
                    if status_enum == EvaluationStatus.FAILED
                    else None
                )
            )
            eval_res = EvaluationResult(
                evaluation_id=selected_ev.evaluation_id,
                hypothesis_id=hyp.hypothesis_id,
                status=status_enum,
                metrics=selected_ev.metrics or {},
                held_out_metrics=selected_ev.held_out_metrics,
                sample_count=selected_ev.sample_count or len(training_ids),
                held_out_sample_count=len(held_out_ids) if selected_ev.held_out_metrics else None,
                error=err,
                model_family=hyp.model_family,
                fitted_specification=selected_params,
                metric_version="1.0.0",
                metric_definitions={
                    "mae": "Mean Absolute Error",
                    "rmse": "Root Mean Squared Error",
                    "r2": "R2 Score",
                },
                dataset_ids={
                    "dataset_id": run_id,
                    "dataset_version": selected_ev.dataset_version,
                    "observation_ids": training_ids,
                    "training_observation_ids": training_ids,
                    "held_out_observation_ids": held_out_ids,
                },
            )
            evaluations_by_hyp[hyp.hypothesis_id] = eval_res

    # Candidate Experiments
    experiments_data: List[Dict[str, Any]] = []
    for exp in run.experiments:
        experiments_data.append({
            "experiment_id": exp.experiment_id,
            "proposed_input": exp.proposed_input,
            "selection_rationale": exp.selection_rationale,
            "execution_status": exp.execution_status,
        })

    # Budget Limits & Usage
    budget_limits = {
        "max_queries": run.max_queries,
        "max_evaluations": run.max_evaluations,
        "max_runtime_seconds": run.max_runtime_seconds,
        "spend_ceiling_usd": run.spend_ceiling_usd,
    }

    usage_data: Optional[Dict[str, Any]] = None
    if run.usage_records:
        latest_usage = run.usage_records[-1]
        usage_data = {
            "queries_executed": latest_usage.queries_executed,
            "evaluations_scheduled": latest_usage.evaluations_scheduled,
            "evaluations_completed": latest_usage.evaluations_completed,
            "estimated_cost_usd": latest_usage.estimated_cost_usd,
            "live_accruing_cost_usd": latest_usage.live_accruing_cost_usd,
            "settled_cost_usd": latest_usage.settled_cost_usd,
            "is_budget_exhausted": latest_usage.is_budget_exhausted,
        }

    # Generate the structured report
    report_id = f"rep-{uuid.uuid4().hex[:12]}"
    scientific_report = generate_scientific_report(
        run_id=run.run_id,
        run_name=run.name,
        domain=run.domain,
        execution_mode=run.execution_mode,
        sampling_strategy=run.sampling_strategy,
        run_status=run.status,
        stop_reason=run.stop_reason,
        created_at=run.created_at.isoformat() if run.created_at else None,
        started_at=run.started_at.isoformat() if run.started_at else None,
        completed_at=run.completed_at.isoformat() if run.completed_at else None,
        budget_limits=budget_limits,
        usage_data=usage_data,
        candidates=hypotheses,
        evaluations_by_hypothesis=evaluations_by_hyp,
        observations=domain_observations,
        candidate_experiments=experiments_data,
        ranking_policy=RankingPolicyConfig(
            allow_unverified_comparability=True,
            min_samples_for_plausibility=2,
            performance_acceptance_threshold=0.1,
        ),
        report_id=report_id,
    )

    # 2. Persist artifact to disk
    json_export = export_report_as_json(scientific_report)
    artifact_uri = _save_report_artifact(run.run_id, report_id, json_export)

    # Update report object with resolved artifact_uri
    scientific_report = ScientificReport(
        report_id=scientific_report.report_id,
        run_id=scientific_report.run_id,
        status=scientific_report.status,
        schema_version=scientific_report.schema_version,
        generated_at=scientific_report.generated_at,
        run_summary=scientific_report.run_summary,
        discovery_process=scientific_report.discovery_process,
        evaluation_methodology=scientific_report.evaluation_methodology,
        ranked_hypotheses=scientific_report.ranked_hypotheses,
        complexity_and_uncertainty=scientific_report.complexity_and_uncertainty,
        budget_and_execution=scientific_report.budget_and_execution,
        limitations=scientific_report.limitations,
        next_experiment=scientific_report.next_experiment,
        conclusion=scientific_report.conclusion,
        artifact_uri=artifact_uri,
    )

    # 3. Save ReportModel in DB
    report_model = ReportModel(
        report_id=report_id,
        run_id=run.run_id,
        status="generated",
        ranked_candidates=[h.to_dict() for h in scientific_report.ranked_hypotheses],
        stop_reason=run.stop_reason,
        limitations="; ".join(scientific_report.limitations),
        artifact_uri=artifact_uri,
        generated_at=datetime.now(timezone.utc),
        report_json=scientific_report.to_dict(),
    )
    repo.save_report(report_model)

    # 4. Record audit event
    try:
        audit_event = AuditEventModel(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            run_id=run.run_id,
            event_type="REPORT_GENERATED",
            actor=actor,
            metadata_json={
                "report_id": report_id,
                "artifact_uri": artifact_uri,
                "ranked_count": len(scientific_report.ranked_hypotheses),
                "is_partial": scientific_report.run_summary.is_partial,
            },
        )
        repo.record_audit_event(audit_event)
    except Exception:
        pass

    session.flush()
    return report_model, scientific_report


def get_report_service(
    session: Session,
    run_id: str,
) -> Optional[ReportModel]:
    """Retrieve the latest ReportModel for a run, or None if pending."""
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise RunNotFoundError(f"Run '{run_id}' not found.")
    return repo.get_latest_report_for_run(run_id)


def export_report_service(
    session: Session,
    run_id: str,
    format_type: str = "json",
    actor: str = "researcher",
) -> Tuple[str, str]:
    """Export the scientific report for a run.

    Returns:
      (content_string, mime_type)
    """
    repo = ClarityRepository(session)
    run = repo.get_run(run_id)
    if not run:
        raise RunNotFoundError(f"Run '{run_id}' not found.")

    report_model = repo.get_latest_report_for_run(run_id)
    if not report_model:
        # Automatically generate report if none exists
        report_model, report_obj = generate_report_service(session, run_id, actor=actor)
    else:
        # Load structured report from disk or fallback to canonical DB payload
        loaded = _load_report_artifact(report_model.artifact_uri)
        if loaded:
            report_obj = _dict_to_scientific_report(loaded)
        elif report_model.report_json:
            report_obj = _dict_to_scientific_report(report_model.report_json)
        else:
            raise ReportNotAvailableError(
                f"Historical report '{report_model.report_id}' exists for run '{run_id}' but its artifact cannot be loaded. Historical reports are immutable and cannot be silently regenerated."
            )

    format_norm = format_type.lower().strip()
    if format_norm in ("markdown", "md", "text/markdown"):
        content = export_report_as_markdown(report_obj)
        mime = "text/markdown; charset=utf-8"
    else:
        content = export_report_as_json(report_obj)
        mime = "application/json"

    # Record export audit event
    try:
        audit_event = AuditEventModel(
            event_id=f"evt-{uuid.uuid4().hex[:12]}",
            run_id=run_id,
            event_type="REPORT_EXPORTED",
            actor=actor,
            metadata_json={
                "report_id": report_model.report_id,
                "format": format_norm,
            },
        )
        repo.record_audit_event(audit_event)
        session.flush()
    except Exception:
        pass

    return content, mime


def _dict_to_scientific_report(d: Dict[str, Any]) -> ScientificReport:
    """Helper reconstructing a ScientificReport dataclass from stored JSON."""
    s = d.get("run_summary", {})
    run_summary = RunSummarySection(
        run_id=s.get("run_id", ""),
        name=s.get("name", ""),
        domain=s.get("domain", ""),
        execution_mode=s.get("execution_mode", ""),
        sampling_strategy=s.get("sampling_strategy", ""),
        status=s.get("status", ""),
        stop_reason=s.get("stop_reason"),
        is_partial=bool(s.get("is_partial", False)),
        created_at=s.get("created_at"),
        started_at=s.get("started_at"),
        completed_at=s.get("completed_at"),
        duration_seconds=s.get("duration_seconds"),
    )

    dp = d.get("discovery_process", {})
    discovery_process = DiscoveryProcessSection(
        oracle_queries_used=dp.get("oracle_queries_used", 0),
        candidate_evaluations_performed=dp.get("candidate_evaluations_performed", 0),
        hypotheses_generated=dp.get("hypotheses_generated", 0),
        search_progress=dp.get("search_progress", ""),
        resource_usage=dp.get("resource_usage", {}),
    )

    m = d.get("evaluation_methodology", {})
    methodology = EvaluationMethodologySection(
        primary_metric=m.get("primary_metric", "mae"),
        metric_definitions=m.get("metric_definitions", {}),
        held_out_evaluation_approach=m.get("held_out_evaluation_approach", ""),
        dataset_version=m.get("dataset_version"),
        ranking_criteria=m.get("ranking_criteria", ""),
    )

    ranked_hypotheses: List[ReportHypothesis] = []
    for h in d.get("ranked_hypotheses", []):
        sup_items = [
            EvidenceItem(
                observation_id=ei.get("observation_id", ""),
                sequence_number=ei.get("sequence_number"),
                input_values=ei.get("input_values", {}),
                observed_value=ei.get("observed_value"),
                predicted_value=ei.get("predicted_value"),
                residual=ei.get("residual"),
                is_held_out=bool(ei.get("is_held_out", False)),
                tolerance=ei.get("tolerance"),
            )
            for ei in h.get("supporting_evidence", [])
        ]
        contra_items = [
            EvidenceItem(
                observation_id=ei.get("observation_id", ""),
                sequence_number=ei.get("sequence_number"),
                input_values=ei.get("input_values", {}),
                observed_value=ei.get("observed_value"),
                predicted_value=ei.get("predicted_value"),
                residual=ei.get("residual"),
                is_held_out=bool(ei.get("is_held_out", False)),
                tolerance=ei.get("tolerance"),
            )
            for ei in h.get("contradicting_evidence", [])
        ]
        ranked_hypotheses.append(
            ReportHypothesis(
                rank=h.get("rank", 1),
                within_cohort_rank=h.get("within_cohort_rank"),
                hypothesis_id=h.get("hypothesis_id", ""),
                name=h.get("name", ""),
                model_family=h.get("model_family", ""),
                formula=h.get("formula", ""),
                parameters=h.get("parameters", {}),
                revision_number=h.get("revision_number", 1),
                evaluation_id=h.get("evaluation_id"),
                status=h.get("status", "uncertain"),
                evaluation_tier=h.get("evaluation_tier", "unevaluated"),
                primary_metric_name=h.get("primary_metric_name", "mae"),
                held_out_score=h.get("held_out_score"),
                training_score=h.get("training_score"),
                complexity_score=h.get("complexity_score"),
                dataset_id=h.get("dataset_id"),
                dataset_version=h.get("dataset_version"),
                explanation=h.get("explanation", ""),
                supporting_evidence=tuple(sup_items),
                contradicting_evidence=tuple(contra_items),
                indeterminate_count=h.get("indeterminate_count", 0),
                uncertainty_and_limitations=tuple(h.get("uncertainty_and_limitations", [])),
            )
        )

    cu = d.get("complexity_and_uncertainty", {})
    complexity = ComplexityAndUncertaintySection(
        complexity_metric=cu.get("complexity_metric", ""),
        complexity_definition=cu.get("complexity_definition", ""),
        uncertainty_status=cu.get("uncertainty_status", ""),
        uncertainty_disclosures=tuple(cu.get("uncertainty_disclosures", [])),
    )

    be = d.get("budget_and_execution", {})
    budget_exec = BudgetAndExecutionSection(
        configured_limits=be.get("configured_limits", {}),
        measured_usage=be.get("measured_usage", {}),
        cost=be.get("cost", {}),
    )

    ne = d.get("next_experiment", {})
    next_exp = NextExperimentRecommendation(
        recommended=bool(ne.get("recommended", False)),
        experiment_id=ne.get("experiment_id"),
        input_values=ne.get("input_values"),
        justification=ne.get("justification", ""),
        selection_strategy=ne.get("selection_strategy"),
        predicted_disagreement=ne.get("predicted_disagreement"),
    )

    return ScientificReport(
        report_id=d.get("report_id", ""),
        run_id=d.get("run_id", ""),
        status=ReportStatus(d.get("status", "generated")),
        schema_version=d.get("schema_version", REPORT_SCHEMA_VERSION),
        generated_at=d.get("generated_at", ""),
        artifact_uri=d.get("artifact_uri"),
        run_summary=run_summary,
        discovery_process=discovery_process,
        evaluation_methodology=methodology,
        ranked_hypotheses=tuple(ranked_hypotheses),
        complexity_and_uncertainty=complexity,
        budget_and_execution=budget_exec,
        limitations=tuple(d.get("limitations", [])),
        next_experiment=next_exp,
        conclusion=d.get("conclusion", ""),
    )
