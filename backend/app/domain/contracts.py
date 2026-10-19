"""Core scientific data contracts and interfaces for CLARITY.

Defines schemas and domain objects for:
- Experiment inputs and Oracle observations
- Hypothesis representations and evaluation results
- Oracle abstract base class and query errors
- Evaluator-only ground truth and benchmark scoring
- Serialization utilities ensuring strict boundary isolation
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union
import uuid


def utc_now_iso() -> str:
    """Return current UTC time formatted as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def generate_id(prefix: str = "id") -> str:
    """Generate a unique identifier prefixed with a domain label."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ==============================================================================
# 1. Error Representation
# ==============================================================================

@dataclass(frozen=True)
class StructuredError:
    """Standardized error contract for validation and query failures."""
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StructuredError":
        return cls(
            code=data["code"],
            message=data["message"],
            details=data.get("details"),
        )


class OracleQueryError(Exception):
    """Exception raised when an oracle query cannot be completed."""

    def __init__(self, error: StructuredError):
        super().__init__(f"[{error.code}] {error.message}")
        self.error = error


# ==============================================================================
# 2. Experiment Input Contract
# ==============================================================================

@dataclass(frozen=True)
class ExperimentInput:
    """Validated input specification submitted to a black-box oracle.
    
    Fields:
      input_id: Unique identifier for this experiment proposal.
      values: Key-value dictionary of independent variable values (e.g., {"x": 2.5}).
      domain: Named system or function family (e.g., "1d_polynomial").
      metadata: Non-sensitive planning details (e.g., proposed by "active_disagreement").
    """
    values: Dict[str, Union[int, float, str, bool]]
    domain: str = "default"
    input_id: str = field(default_factory=lambda: generate_id("inp"))
    metadata: Optional[Dict[str, Any]] = None

    def __post_init__(self):
        if not self.input_id or not isinstance(self.input_id, str):
            raise ValueError("ExperimentInput input_id must be a non-empty string.")
        if not isinstance(self.values, dict) or not self.values:
            raise ValueError("ExperimentInput values must be a non-empty dictionary.")
        for k, v in self.values.items():
            if not isinstance(k, str):
                raise ValueError(f"ExperimentInput variable name must be str, got {type(k).__name__}")
            if not isinstance(v, (int, float, str, bool)):
                raise ValueError(
                    f"ExperimentInput value for '{k}' must be int, float, str, or bool; got {type(v).__name__}"
                )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "input_id": self.input_id,
            "values": self.values,
            "domain": self.domain,
        }
        if self.metadata is not None:
            data["metadata"] = self.metadata
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentInput":
        return cls(
            input_id=data.get("input_id", generate_id("inp")),
            values=data["values"],
            domain=data.get("domain", "default"),
            metadata=data.get("metadata"),
        )


# ==============================================================================
# 3. Observation Contract
# ==============================================================================

class ObservationStatus(str, Enum):
    SUCCESS = "success"
    INVALID_INPUT = "invalid_input"
    ORACLE_FAILURE = "oracle_failure"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True)
class Observation:
    """Observable record returned by an oracle query.
    
    Fields:
      observation_id: Unique identifier for this observation.
      input_id: The identifier of the input submitted.
      input_values: The values queried.
      output_values: Observed dependent variable values (e.g. {"y": 14.2}). None on failure.
      status: Observation status (success, invalid_input, oracle_failure, budget_exceeded).
      sequence_number: Step / query index in the run (1-indexed).
      timestamp: UTC ISO-8601 string when observed.
      error: Structured error if status is not SUCCESS.
      noise_metadata: Optional uncertainty/noise characteristics reported by simulator.
      is_held_out: Flag indicating whether this observation is reserved for held-out evaluation.
    """
    input_id: str
    input_values: Dict[str, Any]
    output_values: Optional[Dict[str, Any]] = None
    status: ObservationStatus = ObservationStatus.SUCCESS
    observation_id: str = field(default_factory=lambda: generate_id("obs"))
    sequence_number: Optional[int] = None
    timestamp: str = field(default_factory=utc_now_iso)
    error: Optional[StructuredError] = None
    noise_metadata: Optional[Dict[str, Any]] = None
    is_held_out: bool = False

    def __post_init__(self):
        if not self.observation_id:
            raise ValueError("Observation observation_id must be non-empty.")
        if self.status == ObservationStatus.SUCCESS and self.output_values is None:
            raise ValueError("Successful observation must contain output_values.")
        if self.status != ObservationStatus.SUCCESS and self.error is None:
            raise ValueError(f"Non-success observation status ({self.status}) requires an error.")

    def to_dict(self) -> Dict[str, Any]:
        """Convert observation to discovery-safe dictionary.
        
        Strictly excludes any internal simulator parameters or ground truth.
        """
        data: Dict[str, Any] = {
            "observation_id": self.observation_id,
            "input_id": self.input_id,
            "input_values": self.input_values,
            "output_values": self.output_values,
            "status": self.status.value,
            "sequence_number": self.sequence_number,
            "timestamp": self.timestamp,
            "is_held_out": self.is_held_out,
        }
        if self.error is not None:
            data["error"] = self.error.to_dict()
        if self.noise_metadata is not None:
            data["noise_metadata"] = self.noise_metadata
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Observation":
        error_data = data.get("error")
        error_obj = StructuredError.from_dict(error_data) if error_data else None
        return cls(
            observation_id=data.get("observation_id", generate_id("obs")),
            input_id=data["input_id"],
            input_values=data["input_values"],
            output_values=data.get("output_values"),
            status=ObservationStatus(data.get("status", ObservationStatus.SUCCESS.value)),
            sequence_number=data.get("sequence_number"),
            timestamp=data.get("timestamp", utc_now_iso()),
            error=error_obj,
            noise_metadata=data.get("noise_metadata"),
            is_held_out=data.get("is_held_out", False),
        )


# ==============================================================================
# 4. Hypothesis & Evaluation Contracts
# ==============================================================================

class EvaluationStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class HypothesisRepresentation:
    """Scientific description of a candidate explanation.
    
    Fields:
      hypothesis_id: Unique candidate identifier.
      model_family: Family tag (e.g. "polynomial", "piecewise_linear", "boolean_dnf").
      formula: Mathematical or symbolic representation string (e.g. "y = 0.5x^3 - 2x + 1").
      parameters: Estimated model parameters / weights.
      complexity: Defined complexity score (e.g. polynomial degree, number of free terms).
      revision: Revision sequence number for tracked revisions.
      created_at: UTC ISO-8601 timestamp.
    """
    model_family: str
    formula: str
    parameters: Dict[str, Any]
    hypothesis_id: str = field(default_factory=lambda: generate_id("hyp"))
    complexity: int = 1
    revision: int = 1
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "model_family": self.model_family,
            "formula": self.formula,
            "parameters": self.parameters,
            "complexity": self.complexity,
            "revision": self.revision,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HypothesisRepresentation":
        return cls(
            hypothesis_id=data.get("hypothesis_id", generate_id("hyp")),
            model_family=data["model_family"],
            formula=data["formula"],
            parameters=data.get("parameters", {}),
            complexity=data.get("complexity", 1),
            revision=data.get("revision", 1),
            created_at=data.get("created_at", utc_now_iso()),
        )


@dataclass(frozen=True)
class EvaluationRequest:
    """Request dispatched to evaluate a hypothesis against permitted observations.
    
    Crucial guardrail:
      - observation_ids: IDs of training observations used for fitting/measuring fit.
      - held_out_observation_ids: Reserved test observations.
    """
    hypothesis: HypothesisRepresentation
    observation_ids: List[str]
    held_out_observation_ids: Optional[List[str]] = None
    request_id: str = field(default_factory=lambda: generate_id("evreq"))
    metric_names: List[str] = field(default_factory=lambda: ["mae", "rmse"])

    def __post_init__(self):
        # Prevent overlap between training and held-out evaluation observations
        if self.held_out_observation_ids:
            overlap = set(self.observation_ids).intersection(set(self.held_out_observation_ids))
            if overlap:
                raise ValueError(
                    f"Data leakage detected: observations {sorted(list(overlap))} "
                    f"cannot be both training and held-out evaluation data."
                )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "hypothesis": self.hypothesis.to_dict(),
            "observation_ids": self.observation_ids,
            "held_out_observation_ids": self.held_out_observation_ids or [],
            "metric_names": self.metric_names,
        }


@dataclass(frozen=True)
class EvaluationResult:
    """Result of evaluating a hypothesis candidate against observations.
    
    Fields:
      evaluation_id: Unique evaluation execution identifier.
      hypothesis_id: ID of evaluated candidate.
      status: Completed, pending, or failed.
      metrics: Measured predictive metrics (e.g. {"mae": 0.05, "rmse": 0.08}).
      sample_count: Number of observations evaluated on.
      held_out_metrics: Metrics computed on held-out test data if provided.
      error: Error details if status is FAILED.
      evaluated_at: UTC ISO-8601 timestamp.
    """
    hypothesis_id: str
    status: EvaluationStatus = EvaluationStatus.COMPLETED
    evaluation_id: str = field(default_factory=lambda: generate_id("eval"))
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    sample_count: int = 0
    held_out_metrics: Optional[Dict[str, Optional[float]]] = None
    error: Optional[StructuredError] = None
    evaluated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self):
        if self.status != EvaluationStatus.COMPLETED and self.error is None:
            raise ValueError("Failed or non-completed evaluation requires error explanation.")

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "evaluation_id": self.evaluation_id,
            "hypothesis_id": self.hypothesis_id,
            "status": self.status.value,
            "metrics": self.metrics,
            "sample_count": self.sample_count,
            "evaluated_at": self.evaluated_at,
        }
        if self.held_out_metrics is not None:
            data["held_out_metrics"] = self.held_out_metrics
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data


# ==============================================================================
# 5. Black-Box Oracle Interface
# ==============================================================================

class OracleInterface(ABC):
    """Abstract interface for all black-box systems and simulators.
    
    The discovery engine communicates strictly through this interface.
    No hidden rule, internal coefficients, or reference formulas may be accessed.
    """

    @property
    @abstractmethod
    def domain(self) -> str:
        """Domain or system family identifier."""
        pass

    @abstractmethod
    def validate_input(self, experiment_input: ExperimentInput) -> Optional[StructuredError]:
        """Validate input before execution. Return None if valid, StructuredError if invalid."""
        pass

    @abstractmethod
    def query(self, experiment_input: ExperimentInput) -> Observation:
        """Execute experiment query and return validated Observation.
        
        Must return structured error on invalid input or system failure,
        without throwing raw internal exceptions.
        """
        pass


# ==============================================================================
# 6. Isolated Evaluator & Ground Truth Contract
# ==============================================================================

@dataclass(frozen=True)
class GroundTruthRule:
    """Reference ground-truth specification.
    
    STRICT TRUST BOUNDARY:
    This class is reserved exclusively for benchmark harnesses and test verification.
    It MUST NEVER be imported or referenced by discovery, hypothesis planning,
    or general API route serializers.
    """
    domain: str
    target_formula: str
    target_parameters: Dict[str, Any]
    evaluator_label: str
    secret_seed: Optional[int] = None

    def evaluate_exact(self, values: Dict[str, Any]) -> Dict[str, Any]:
        """Compute exact ground truth output for verification."""
        raise NotImplementedError("Subclasses implement exact evaluation.")


@dataclass(frozen=True)
class BenchmarkReport:
    """Standardized ground-truth benchmark evaluation report.
    
    Computed by the Isolated Evaluator, separate from the discovery loop.
    """
    benchmark_id: str
    run_id: str
    target_label: str
    hit_at_1: bool
    hit_at_3: bool
    hit_at_5: bool
    reciprocal_rank: float
    best_candidate_id: str
    queries_to_target: Optional[int] = None
    held_out_mae: Optional[float] = None
    held_out_rmse: Optional[float] = None
    metadata: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "benchmark_id": self.benchmark_id,
            "run_id": self.run_id,
            "target_label": self.target_label,
            "hit_at_1": self.hit_at_1,
            "hit_at_3": self.hit_at_3,
            "hit_at_5": self.hit_at_5,
            "reciprocal_rank": self.reciprocal_rank,
            "best_candidate_id": self.best_candidate_id,
            "queries_to_target": self.queries_to_target,
            "held_out_mae": self.held_out_mae,
            "held_out_rmse": self.held_out_rmse,
        }
        if self.metadata is not None:
            data["metadata"] = self.metadata
        return data


# ==============================================================================
# 7. Serialization & Boundary Guardrails
# ==============================================================================

LEAKAGE_DENYLIST_PREFIXES = (
    "ground_truth",
    "secret_",
    "true_",
    "evaluator_only",
    "oracle_internal",
)


def verify_no_ground_truth_leakage(payload: Dict[str, Any]) -> None:
    """Recursively verify that no evaluator-only or ground-truth keys exist in payload.
    
    Raises:
      ValueError if any forbidden key is detected in the dictionary.
    """
    for key, value in payload.items():
        for prefix in LEAKAGE_DENYLIST_PREFIXES:
            if key.startswith(prefix):
                raise ValueError(
                    f"Ground-truth leakage detected: key '{key}' is forbidden in discovery payloads."
                )
        if isinstance(value, dict):
            verify_no_ground_truth_leakage(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    verify_no_ground_truth_leakage(item)
