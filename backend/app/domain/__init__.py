"""Domain package initialization for CLARITY contracts and models."""

from .contracts import (
    StructuredError,
    OracleQueryError,
    ExperimentInput,
    Observation,
    ObservationStatus,
    HypothesisRepresentation,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    OracleInterface,
    GroundTruthRule,
    BenchmarkReport,
    verify_no_ground_truth_leakage,
    utc_now_iso,
    generate_id,
)

__all__ = [
    "StructuredError",
    "OracleQueryError",
    "ExperimentInput",
    "Observation",
    "ObservationStatus",
    "HypothesisRepresentation",
    "EvaluationRequest",
    "EvaluationResult",
    "EvaluationStatus",
    "OracleInterface",
    "GroundTruthRule",
    "BenchmarkReport",
    "verify_no_ground_truth_leakage",
    "utc_now_iso",
    "generate_id",
]
