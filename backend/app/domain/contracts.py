"""Core scientific data contracts and interfaces for CLARITY.

Defines schemas and domain objects for:
- Experiment inputs and Oracle observations
- Hypothesis representations and evaluation results
- Oracle abstract base class and query errors
- Evaluator-only ground truth and benchmark scoring
- Serialization utilities ensuring strict boundary isolation
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Any, Dict, List, Optional, Tuple, Union
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


class FittingStatus(str, Enum):
    """Outcome of fitting a candidate's parameters to training observations."""
    SUCCESS = "success"
    FAILED = "failed"


class HypothesisStatus(str, Enum):
    """Lifecycle state of a candidate explanation.

    CANDIDATE covers the generated/proposed state: the hypothesis has a
    specification but has not been evaluated against observations. EVALUATED is
    set by downstream evaluation services (Issue #6) once a result exists; the
    generator must never set it.
    """
    CANDIDATE = "candidate"
    EVALUATED = "evaluated"


class HypothesisOrigin(str, Enum):
    """Where a candidate explanation came from."""
    SYSTEM_GENERATED = "system_generated"
    RESEARCHER_PROPOSED = "researcher_proposed"
    REFINED = "refined"


@dataclass(frozen=True)
class HypothesisProvenance:
    """Structured description of a candidate's origin.

    Fields:
      origin: One of HypothesisOrigin.
      generator: Generator identifier. Required for SYSTEM_GENERATED and rejected
        for RESEARCHER_PROPOSED so a researcher submission cannot claim to be
        produced by the generator.
      generator_version: Generator version string, when applicable.
      parent_hypothesis_id: Parent candidate ID. Required for REFINED candidates.
      note: Optional short non-sensitive note (for example a researcher rationale).

    Configuration and seed details live in the hypothesis's generation_metadata
    rather than being duplicated here.
    """
    origin: HypothesisOrigin
    generator: Optional[str] = None
    generator_version: Optional[str] = None
    parent_hypothesis_id: Optional[str] = None
    note: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.origin, HypothesisOrigin):
            raise ValueError(
                f"HypothesisProvenance origin must be a HypothesisOrigin, got {type(self.origin).__name__}."
            )
        if self.origin == HypothesisOrigin.SYSTEM_GENERATED and not self.generator:
            raise ValueError("SYSTEM_GENERATED provenance requires a non-empty generator identifier.")
        if self.origin == HypothesisOrigin.REFINED and not self.parent_hypothesis_id:
            raise ValueError("REFINED provenance requires a parent_hypothesis_id.")
        if self.origin == HypothesisOrigin.RESEARCHER_PROPOSED and self.generator is not None:
            raise ValueError(
                "RESEARCHER_PROPOSED provenance must not claim a generator; "
                "record the submission source in note instead."
            )

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"origin": self.origin.value}
        if self.generator is not None:
            data["generator"] = self.generator
        if self.generator_version is not None:
            data["generator_version"] = self.generator_version
        if self.parent_hypothesis_id is not None:
            data["parent_hypothesis_id"] = self.parent_hypothesis_id
        if self.note is not None:
            data["note"] = self.note
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HypothesisProvenance":
        origin = data["origin"]
        return cls(
            origin=origin if isinstance(origin, HypothesisOrigin) else HypothesisOrigin(origin),
            generator=data.get("generator"),
            generator_version=data.get("generator_version"),
            parent_hypothesis_id=data.get("parent_hypothesis_id"),
            note=data.get("note"),
        )


@dataclass(frozen=True)
class HypothesisRepresentation:
    """Scientific description of a candidate explanation.

    Fields:
      hypothesis_id: Unique, stable candidate identifier.
      model_family: Registered family identifier (e.g. "polynomial").
      formula: Human-readable summary of the model (e.g. "y = 3.0 - 2.0*x").
      parameters: Machine-readable model specification in the canonical shape
        defined by the registered model family (see docs/contracts.md).
      complexity: Defined complexity score (e.g. number of polynomial coefficients).
      revision: Revision sequence number, incremented only on specification changes.
      created_at: UTC ISO-8601 timestamp.
      status: Lifecycle state; generated candidates are CANDIDATE, not EVALUATED.
      provenance: Structured origin information (see HypothesisProvenance).
      generation_metadata: Generator name/version, seed, and configuration.
      fitting_metadata: Optional output reserved for downstream fitting services.
      uncertainty_metadata: Optional output reserved for downstream uncertainty work.

    Absent optional fields mean "not computed yet". They are never defaulted to
    zero error, perfect confidence, or a completed evaluation.

    `parameters`, `generation_metadata`, `fitting_metadata`, and
    `uncertainty_metadata` are checked by verify_no_ground_truth_leakage() and
    stored as immutable snapshots: nested dicts become read-only mappings and
    sequences become tuples, so later mutation of the caller's objects cannot
    change validated data and in-place edits raise. Use to_dict() for a mutable
    plain-JSON copy. This is a data-integrity convention, not a security boundary.
    """
    model_family: str
    formula: str
    parameters: Mapping[str, Any]
    hypothesis_id: str = field(default_factory=lambda: generate_id("hyp"))
    complexity: int = 1
    revision: int = 1
    created_at: str = field(default_factory=utc_now_iso)
    status: HypothesisStatus = HypothesisStatus.CANDIDATE
    provenance: Optional[HypothesisProvenance] = None
    generation_metadata: Optional[Mapping[str, Any]] = None
    fitting_metadata: Optional[Mapping[str, Any]] = None
    uncertainty_metadata: Optional[Mapping[str, Any]] = None

    def __post_init__(self):
        if not isinstance(self.model_family, str) or not self.model_family:
            raise ValueError("Hypothesis model_family must be a non-empty string.")
        if not isinstance(self.formula, str) or not self.formula:
            raise ValueError("Hypothesis formula (human-readable summary) must be a non-empty string.")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("Hypothesis parameters must be a mapping specification.")
        if not isinstance(self.status, HypothesisStatus):
            raise ValueError(
                f"Hypothesis status must be a HypothesisStatus, got {type(self.status).__name__}."
            )
        if self.provenance is not None and not isinstance(self.provenance, HypothesisProvenance):
            raise ValueError("Hypothesis provenance must be a HypothesisProvenance instance.")
        for field_name, payload in (
            ("parameters", self.parameters),
            ("generation_metadata", self.generation_metadata),
            ("fitting_metadata", self.fitting_metadata),
            ("uncertainty_metadata", self.uncertainty_metadata),
        ):
            if payload is not None:
                verify_no_ground_truth_leakage(payload)

        # Freeze after validation so the checked payload cannot drift afterwards.
        object.__setattr__(self, "parameters", freeze_payload(self.parameters))
        for field_name in ("generation_metadata", "fitting_metadata", "uncertainty_metadata"):
            payload = getattr(self, field_name)
            if payload is not None:
                object.__setattr__(self, field_name, freeze_payload(payload))

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "hypothesis_id": self.hypothesis_id,
            "model_family": self.model_family,
            "formula": self.formula,
            "parameters": thaw_payload(self.parameters),
            "complexity": self.complexity,
            "revision": self.revision,
            "created_at": self.created_at,
            "status": self.status.value,
        }
        if self.provenance is not None:
            data["provenance"] = self.provenance.to_dict()
        if self.generation_metadata is not None:
            data["generation_metadata"] = thaw_payload(self.generation_metadata)
        if self.fitting_metadata is not None:
            data["fitting_metadata"] = thaw_payload(self.fitting_metadata)
        if self.uncertainty_metadata is not None:
            data["uncertainty_metadata"] = thaw_payload(self.uncertainty_metadata)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HypothesisRepresentation":
        provenance_data = data.get("provenance")
        status = data.get("status", HypothesisStatus.CANDIDATE.value)
        return cls(
            hypothesis_id=data.get("hypothesis_id", generate_id("hyp")),
            model_family=data["model_family"],
            formula=data["formula"],
            parameters=data.get("parameters", {}),
            complexity=data.get("complexity", 1),
            revision=data.get("revision", 1),
            created_at=data.get("created_at", utc_now_iso()),
            status=status if isinstance(status, HypothesisStatus) else HypothesisStatus(status),
            provenance=(
                provenance_data
                if isinstance(provenance_data, HypothesisProvenance)
                else HypothesisProvenance.from_dict(provenance_data)
                if provenance_data is not None
                else None
            ),
            generation_metadata=data.get("generation_metadata"),
            fitting_metadata=data.get("fitting_metadata"),
            uncertainty_metadata=data.get("uncertainty_metadata"),
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
    """Standardized result of fitting and evaluating a candidate hypothesis.

    Fields:
      evaluation_id: Unique evaluation execution identifier.
      hypothesis_id: Stable ID of the evaluated candidate.
      status: COMPLETED or FAILED (PENDING reserved for queued work).
      metrics: Training metrics for accepted training observations. An empty
        mapping means no metrics are available; it never means zero error.
      sample_count: Accepted training observations used for fitting.
      held_out_metrics: Metrics on held-out observations, absent when held-out
        evaluation was not possible.
      error: Structured primary failure explanation for FAILED results.
      evaluated_at: UTC ISO-8601 timestamp.
      model_family: Registered family identifier of the candidate.
      fitting_status: Whether fitting succeeded, failed, or was not attempted.
      fitting_method: Fitting method actually used.
      fitting_config: Fitting configuration that affected the fit.
      fitted_specification: Fitted canonical model specification. Present only
        for successful fitting; the original candidate is never mutated.
      complexity: Complexity of the fitted model when available.
      held_out_sample_count: Accepted held-out observations used for metrics.
      observation_counts: Requested/accepted/excluded counts per partition.
      exclusion_codes: Aggregated exclusion reason codes and counts.
      metric_definitions: Metric name to definition/version text.
      metric_version: Version identifier for the metric implementations.
      dataset_ids: Dataset identifier and partition observation IDs.
      request_id: EvaluationRequest that produced this result, when available.
      warnings: Non-fatal structured warnings (for example unavailable held-out
        metrics or an exactly determined fit).

    Absent metrics mean "unavailable", never zero. A FAILED result carries no
    fitted specification and no metrics, so an unfitted or failed candidate
    cannot look favorable.
    """
    hypothesis_id: str
    status: EvaluationStatus = EvaluationStatus.COMPLETED
    evaluation_id: str = field(default_factory=lambda: generate_id("eval"))
    metrics: Dict[str, Optional[float]] = field(default_factory=dict)
    sample_count: int = 0
    held_out_metrics: Optional[Dict[str, Optional[float]]] = None
    error: Optional[StructuredError] = None
    evaluated_at: str = field(default_factory=utc_now_iso)
    model_family: Optional[str] = None
    fitting_status: Optional[FittingStatus] = None
    fitting_method: Optional[str] = None
    fitting_config: Optional[Mapping[str, Any]] = None
    fitted_specification: Optional[Mapping[str, Any]] = None
    complexity: Optional[int] = None
    held_out_sample_count: Optional[int] = None
    observation_counts: Optional[Mapping[str, int]] = None
    exclusion_codes: Optional[Mapping[str, int]] = None
    metric_definitions: Optional[Mapping[str, str]] = None
    metric_version: Optional[str] = None
    dataset_ids: Optional[Mapping[str, Any]] = None
    request_id: Optional[str] = None
    warnings: Optional[Tuple[StructuredError, ...]] = None

    def __post_init__(self):
        if self.status != EvaluationStatus.COMPLETED and self.error is None:
            raise ValueError("Failed or non-completed evaluation requires error explanation.")
        if self.fitting_status is not None and not isinstance(self.fitting_status, FittingStatus):
            raise ValueError(
                "EvaluationResult fitting_status must be a FittingStatus, "
                f"got {type(self.fitting_status).__name__}."
            )

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
        if self.model_family is not None:
            data["model_family"] = self.model_family
        if self.fitting_status is not None:
            data["fitting_status"] = self.fitting_status.value
        if self.fitting_method is not None:
            data["fitting_method"] = self.fitting_method
        if self.fitting_config is not None:
            data["fitting_config"] = thaw_payload(self.fitting_config)
        if self.fitted_specification is not None:
            data["fitted_specification"] = thaw_payload(self.fitted_specification)
        if self.complexity is not None:
            data["complexity"] = self.complexity
        if self.held_out_sample_count is not None:
            data["held_out_sample_count"] = self.held_out_sample_count
        if self.observation_counts is not None:
            data["observation_counts"] = thaw_payload(self.observation_counts)
        if self.exclusion_codes is not None:
            data["exclusion_codes"] = thaw_payload(self.exclusion_codes)
        if self.metric_definitions is not None:
            data["metric_definitions"] = thaw_payload(self.metric_definitions)
        if self.metric_version is not None:
            data["metric_version"] = self.metric_version
        if self.dataset_ids is not None:
            data["dataset_ids"] = thaw_payload(self.dataset_ids)
        if self.request_id is not None:
            data["request_id"] = self.request_id
        if self.warnings is not None:
            data["warnings"] = [warning.to_dict() for warning in self.warnings]
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


def verify_no_ground_truth_leakage(payload: Mapping[str, Any]) -> None:
    """Recursively verify that no evaluator-only or ground-truth keys exist in payload.

    Accepts any mapping, including the read-only snapshots stored on hypotheses.

    Raises:
      ValueError if any forbidden key is detected or a key is not a string.
    """
    for key, value in payload.items():
        if not isinstance(key, str):
            raise ValueError(
                f"Ground-truth leakage check requires string keys, got {type(key).__name__}."
            )
        for prefix in LEAKAGE_DENYLIST_PREFIXES:
            if key.startswith(prefix):
                raise ValueError(
                    f"Ground-truth leakage detected: key '{key}' is forbidden in discovery payloads."
                )
        if isinstance(value, Mapping):
            verify_no_ground_truth_leakage(value)
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, Mapping):
                    verify_no_ground_truth_leakage(item)


def freeze_payload(payload: Any) -> Any:
    """Recursively copy a JSON-like payload into an immutable snapshot.

    Mappings become read-only ``MappingProxyType`` views and sequences become
    tuples, so later mutation of the caller's dict/list cannot change stored
    domain data and in-place edits of the snapshot fail immediately.

    This is a data-integrity convention inside the domain model, not a security
    boundary: it prevents accidental aliasing after validation runs.
    """
    if isinstance(payload, Mapping):
        return MappingProxyType({key: freeze_payload(value) for key, value in payload.items()})
    if isinstance(payload, (list, tuple)):
        return tuple(freeze_payload(item) for item in payload)
    return payload


def thaw_payload(payload: Any) -> Any:
    """Recursively convert a frozen snapshot back into plain dict/list values."""
    if isinstance(payload, Mapping):
        return {key: thaw_payload(value) for key, value in payload.items()}
    if isinstance(payload, tuple):
        return [thaw_payload(item) for item in payload]
    if isinstance(payload, list):
        return [thaw_payload(item) for item in payload]
    return payload
