"""Comprehensive contract and boundary tests for Issue #4.

Tests:
1. ExperimentInput schema validation (valid, missing fields, invalid types).
2. Observation schema serialization and structured error handling.
3. Local simulator execution (valid query, out of bounds, missing param, wrong type).
4. Hypothesis representation and EvaluationRequest data leakage guard (train vs held-out).
5. Boundary isolation (verify ground-truth parameters are absent from discovery responses).
6. Evaluator-only benchmark scoring (Hit@K, MRR calculation).
"""

import pytest
from app.domain.contracts import (
    ExperimentInput,
    Observation,
    ObservationStatus,
    HypothesisRepresentation,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    StructuredError,
    GroundTruthRule,
    verify_no_ground_truth_leakage,
)
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.evaluator.benchmark_evaluator import BenchmarkEvaluator


# ==============================================================================
# 1. ExperimentInput Contract Tests
# ==============================================================================

def test_experiment_input_valid():
    """Verify standard valid ExperimentInput instantiates and serializes correctly."""
    inp = ExperimentInput(
        values={"x": 2.5},
        domain="1d_polynomial",
        metadata={"strategy": "active_disagreement"},
    )
    assert inp.input_id.startswith("inp-")
    assert inp.values == {"x": 2.5}
    assert inp.domain == "1d_polynomial"

    serialized = inp.to_dict()
    assert serialized["input_id"] == inp.input_id
    assert serialized["values"]["x"] == 2.5
    assert serialized["metadata"]["strategy"] == "active_disagreement"


def test_experiment_input_empty_values_fails():
    """Verify ExperimentInput rejects empty value dictionaries."""
    with pytest.raises(ValueError, match="non-empty dictionary"):
        ExperimentInput(values={})


def test_experiment_input_invalid_types():
    """Verify ExperimentInput rejects non-primitive value types."""
    with pytest.raises(ValueError, match="must be int, float, str, or bool"):
        ExperimentInput(values={"x": [1, 2, 3]})


# ==============================================================================
# 2. Observation Contract Tests
# ==============================================================================

def test_observation_success_serialization():
    """Verify successful observation contains outputs and correct status."""
    obs = Observation(
        input_id="inp-001",
        input_values={"x": 1.0},
        output_values={"y": 0.5},
        status=ObservationStatus.SUCCESS,
        sequence_number=1,
    )
    data = obs.to_dict()
    assert data["status"] == "success"
    assert data["output_values"] == {"y": 0.5}
    assert data["is_held_out"] is False
    assert "error" not in data


def test_observation_failure_requires_error():
    """Verify non-success observation requires a StructuredError."""
    with pytest.raises(ValueError, match="requires an error"):
        Observation(
            input_id="inp-001",
            input_values={"x": 100.0},
            status=ObservationStatus.INVALID_INPUT,
            error=None,
        )


def test_observation_structured_error():
    """Verify structured error formats accurately."""
    err = StructuredError(
        code="OUT_OF_BOUNDS",
        message="Parameter x=50 exceeds bounds [-10, 10]",
        details={"x": 50, "max": 10},
    )
    obs = Observation(
        input_id="inp-002",
        input_values={"x": 50.0},
        status=ObservationStatus.INVALID_INPUT,
        error=err,
    )
    data = obs.to_dict()
    assert data["status"] == "invalid_input"
    assert data["output_values"] is None
    assert data["error"]["code"] == "OUT_OF_BOUNDS"
    assert data["error"]["details"]["max"] == 10


# ==============================================================================
# 3. Local Deterministic Simulator Tests
# ==============================================================================

def test_local_oracle_valid_query():
    """Verify local polynomial oracle evaluates deterministic function correctly.
    
    Default rule: y = 0.5*x^3 - 1.0*x^2 - 2.0*x + 3.0
    For x = 2.0:
      y = 0.5*(8) - 1.0*(4) - 2.0*(2) + 3.0 = 4.0 - 4.0 - 4.0 + 3.0 = -1.0
    """
    oracle = PolynomialLocalOracle()
    inp = ExperimentInput(values={"x": 2.0})

    obs = oracle.query(inp)
    assert obs.status == ObservationStatus.SUCCESS
    assert obs.input_id == inp.input_id
    assert obs.input_values == {"x": 2.0}
    assert obs.output_values == {"y": -1.0}
    assert obs.sequence_number == 1
    assert oracle.query_count == 1


def test_local_oracle_out_of_bounds_error():
    """Verify oracle rejects out of bounds query with structured error."""
    oracle = PolynomialLocalOracle(min_x=-5.0, max_x=5.0)
    inp = ExperimentInput(values={"x": 15.0})

    obs = oracle.query(inp)
    assert obs.status == ObservationStatus.INVALID_INPUT
    assert obs.output_values is None
    assert obs.error is not None
    assert obs.error.code == "OUT_OF_BOUNDS"
    # Query count does not advance for invalid inputs before execution
    assert oracle.query_count == 0


def test_local_oracle_missing_parameter():
    """Verify oracle rejects input missing required variable 'x'."""
    oracle = PolynomialLocalOracle()
    inp = ExperimentInput(values={"z": 3.0})

    obs = oracle.query(inp)
    assert obs.status == ObservationStatus.INVALID_INPUT
    assert obs.error.code == "MISSING_PARAMETER"


def test_local_oracle_invalid_type():
    """Verify oracle rejects non-numeric types for 'x'."""
    oracle = PolynomialLocalOracle()
    inp = ExperimentInput(values={"x": "invalid_string"})

    obs = oracle.query(inp)
    assert obs.status == ObservationStatus.INVALID_INPUT
    assert obs.error.code == "INVALID_PARAMETER_TYPE"


# ==============================================================================
# 4. Hypothesis & Evaluation Data Leakage Guard Tests
# ==============================================================================

def test_evaluation_request_train_and_heldout_separation():
    """Verify EvaluationRequest forbids overlapping training and held-out observation IDs."""
    hyp = HypothesisRepresentation(
        model_family="polynomial",
        formula="y = 0.5*x^3",
        parameters={"degree": 3},
        complexity=3,
    )

    # Valid non-overlapping sets
    req = EvaluationRequest(
        hypothesis=hyp,
        observation_ids=["obs-1", "obs-2", "obs-3"],
        held_out_observation_ids=["obs-4", "obs-5"],
    )
    assert len(req.observation_ids) == 3
    assert len(req.held_out_observation_ids) == 2

    # Invalid overlapping sets must fail
    with pytest.raises(ValueError, match="Data leakage detected"):
        EvaluationRequest(
            hypothesis=hyp,
            observation_ids=["obs-1", "obs-2", "obs-3"],
            held_out_observation_ids=["obs-3", "obs-4"],
        )


def test_evaluation_result_serialization():
    """Verify EvaluationResult serialization."""
    res = EvaluationResult(
        hypothesis_id="hyp-101",
        status=EvaluationStatus.COMPLETED,
        metrics={"mae": 0.045, "rmse": 0.052},
        sample_count=10,
        held_out_metrics={"mae": 0.082, "rmse": 0.091},
    )
    data = res.to_dict()
    assert data["metrics"]["mae"] == 0.045
    assert data["held_out_metrics"]["mae"] == 0.082
    assert data["sample_count"] == 10


# ==============================================================================
# 5. Boundary Isolation & Ground-Truth Leakage Tests
# ==============================================================================

def test_observation_excludes_ground_truth_and_internal_secrets():
    """Verify observation dictionary contains no forbidden ground truth prefixes."""
    oracle = PolynomialLocalOracle()
    inp = ExperimentInput(values={"x": 1.5})
    obs = oracle.query(inp)

    obs_dict = obs.to_dict()

    # Must pass leakage assertion
    verify_no_ground_truth_leakage(obs_dict)

    # Assert secret coefficients are not in returned dictionary
    assert "coefficients" not in obs_dict
    assert "hidden_rule" not in obs_dict
    assert "w3" not in obs_dict


def test_leakage_detector_raises_on_forbidden_keys():
    """Verify verify_no_ground_truth_leakage detects leaked keys in nested dicts."""
    clean_dict = {"input": {"x": 1.0}, "output": {"y": 2.0}}
    verify_no_ground_truth_leakage(clean_dict)

    leaked_dict = {
        "input": {"x": 1.0},
        "output": {"y": 2.0},
        "secret_equation": "0.5*x^3",
    }
    with pytest.raises(ValueError, match="Ground-truth leakage detected"):
        verify_no_ground_truth_leakage(leaked_dict)

    nested_leaked = {
        "results": [{"evaluator_only_label": "cubic_poly"}]
    }
    with pytest.raises(ValueError, match="Ground-truth leakage detected"):
        verify_no_ground_truth_leakage(nested_leaked)


# ==============================================================================
# 6. Isolated Evaluator Benchmark Tests
# ==============================================================================

def test_benchmark_evaluator_scoring():
    """Verify BenchmarkEvaluator scores candidate hypotheses against ground truth."""
    ground_truth = GroundTruthRule(
        domain="1d_polynomial",
        target_formula="0.5*x^3 - 1.0*x^2 - 2.0*x + 3.0",
        target_parameters={"degree": 3},
        evaluator_label="target_degree_3_polynomial",
    )
    evaluator = BenchmarkEvaluator(ground_truth)

    # Candidate list with target at rank 2
    candidates = [
        HypothesisRepresentation(
            hypothesis_id="hyp-linear",
            model_family="1d_linear",
            formula="2.0*x + 1.0",
            parameters={"degree": 1},
        ),
        HypothesisRepresentation(
            hypothesis_id="hyp-cubic",
            model_family="1d_polynomial",
            formula="0.5*x^3 - 1.0*x^2 - 2.0*x + 3.0",
            parameters={"degree": 3},
        ),
        HypothesisRepresentation(
            hypothesis_id="hyp-quadratic",
            model_family="1d_polynomial",
            formula="1.5*x^2 - 0.5*x",
            parameters={"degree": 2},
        ),
    ]

    report = evaluator.evaluate_candidates(
        run_id="run-001",
        ranked_candidates=candidates,
        queries_executed=5,
        held_out_mae=0.015,
        held_out_rmse=0.022,
    )

    # Candidate 2 is the target
    assert report.hit_at_1 is False
    assert report.hit_at_3 is True
    assert report.hit_at_5 is True
    assert report.reciprocal_rank == 0.5  # 1 / 2
    assert report.queries_to_target == 5
    assert report.held_out_mae == 0.015
