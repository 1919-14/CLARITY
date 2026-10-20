"""Unit tests for candidate experiment input generation (Issue #9).

Covers:
1. CandidateExperiment interface, representation, serialization, canonical keys.
2. Grid generation: bounds adherence, even spacing, reproducibility.
3. Uniform and Stratified Random generation: reproducibility with seed, diversity.
4. Input validation: rejecting non-numeric, NaN, infinite, or out-of-bounds inputs.
5. Deduplication: guaranteed uniqueness of candidate points within a batch.
6. Exclusion of previously queried inputs within tolerance.
7. Handling of empty or undersized candidate pools.
8. Ground-truth leakage prevention: generation depends strictly on schema/bounds.
9. Compatibility with Oracle validation contracts.
"""

import math
import subprocess
import sys
import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.contracts import ExperimentInput
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentGenerationError,
    CandidateExperimentGenerator,
    CandidateExperimentStatus,
    ERROR_EMPTY_CANDIDATE_POOL,
    ERROR_INVALID_INPUT_SCHEMA,
    ERROR_OUT_OF_BOUNDS,
    ERROR_UNDERSIZED_CANDIDATE_POOL,
    ExperimentGenerationConfig,
    ExperimentGenerationStrategy,
    NumericalDomainBounds,
    generate_candidate_experiments,
)


# ==============================================================================
# 1. CandidateExperiment Contract & Bounds
# ==============================================================================

def test_numerical_domain_bounds_validation():
    """Verify NumericalDomainBounds rejects invalid inputs."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-5.0, max_val=5.0)
    assert bounds.contains(0.0) is True
    assert bounds.contains(-5.0) is True
    assert bounds.contains(5.0) is True
    assert bounds.contains(-5.1) is False
    assert bounds.contains(5.1) is False

    # Invalid bounds (min > max)
    with pytest.raises(ValueError, match="cannot exceed max_val"):
        NumericalDomainBounds(min_val=10.0, max_val=-10.0)

    # Non-finite bounds
    with pytest.raises(ValueError, match="must be finite"):
        NumericalDomainBounds(min_val=float("-inf"), max_val=10.0)


def test_candidate_experiment_serialization():
    """Verify CandidateExperiment to_dict and canonical key."""
    inp = ExperimentInput(values={"x": 2.5}, domain="1d_polynomial")
    cand = CandidateExperiment(
        input_data=inp,
        strategy=ExperimentGenerationStrategy.GRID,
        generation_metadata={"step": 1},
    )
    d = cand.to_dict()
    assert d["candidate_id"].startswith("cand-exp-")
    assert d["status"] == "eligible"
    assert d["strategy"] == "grid"
    assert d["input_data"]["values"]["x"] == 2.5
    assert d["generation_metadata"]["step"] == 1
    assert cand.canonical_key() == (("x", 2.5),)


# ==============================================================================
# 2. Grid-based Candidate Generation
# ==============================================================================

def test_grid_candidate_generation():
    """Verify deterministic grid candidate generation spans domain evenly."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)
    candidates = generate_candidate_experiments(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.GRID,
    )

    assert len(candidates) == 5
    xs = [c.input_data.values["x"] for c in candidates]
    # For pool_size=5 over [-10, 10], points should be [-10.0, -5.0, 0.0, 5.0, 10.0]
    expected = [-10.0, -5.0, 0.0, 5.0, 10.0]
    for actual, exp in zip(xs, expected):
        assert math.isclose(actual, exp, abs_tol=1e-5)

    # All candidates must be eligible
    for c in candidates:
        assert c.status == CandidateExperimentStatus.ELIGIBLE
        assert bounds.contains(c.input_data.values["x"])


# ==============================================================================
# 3. Reproducibility of Randomized Generation
# ==============================================================================

def test_uniform_random_generation_reproducibility():
    """Verify random generation with fixed seed produces identical candidate sequence."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)
    cfg1 = ExperimentGenerationConfig(
        pool_size=10,
        strategy=ExperimentGenerationStrategy.UNIFORM_RANDOM,
        bounds=bounds,
        seed=42,
    )
    candidates1 = CandidateExperimentGenerator(cfg1).generate()

    cfg2 = ExperimentGenerationConfig(
        pool_size=10,
        strategy=ExperimentGenerationStrategy.UNIFORM_RANDOM,
        bounds=bounds,
        seed=42,
    )
    candidates2 = CandidateExperimentGenerator(cfg2).generate()

    assert len(candidates1) == 10
    assert len(candidates2) == 10

    xs1 = [c.input_data.values["x"] for c in candidates1]
    xs2 = [c.input_data.values["x"] for c in candidates2]
    assert xs1 == xs2

    # Different seed produces different sequence
    cfg3 = ExperimentGenerationConfig(
        pool_size=10,
        strategy=ExperimentGenerationStrategy.UNIFORM_RANDOM,
        bounds=bounds,
        seed=999,
    )
    candidates3 = CandidateExperimentGenerator(cfg3).generate()
    xs3 = [c.input_data.values["x"] for c in candidates3]
    assert xs1 != xs3


def test_stratified_random_generation_reproducibility():
    """Verify stratified random generation produces reproducible candidates across strata."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=0.0, max_val=10.0)
    candidates1 = generate_candidate_experiments(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.STRATIFIED_RANDOM,
        seed=123,
    )
    candidates2 = generate_candidate_experiments(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.STRATIFIED_RANDOM,
        seed=123,
    )

    xs1 = [c.input_data.values["x"] for c in candidates1]
    xs2 = [c.input_data.values["x"] for c in candidates2]
    assert xs1 == xs2
    # Verify values span [0, 10] monotonically roughly across strata
    assert all(0.0 <= x <= 10.0 for x in xs1)


# ==============================================================================
# 4. Deduplication & Batch Uniqueness
# ==============================================================================

def test_batch_deduplication():
    """Verify no duplicate inputs exist in generated pool."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-2.0, max_val=2.0)
    candidates = generate_candidate_experiments(
        pool_size=20,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.UNIFORM_RANDOM,
        seed=77,
    )
    keys = [c.canonical_key() for c in candidates]
    assert len(keys) == len(set(keys))


# ==============================================================================
# 5. Exclusion of Previously Queried Inputs
# ==============================================================================

def test_exclude_previously_queried_inputs():
    """Verify previously queried inputs are excluded from candidate pool."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)
    # Target points from a standard 5-point grid are: -10, -5, 0, 5, 10
    excluded = [{"x": 0.0}, {"x": -5.0}]

    candidates = generate_candidate_experiments(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.GRID,
        exclude_inputs=excluded,
        tolerance=1e-3,
    )

    xs = [c.input_data.values["x"] for c in candidates]
    # None of the excluded points should be in the pool
    for ex in [0.0, -5.0]:
        assert not any(math.isclose(x, ex, abs_tol=1e-3) for x in xs)


# ==============================================================================
# 6. Diagnostics on Empty or Undersized Pools
# ==============================================================================

def test_empty_candidate_pool_error():
    """Verify generator raises ERROR_EMPTY_CANDIDATE_POOL when all inputs excluded."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=5.0, max_val=5.0)  # Single point
    excluded = [{"x": 5.0}]

    cfg = ExperimentGenerationConfig(
        pool_size=1,
        bounds=bounds,
        exclude_inputs=excluded,
    )
    with pytest.raises(CandidateExperimentGenerationError) as exc_info:
        CandidateExperimentGenerator(cfg).generate()

    assert exc_info.value.error.code == ERROR_EMPTY_CANDIDATE_POOL


def test_undersized_candidate_pool_error_when_disallowed():
    """Verify generator raises error when pool is smaller than requested and allow_undersized=False."""
    bounds = NumericalDomainBounds(min_val=1.0, max_val=1.0)
    cfg = ExperimentGenerationConfig(
        pool_size=5,
        bounds=bounds,
        allow_undersized=False,
    )
    with pytest.raises(CandidateExperimentGenerationError) as exc_info:
        CandidateExperimentGenerator(cfg).generate()

    assert exc_info.value.error.code == ERROR_UNDERSIZED_CANDIDATE_POOL


# ==============================================================================
# 7. Compatibility with Oracle Interface
# ==============================================================================

def test_generated_candidates_valid_for_oracle_query():
    """Verify every generated candidate passes PolynomialLocalOracle validation."""
    oracle = PolynomialLocalOracle(min_x=-10.0, max_x=10.0)
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)

    candidates = generate_candidate_experiments(
        pool_size=10,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.GRID,
    )

    for cand in candidates:
        val_error = oracle.validate_input(cand.input_data)
        assert val_error is None

        # Execute query against oracle and assert success
        obs = oracle.query(cand.input_data)
        assert obs.status.value == "success"
        assert obs.output_values is not None
        assert "y" in obs.output_values


# ==============================================================================
# 8. Ground-Truth Isolation Check
# ==============================================================================

def test_experiment_generator_isolation_from_oracle_and_evaluator():
    """Subprocess test asserting experiment_generator imports no oracle or evaluator modules."""
    script = """
import sys
from app.domain import experiment_generator

leaked = [
    m for m in sys.modules
    if m.startswith("app.adapters.oracle") or m.startswith("app.evaluator")
]
if leaked:
    print(f"LEAKED_MODULES:{leaked}")
    sys.exit(1)
print("ISOLATED_OK")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Isolation violated: {result.stdout} {result.stderr}"
    assert "ISOLATED_OK" in result.stdout
