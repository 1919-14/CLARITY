"""Domain contracts and data structures for Scientific Reports (Issue #23).

Defines the structured schema for scientific report generation and export.
No database access, network calls, or secret handling occurs here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Tuple

REPORT_SCHEMA_VERSION = "1.0.0"


class ReportStatus(str, Enum):
    """Lifecycle status of a scientific report."""
    PENDING = "pending"
    GENERATED = "generated"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EvidenceItem:
    """An empirical observation reference serving as supporting or contradicting evidence."""
    observation_id: str
    sequence_number: Optional[int]
    input_values: Dict[str, Any]
    observed_value: Optional[float]
    predicted_value: Optional[float]
    residual: Optional[float]
    is_held_out: bool
    tolerance: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "sequence_number": self.sequence_number,
            "input_values": dict(self.input_values),
            "observed_value": self.observed_value,
            "predicted_value": self.predicted_value,
            "residual": self.residual,
            "is_held_out": self.is_held_out,
            "tolerance": self.tolerance,
        }


@dataclass(frozen=True)
class ReportHypothesis:
    """A ranked candidate hypothesis included in the scientific report."""
    rank: int
    within_cohort_rank: Optional[int]
    hypothesis_id: str
    name: str
    model_family: str
    formula: str
    parameters: Dict[str, Any]
    revision_number: int
    evaluation_id: Optional[str]
    status: str
    evaluation_tier: str
    primary_metric_name: str
    held_out_score: Optional[float]
    training_score: Optional[float]
    complexity_score: Optional[int]
    dataset_id: Optional[str]
    dataset_version: Optional[int]
    explanation: str
    supporting_evidence: Tuple[EvidenceItem, ...]
    contradicting_evidence: Tuple[EvidenceItem, ...]
    indeterminate_count: int
    uncertainty_and_limitations: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rank": self.rank,
            "within_cohort_rank": self.within_cohort_rank,
            "hypothesis_id": self.hypothesis_id,
            "name": self.name,
            "model_family": self.model_family,
            "formula": self.formula,
            "parameters": dict(self.parameters),
            "revision_number": self.revision_number,
            "evaluation_id": self.evaluation_id,
            "status": self.status,
            "evaluation_tier": self.evaluation_tier,
            "primary_metric_name": self.primary_metric_name,
            "held_out_score": self.held_out_score,
            "training_score": self.training_score,
            "complexity_score": self.complexity_score,
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "explanation": self.explanation,
            "supporting_evidence": [e.to_dict() for e in self.supporting_evidence],
            "contradicting_evidence": [e.to_dict() for e in self.contradicting_evidence],
            "indeterminate_count": self.indeterminate_count,
            "uncertainty_and_limitations": list(self.uncertainty_and_limitations),
        }


@dataclass(frozen=True)
class RunSummarySection:
    """High-level summary of the run configuration and execution state."""
    run_id: str
    name: str
    domain: str
    execution_mode: str
    sampling_strategy: str
    status: str
    stop_reason: Optional[str]
    is_partial: bool
    created_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    duration_seconds: Optional[float]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "domain": self.domain,
            "execution_mode": self.execution_mode,
            "sampling_strategy": self.sampling_strategy,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "is_partial": self.is_partial,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "duration_seconds": self.duration_seconds,
        }


@dataclass(frozen=True)
class DiscoveryProcessSection:
    """Summary of discovery progress, query counts, and resource usage."""
    oracle_queries_used: int
    candidate_evaluations_performed: int
    hypotheses_generated: int
    search_progress: str
    resource_usage: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "oracle_queries_used": self.oracle_queries_used,
            "candidate_evaluations_performed": self.candidate_evaluations_performed,
            "hypotheses_generated": self.hypotheses_generated,
            "search_progress": self.search_progress,
            "resource_usage": dict(self.resource_usage),
        }


@dataclass(frozen=True)
class EvaluationMethodologySection:
    """Documentation of evaluation metrics, splits, dataset versions, and ranking criteria."""
    primary_metric: str
    metric_definitions: Dict[str, str]
    held_out_evaluation_approach: str
    dataset_version: Optional[int]
    ranking_criteria: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_metric": self.primary_metric,
            "metric_definitions": dict(self.metric_definitions),
            "held_out_evaluation_approach": self.held_out_evaluation_approach,
            "dataset_version": self.dataset_version,
            "ranking_criteria": self.ranking_criteria,
        }


@dataclass(frozen=True)
class ComplexityAndUncertaintySection:
    """Definitions of model complexity and explicit disclosures of uncertainty limitations."""
    complexity_metric: str
    complexity_definition: str
    uncertainty_status: str
    uncertainty_disclosures: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "complexity_metric": self.complexity_metric,
            "complexity_definition": self.complexity_definition,
            "uncertainty_status": self.uncertainty_status,
            "uncertainty_disclosures": list(self.uncertainty_disclosures),
        }


@dataclass(frozen=True)
class BudgetAndExecutionSection:
    """Summary of configured budget limits and measured usage."""
    configured_limits: Dict[str, Any]
    measured_usage: Dict[str, Any]
    cost: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "configured_limits": dict(self.configured_limits),
            "measured_usage": dict(self.measured_usage),
            "cost": dict(self.cost),
        }


@dataclass(frozen=True)
class NextExperimentRecommendation:
    """Candidate experiment or query recommended by the planner to distinguish top hypotheses."""
    recommended: bool
    experiment_id: Optional[str]
    input_values: Optional[Dict[str, Any]]
    justification: str
    selection_strategy: Optional[str]
    predicted_disagreement: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recommended": self.recommended,
            "experiment_id": self.experiment_id,
            "input_values": dict(self.input_values) if self.input_values is not None else None,
            "justification": self.justification,
            "selection_strategy": self.selection_strategy,
            "predicted_disagreement": self.predicted_disagreement,
        }


@dataclass(frozen=True)
class ScientificReport:
    """Complete, immutable scientific discovery report."""
    report_id: str
    run_id: str
    status: ReportStatus
    schema_version: str
    generated_at: str
    run_summary: RunSummarySection
    discovery_process: DiscoveryProcessSection
    evaluation_methodology: EvaluationMethodologySection
    ranked_hypotheses: Tuple[ReportHypothesis, ...]
    complexity_and_uncertainty: ComplexityAndUncertaintySection
    budget_and_execution: BudgetAndExecutionSection
    limitations: Tuple[str, ...]
    next_experiment: NextExperimentRecommendation
    conclusion: str
    artifact_uri: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "report_id": self.report_id,
            "run_id": self.run_id,
            "status": self.status.value,
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "artifact_uri": self.artifact_uri,
            "run_summary": self.run_summary.to_dict(),
            "discovery_process": self.discovery_process.to_dict(),
            "evaluation_methodology": self.evaluation_methodology.to_dict(),
            "ranked_hypotheses": [h.to_dict() for h in self.ranked_hypotheses],
            "complexity_and_uncertainty": self.complexity_and_uncertainty.to_dict(),
            "budget_and_execution": self.budget_and_execution.to_dict(),
            "limitations": list(self.limitations),
            "next_experiment": self.next_experiment.to_dict(),
            "conclusion": self.conclusion,
        }
