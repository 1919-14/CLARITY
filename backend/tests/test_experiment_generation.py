"""Unit tests for candidate experiment input generation (Issue #9).

Covers:
1. CandidateExperiment interface, representation, serialization, canonical keys.
2. Grid generation: bounds adherence, even spacing, reproducibility.
3. Uniform and Stratified Random generation: reproducibility with seed, diversity.
4. Input validation: rejecting non-numeric, NaN, infinite, or out-of-bounds inputs.
5. Deduplication: guaranteed uniqueness of candidate points within a batch.
6. Exclusion of previously queried inputs with pure absolute tolerance (rel_tol=0.0).
7. Narrow-interval precision preservation across all strategies.
8. Full domain coverage in grid generation after exclusions.
9. Tolerance type and finiteness validation.
10. OverflowError handling for extreme arbitrary-precision integers in validate_value().
11. Diagnostic information and requested-vs-generated counts in CandidateExperimentBatch.
12. Handling of empty or undersized candidate pools.
13. Ground-truth isolation: generation depends strictly on schema/bounds without oracle leakage.
14. Compatibility with Oracle validation contracts.
"""

import math
import subprocess
import sys
import pytest

from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.contracts import ExperimentInput
from app.domain.experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
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
    """Verify NumericalDomainBounds rejects invalid bounds and types."""
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


def test_validate_value_handles_overflow_integer():
    """Verify validate_value handles huge integers (10**400) cleanly without leaking OverflowError."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)
    huge_int = 10 ** 400

    err = bounds.validate_value(huge_int)
    assert err is not None
    assert err.code == ERROR_OUT_OF_BOUNDS
    assert "overflow" in err.message.lower()


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
# 2. Tolerance Type and Finiteness Validation (Review Finding 5)
# ==============================================================================

@pytest.mark.parametrize("invalid_tolerance, error_pattern", [
    (-0.01, "tolerance must be non-negative"),
    (float("nan"), "tolerance must be finite"),
    (float("inf"), "tolerance must be finite"),
    (float("-inf"), "tolerance must be finite"),
    ("0.05", "tolerance must be a real number"),
    (True, "tolerance must be a real number"),
    (False, "tolerance must be a real number"),
])
def test_experiment_generation_config_rejects_invalid_tolerance(invalid_tolerance, error_pattern):
    """Verify ExperimentGenerationConfig validates tolerance strictly."""
    with pytest.raises(ValueError, match=error_pattern):
        ExperimentGenerationConfig(tolerance=invalid_tolerance)


# ==============================================================================
# 3. Pure Absolute Tolerance on Large-Magnitude Domains (Review Finding 1)
# ==============================================================================

def test_pure_absolute_tolerance_large_magnitude():
    """Verify exclusion uses pure absolute tolerance (rel_tol=0.0) without false exclusions at ~1e9."""
    # Domain around 1e9: points are 0.5 apart
    bounds = NumericalDomainBounds(variable_name="x", min_val=1e9, max_val=1e9 + 10.0)
    # Exclude exactly 1e9 + 5.0
    excluded = [{"x": 1e9 + 5.0}]
    # Query point is 1e9 + 5.5 (distance = 0.5)
    # If default rel_tol=1e-9 was used, relative difference 0.5 / 1e9 = 5e-10 < 1e-9 would falsely exclude it!
    # With rel_tol=0.0 and abs_tol=1e-5, distance 0.5 > 1e-5, so it MUST NOT be excluded.
    cfg = ExperimentGenerationConfig(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.GRID,
        exclude_inputs=excluded,
        tolerance=1e-5,
    )
    candidates = CandidateExperimentGenerator(cfg).generate()
    xs = [c.input_data.values["x"] for c in candidates]

    # Verify candidate 1e9 + 5.0 is excluded, but points > 1e-5 away are kept
    assert not any(math.isclose(x, 1e9 + 5.0, abs_tol=1e-5, rel_tol=0.0) for x in xs)
    # Ensure points close to 1e9 + 5.5 can exist
    assert any(x > 1e9 + 5.0 for x in xs)


# ==============================================================================
# 4. Precision Preservation on Narrow Intervals (Review Finding 2)
# ==============================================================================

@pytest.mark.parametrize("strategy", [
    ExperimentGenerationStrategy.GRID,
    ExperimentGenerationStrategy.UNIFORM_RANDOM,
    ExperimentGenerationStrategy.STRATIFIED_RANDOM,
])
def test_narrow_interval_bounds_preservation(strategy):
    """Verify narrow interval [1e-7, 4e-7] is preserved and never truncated to 0.0 out of bounds."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=1e-7, max_val=4e-7)
    cfg = ExperimentGenerationConfig(
        pool_size=4,
        bounds=bounds,
        strategy=strategy,
        seed=42,
    )
    candidates = CandidateExperimentGenerator(cfg).generate()

    assert len(candidates) > 0
    for cand in candidates:
        x = cand.input_data.values["x"]
        assert bounds.contains(x), f"Candidate x={x} violated narrow bounds [{bounds.min_val}, {bounds.max_val}]"
        assert x != 0.0


# ==============================================================================
# 5. Full Domain Coverage in Grid Generation after Exclusions (Review Finding 3)
# ==============================================================================

def test_grid_maintains_full_domain_coverage_after_early_exclusions():
    """Verify grid generation maintains coverage across upper domain after lower-interval exclusions."""
    bounds = NumericalDomainBounds(variable_name="x", min_val=-10.0, max_val=10.0)
    # Exclude specific early grid points
    excluded = [{"x": -10.0}, {"x": -5.0}, {"x": 0.0}]

    candidates = generate_candidate_experiments(
        pool_size=5,
        bounds=bounds,
        strategy=ExperimentGenerationStrategy.GRID,
        exclude_inputs=excluded,
        tolerance=1e-3,
    )

    assert len(candidates) == 5
    xs = [c.input_data.values["x"] for c in candidates]
    # The pool must span towards the upper domain limit (+10.0) rather than stopping early at 0.0
    assert max(xs) >= 8.0, f"Upper domain was omitted: max(xs)={max(xs)}"
    # Excluded points must not be present
    for ex in [-10.0, -5.0, 0.0]:
        assert not any(math.isclose(x, ex, abs_tol=1e-3, rel_tol=0.0) for x in xs)


# ==============================================================================
# 6. Undersized Pool Diagnostics (Review Finding 4)
# ==============================================================================

def test_undersized_pool_returns_diagnostics_when_allowed():
    """Verify CandidateExperimentBatch exposes requested vs generated counts and diagnostic message."""
    # Degenerate single-point domain requesting 5 points with allow_undersized=True
    bounds = NumericalDomainBounds(variable_name="x", min_val=3.0, max_val=3.0)
    cfg = ExperimentGenerationConfig(
        pool_size=5,
        bounds=bounds,
        allow_undersized=True,
    )
    generator = CandidateExperimentGenerator(cfg)
    batch = generator.generate_batch()

    assert isinstance(batch, CandidateExperimentBatch)
    assert batch.requested_count == 5
    assert batch.generated_count == 1
    assert batch.is_undersized is True
    assert batch.diagnostic_message is not None
    assert "fewer than requested" in batch.diagnostic_message
    assert len(batch) == 1
    assert batch[0].input_data.values["x"] == 3.0


# ==============================================================================
# 7. Reproducibility of Randomized Generation
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
    assert all(0.0 <= x <= 10.0 for x in xs1)


# ==============================================================================
# 8. Deduplication & Batch Uniqueness
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
# 9. Diagnostics on Empty or Undersized Pools (When Not Allowed)
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
# 10. Compatibility with Oracle Interface
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
# 11. Ground-Truth Isolation Check
# ==============================================================================

def test_experiment_generator_isolation_from_oracle_and_evaluator():
    """Subprocess test asserting experiment_generator imports no oracle or evaluator modules.

    The subprocess is run from the backend/ directory so that `app` is on the
    Python path without requiring an installed package.
    """
    import pathlib

    backend_dir = pathlib.Path(__file__).parent.parent

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
        cwd=str(backend_dir),
    )
    assert result.returncode == 0, f"Isolation violated: {result.stdout} {result.stderr}"
    assert "ISOLATED_OK" in result.stdout
