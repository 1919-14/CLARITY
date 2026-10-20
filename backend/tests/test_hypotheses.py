"""Contract, model-family, and candidate-generation tests for Issue #5.

Covers:
1. Hypothesis representation, status, provenance, serialization, leakage guard.
2. Polynomial family validation, predictions, coefficient ordering, error codes.
3. Model-family registry lookup and failure behavior.
4. Candidate generation: limits, uniqueness, provenance, reproducibility,
   duplicate handling, exhaustion, and structured configuration errors.
5. Isolation: the generation module does not depend on the oracle or evaluator.
"""

from collections.abc import Mapping
import inspect
import json
import os
import subprocess
import sys

import pytest

from app.domain.candidate_generator import (
    ERROR_ATTEMPT_BUDGET_EXHAUSTED,
    ERROR_INVALID_GENERATION_CONFIG,
    GENERATOR_NAME,
    GENERATOR_VERSION,
    CandidateGenerationError,
    CandidateGenerator,
    HypothesisGenerationConfig,
    generate_candidates,
)
from app.domain.contracts import (
    HypothesisOrigin,
    HypothesisProvenance,
    HypothesisRepresentation,
    HypothesisStatus,
    StructuredError,
)
from app.domain.hypothesis_models import (
    ERROR_INVALID_PARAMETER_TYPE,
    ERROR_INVALID_SPECIFICATION,
    ERROR_MISSING_PARAMETER,
    ERROR_NON_FINITE_PREDICTION,
    ERROR_NON_FINITE_VALUE,
    ERROR_UNKNOWN_MODEL_FAMILY,
    ERROR_UNSUPPORTED_DEGREE,
    HARD_MAX_POLYNOMIAL_DEGREE,
    POLYNOMIAL_FAMILY_ID,
    HypothesisModelFamily,
    ModelFamilyRegistry,
    PolynomialFamily,
    ValidatedModel,
    default_registry,
)


def polynomial_spec(coefficients, degree=None):
    """Build a canonical polynomial specification from ascending coefficients."""
    return {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": len(coefficients) - 1 if degree is None else degree,
        "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
    }


CUBIC_COEFFICIENTS = (3.0, -2.0, -1.0, 0.5)  # y = 3 - 2x - x^2 + 0.5x^3


# ==============================================================================
# 1. Hypothesis representation
# ==============================================================================

def test_hypothesis_representation_round_trip():
    """A fully populated hypothesis survives to_dict/from_dict unchanged."""
    hypothesis = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = 3.0 + -2.0*x",
        parameters=polynomial_spec([3.0, -2.0]),
        complexity=2,
        provenance=HypothesisProvenance(
            origin=HypothesisOrigin.SYSTEM_GENERATED,
            generator="test_generator",
            generator_version="1.0.0",
        ),
        generation_metadata={"seed": 11, "initialization": "test"},
        status=HypothesisStatus.CANDIDATE,
    )

    data = hypothesis.to_dict()
    assert data["status"] == "candidate"
    assert data["provenance"]["origin"] == "system_generated"
    assert data["provenance"]["generator"] == "test_generator"
    assert data["generation_metadata"]["seed"] == 11

    restored = HypothesisRepresentation.from_dict(data)
    assert restored == hypothesis


def test_hypothesis_optional_metrics_are_absent_not_zero():
    """Uncomputed fitting/uncertainty metadata stays absent instead of defaulting."""
    hypothesis = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = 0.0",
        parameters=polynomial_spec([0.0]),
    )
    data = hypothesis.to_dict()

    assert hypothesis.status == HypothesisStatus.CANDIDATE
    assert "fitting_metadata" not in data
    assert "uncertainty_metadata" not in data
    assert "provenance" not in data
    assert data["complexity"] == 1


def test_hypothesis_rejects_invalid_status_string():
    """Raw strings are rejected so lifecycle state cannot drift silently."""
    with pytest.raises(ValueError, match="HypothesisStatus"):
        HypothesisRepresentation(
            model_family=POLYNOMIAL_FAMILY_ID,
            formula="y = 0.0",
            parameters=polynomial_spec([0.0]),
            status="candidate",
        )


def test_hypothesis_rejects_empty_formula():
    with pytest.raises(ValueError, match="human-readable summary"):
        HypothesisRepresentation(
            model_family=POLYNOMIAL_FAMILY_ID,
            formula="",
            parameters=polynomial_spec([0.0]),
        )


def test_hypothesis_rejects_ground_truth_keys_in_parameters():
    """The representation enforces the Issue #4 leakage denylist on its payloads."""
    with pytest.raises(ValueError, match="leakage"):
        HypothesisRepresentation(
            model_family=POLYNOMIAL_FAMILY_ID,
            formula="y = 0.0",
            parameters={"secret_coefficients": 0.5},
        )


def test_provenance_requires_generator_for_system_origin():
    with pytest.raises(ValueError, match="SYSTEM_GENERATED"):
        HypothesisProvenance(origin=HypothesisOrigin.SYSTEM_GENERATED)


def test_provenance_requires_parent_for_refinement():
    with pytest.raises(ValueError, match="REFINED"):
        HypothesisProvenance(origin=HypothesisOrigin.REFINED, generator="refiner")


def test_provenance_rejects_generator_on_researcher_origin():
    with pytest.raises(ValueError, match="RESEARCHER_PROPOSED"):
        HypothesisProvenance(origin=HypothesisOrigin.RESEARCHER_PROPOSED, generator="test_generator")


def test_provenance_researcher_proposal_is_representable():
    provenance = HypothesisProvenance(
        origin=HypothesisOrigin.RESEARCHER_PROPOSED, note="Researcher proposal"
    )
    assert provenance.to_dict() == {
        "origin": "researcher_proposed",
        "note": "Researcher proposal",
    }


def test_hypothesis_payloads_are_immutable_snapshots():
    """Nested payloads are frozen, so post-validation mutation cannot bypass checks."""
    source = {"degree": 1, "coefficients": {"c0": 0.0, "c1": 1.0}}
    hypothesis = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = 0.0 + 1.0*x",
        parameters=source,
        generation_metadata={"tags": ["baseline"]},
    )

    # The caller's dict is copied, not aliased.
    source["degree"] = 9
    source["coefficients"]["c0"] = 5.0
    assert hypothesis.parameters["degree"] == 1
    assert hypothesis.parameters["coefficients"]["c0"] == 0.0

    # In-place mutation of the stored snapshot fails instead of silently succeeding.
    assert isinstance(hypothesis.parameters, Mapping)
    assert not isinstance(hypothesis.parameters, dict)
    with pytest.raises(TypeError):
        hypothesis.parameters["degree"] = 2
    with pytest.raises(TypeError):
        hypothesis.parameters["coefficients"]["c0"] = 5.0
    with pytest.raises(AttributeError):
        hypothesis.generation_metadata["tags"].append("later")


def test_hypothesis_to_dict_returns_a_mutable_copy():
    """Serialization thaws the snapshot so consumers get plain JSON-safe data."""
    hypothesis = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = 0.0",
        parameters={"degree": 0, "coefficients": {"c0": 0.0}},
        generation_metadata={"tags": ["baseline"]},
    )

    data = hypothesis.to_dict()
    assert data["parameters"] == {"degree": 0, "coefficients": {"c0": 0.0}}
    assert isinstance(data["parameters"], dict)
    assert data["generation_metadata"]["tags"] == ["baseline"]

    data["parameters"]["degree"] = 3
    data["generation_metadata"]["tags"].append("mutated")
    assert hypothesis.parameters["degree"] == 0
    assert list(hypothesis.generation_metadata["tags"]) == ["baseline"]
    json.dumps(data)  # returned copy stays JSON serializable


# ==============================================================================
# 2. Polynomial model family
# ==============================================================================

def test_polynomial_predictions_match_analytical_values():
    """Constant, linear, quadratic, and cubic predictions are analytically checked."""
    family = PolynomialFamily(max_degree=4)

    cases = [
        ((2.5,), -3.0, 2.5),  # constant
        ((1.0, -2.0), 4.0, -7.0),  # linear: 1 - 2*4
        ((1.0, 2.0, 3.0), 2.0, 17.0),  # 1 + 2*2 + 3*4
        (CUBIC_COEFFICIENTS, 2.0, -1.0),  # 3 - 4 - 4 + 4
        (CUBIC_COEFFICIENTS, 0.0, 3.0),
    ]

    for coefficients, x, expected in cases:
        prediction, error = family.predict(polynomial_spec(list(coefficients)), {"x": x})
        assert error is None, error.to_dict() if error else None
        assert prediction == {"y": pytest.approx(expected, abs=1e-12)}


def test_polynomial_coefficient_order_is_ascending():
    """coefficients[i] is the weight of x**i, matching the documented convention."""
    family = PolynomialFamily(max_degree=3)
    specification = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": 2,
        "coefficient_order": "ascending",
        "coefficients": {"c0": 1.0, "c1": 2.0, "c2": 3.0},
    }
    model, error = family.parse_specification(specification)
    assert error is None
    assert model.coefficients == (1.0, 2.0, 3.0)
    assert model.coefficient(2) == 3.0
    assert model.to_specification()["coefficients"] == {"c0": 1.0, "c1": 2.0, "c2": 3.0}

    list_prediction, list_error = family.predict(
        {"family": POLYNOMIAL_FAMILY_ID, "degree": 2, "coefficients": [1.0, 2.0, 3.0]}, {"x": 2.0}
    )
    assert list_error is None
    assert list_prediction == family.predict(specification, {"x": 2.0})[0]


def test_polynomial_summary_and_complexity():
    family = PolynomialFamily(max_degree=3)
    model, error = family.parse_specification(polynomial_spec(list(CUBIC_COEFFICIENTS)))
    assert error is None

    summary = model.summarize()
    assert summary.startswith("y = ")
    assert "x^2" in summary
    assert "x^3" in summary
    # Complexity is documented as the number of free coefficients.
    assert model.complexity() == 4


def test_polynomial_rejects_missing_degree():
    family = PolynomialFamily()
    _, error = family.parse_specification({"family": POLYNOMIAL_FAMILY_ID, "coefficients": [1.0]})
    assert error.code == ERROR_MISSING_PARAMETER


def test_polynomial_rejects_missing_coefficient():
    family = PolynomialFamily(max_degree=2)
    _, error = family.parse_specification(
        {"family": POLYNOMIAL_FAMILY_ID, "degree": 2, "coefficients": {"c0": 1.0, "c2": 1.0}}
    )
    assert error.code == ERROR_MISSING_PARAMETER
    assert error.details["missing"] == ["c1"]


def test_polynomial_rejects_coefficient_beyond_degree():
    family = PolynomialFamily(max_degree=3)
    _, error = family.parse_specification(
        {
            "family": POLYNOMIAL_FAMILY_ID,
            "degree": 1,
            "coefficients": {"c0": 1.0, "c1": 1.0, "c2": 1.0},
        }
    )
    assert error.code == ERROR_INVALID_SPECIFICATION
    assert "exceeds the declared degree" in error.message


def test_polynomial_rejects_unknown_specification_keys():
    family = PolynomialFamily()
    specification = polynomial_spec([1.0])
    specification["expression"] = "eval('1+1')"
    _, error = family.parse_specification(specification)
    assert error.code == ERROR_INVALID_SPECIFICATION
    assert error.details["unknown_keys"] == ["expression"]


def test_polynomial_rejects_degree_above_limit():
    family = PolynomialFamily(max_degree=2)
    _, error = family.parse_specification(polynomial_spec([1.0, 1.0, 1.0, 1.0]))
    assert error.code == ERROR_UNSUPPORTED_DEGREE
    assert error.details == {"degree": 3, "max_degree": 2}


def test_polynomial_rejects_zero_leading_coefficient():
    family = PolynomialFamily(max_degree=3)
    _, error = family.parse_specification(polynomial_spec([1.0, 2.0, 0.0], degree=2))
    assert error.code == ERROR_INVALID_SPECIFICATION
    assert "Leading coefficient" in error.message


def test_polynomial_allows_zero_constant_term():
    family = PolynomialFamily(max_degree=0)
    model, error = family.parse_specification(polynomial_spec([0.0]))
    assert error is None
    assert model.degree == 0


def test_polynomial_rejects_boolean_and_string_coefficients():
    family = PolynomialFamily(max_degree=2)

    _, bool_error = family.parse_specification(polynomial_spec([1.0, True, 1.0]))
    assert bool_error.code == ERROR_INVALID_PARAMETER_TYPE

    _, string_error = family.parse_specification(polynomial_spec(["1.0", 1.0, 1.0]))
    assert string_error.code == ERROR_INVALID_PARAMETER_TYPE


def test_polynomial_rejects_non_finite_coefficient():
    family = PolynomialFamily(max_degree=2)
    _, error = family.parse_specification(polynomial_spec([1.0, float("nan"), 1.0]))
    assert error.code == ERROR_NON_FINITE_VALUE

    _, inf_error = family.parse_specification(polynomial_spec([1.0, float("inf"), 1.0]))
    assert inf_error.code == ERROR_NON_FINITE_VALUE


def test_polynomial_rejects_boolean_degree():
    family = PolynomialFamily()
    _, error = family.parse_specification(
        {"family": POLYNOMIAL_FAMILY_ID, "degree": True, "coefficients": [1.0, 1.0]}
    )
    assert error.code == ERROR_INVALID_PARAMETER_TYPE


def test_polynomial_predict_rejects_missing_and_invalid_inputs():
    family = PolynomialFamily(max_degree=2)
    specification = polynomial_spec([1.0, 2.0])

    _, missing = family.predict(specification, {"z": 1.0})
    assert missing.code == ERROR_MISSING_PARAMETER

    _, boolean = family.predict(specification, {"x": True})
    assert boolean.code == ERROR_INVALID_PARAMETER_TYPE

    _, string = family.predict(specification, {"x": "2.0"})
    assert string.code == ERROR_INVALID_PARAMETER_TYPE

    _, non_finite = family.predict(specification, {"x": float("nan")})
    assert non_finite.code == ERROR_NON_FINITE_VALUE

    _, mapping_error = family.predict(specification, [("x", 1.0)])
    assert mapping_error.code == ERROR_INVALID_PARAMETER_TYPE


def test_polynomial_predict_rejects_non_finite_overflow():
    family = PolynomialFamily(max_degree=HARD_MAX_POLYNOMIAL_DEGREE)
    coefficients = [0.0] * 8 + [1.0]
    prediction, error = family.predict(polynomial_spec(coefficients), {"x": 1e200})

    assert prediction is None
    assert error.code == ERROR_NON_FINITE_PREDICTION


def test_polynomial_predict_is_deterministic():
    family = PolynomialFamily(max_degree=3)
    specification = polynomial_spec(list(CUBIC_COEFFICIENTS))

    first, _ = family.predict(specification, {"x": 1.25})
    second, _ = family.predict(specification, {"x": 1.25})
    assert first == second


# ==============================================================================
# 3. Model family registry
# ==============================================================================

def test_registry_parses_and_predicts_through_family_id():
    registry = default_registry(max_degree=3)
    assert registry.family_ids() == (POLYNOMIAL_FAMILY_ID,)

    prediction, error = registry.predict(polynomial_spec([1.0, 2.0]), {"x": 3.0})
    assert error is None
    assert prediction == {"y": 7.0}


def test_registry_rejects_unknown_family():
    registry = default_registry()
    specification = polynomial_spec([1.0])
    specification["family"] = "neural_network"

    _, error = registry.parse_specification(specification)
    assert error.code == ERROR_UNKNOWN_MODEL_FAMILY
    assert error.details["available_families"] == [POLYNOMIAL_FAMILY_ID]

    _, predict_error = registry.predict(specification, {"x": 1.0})
    assert predict_error.code == ERROR_UNKNOWN_MODEL_FAMILY


def test_registry_requires_family_identifier():
    registry = default_registry()
    _, error = registry.parse_specification({"degree": 0, "coefficients": [1.0]})
    assert error.code == ERROR_MISSING_PARAMETER


def test_registry_rejects_duplicate_registration():
    registry = ModelFamilyRegistry([PolynomialFamily()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(PolynomialFamily())


@pytest.mark.parametrize("bad_family", [[], {}, 5, 1.5, False])
def test_registry_rejects_non_string_family_identifier(bad_family):
    """Malformed identifiers must not escape as a raw TypeError from dict lookup."""
    registry = default_registry()
    specification = {"family": bad_family, "degree": 0, "coefficients": [0.0]}

    _, parse_error = registry.parse_specification(specification)
    assert parse_error.code == ERROR_INVALID_PARAMETER_TYPE
    assert parse_error.details == {"received_type": type(bad_family).__name__}

    _, predict_error = registry.predict(specification, {"x": 1.0})
    assert predict_error.code == ERROR_INVALID_PARAMETER_TYPE


def test_registry_still_reports_missing_family_separately():
    registry = default_registry()
    _, error = registry.parse_specification({"degree": 0, "coefficients": [0.0]})
    assert error.code == ERROR_MISSING_PARAMETER


class _ThresholdModel(ValidatedModel):
    """Minimal non-polynomial model used to prove the interface is model-neutral."""

    def __init__(self, threshold: float):
        self.threshold = threshold

    def to_specification(self):
        return {"family": "threshold", "threshold": self.threshold}

    def canonical_key(self):
        return json.dumps(self.to_specification(), sort_keys=True)

    def summarize(self):
        return f"y = 1.0 if x >= {self.threshold} else 0.0"

    def complexity(self):
        return 1


class _ThresholdFamily(HypothesisModelFamily):
    """Second family registered only in tests, with a non-polynomial output shape."""

    family_id = "threshold"

    @property
    def max_complexity(self):
        return 1

    def parse_specification(self, specification):
        if not isinstance(specification, Mapping):
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION, message="Specification must be a mapping."
            )
        threshold = specification.get("threshold")
        if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE, message="threshold must be numeric."
            )
        return _ThresholdModel(float(threshold)), None

    def predict(self, specification, input_values):
        model, error = self.parse_specification(specification)
        if error is not None:
            return None, error
        x = input_values.get("x")
        if not isinstance(x, (int, float)) or isinstance(x, bool):
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE, message="x must be numeric."
            )
        return {"y": 1.0 if x >= model.threshold else 0.0}, None


def test_registry_supports_model_neutral_family_interface():
    """A non-polynomial family registers and predicts without polymorphic leakage."""
    registry = ModelFamilyRegistry([PolynomialFamily(max_degree=2), _ThresholdFamily()])
    assert registry.family_ids() == ("polynomial", "threshold")

    model, error = registry.parse_specification({"family": "threshold", "threshold": 0.5})
    assert error is None
    assert isinstance(model, ValidatedModel)
    assert model.complexity() == 1
    assert model.canonical_key() == '{"family": "threshold", "threshold": 0.5}'
    assert model.summarize().startswith("y = ")

    prediction, predict_error = registry.predict(
        {"family": "threshold", "threshold": 0.5}, {"x": 1.0}
    )
    assert predict_error is None
    assert prediction == {"y": 1.0}


# ==============================================================================
# 4. Candidate generation
# ==============================================================================

def test_generator_produces_unique_valid_candidates():
    config = HypothesisGenerationConfig(
        min_degree=0, max_degree=3, candidate_count=6, seed=7
    )
    candidates = generate_candidates(config)
    registry = default_registry(max_degree=3)

    assert len(candidates) == 6
    assert len({candidate.hypothesis_id for candidate in candidates}) == 6
    assert len({candidate.parameters["degree"] for candidate in candidates}) >= 2

    canonical_keys = {
        json.dumps(candidate.to_dict()["parameters"], sort_keys=True) for candidate in candidates
    }
    assert len(canonical_keys) == len(candidates)

    for candidate in candidates:
        assert candidate.model_family == POLYNOMIAL_FAMILY_ID
        assert registry.validate_specification(candidate.parameters) is None
        assert candidate.formula.startswith("y = ")
        assert candidate.complexity == candidate.parameters["degree"] + 1
        assert candidate.status == HypothesisStatus.CANDIDATE
        assert candidate.provenance.origin == HypothesisOrigin.SYSTEM_GENERATED
        assert candidate.provenance.generator == GENERATOR_NAME
        assert candidate.provenance.generator_version == GENERATOR_VERSION
        assert candidate.generation_metadata["seed"] == 7
        assert candidate.generation_metadata["configuration"]["candidate_count"] == 6
        assert candidate.generation_metadata["sequence_index"] in range(6)


def test_generator_covers_low_degree_baselines():
    config = HypothesisGenerationConfig(min_degree=0, max_degree=2, candidate_count=3, seed=1)
    candidates = generate_candidates(config)
    degrees = [candidate.parameters["degree"] for candidate in candidates]

    assert degrees == [0, 1, 2]
    assert all(
        candidate.generation_metadata["initialization"] == "deterministic_monomial_baseline"
        for candidate in candidates
    )


def test_generator_is_reproducible_with_fixed_seed():
    config = HypothesisGenerationConfig(min_degree=0, max_degree=3, candidate_count=5, seed=42)
    first = generate_candidates(config)
    second = generate_candidates(config)

    assert [candidate.parameters for candidate in first] == [
        candidate.parameters for candidate in second
    ]
    assert [candidate.formula for candidate in first] == [
        candidate.formula for candidate in second
    ]
    # IDs stay unique per batch; reproducibility covers specifications, not identity.
    assert {candidate.hypothesis_id for candidate in first}.isdisjoint(
        {candidate.hypothesis_id for candidate in second}
    )


def test_generator_differs_for_different_seeds():
    first = generate_candidates(
        HypothesisGenerationConfig(candidate_count=5, seed=1)
    )
    second = generate_candidates(
        HypothesisGenerationConfig(candidate_count=5, seed=2)
    )
    assert [candidate.parameters for candidate in first] != [
        candidate.parameters for candidate in second
    ]


def test_generator_respects_degree_limits():
    config = HypothesisGenerationConfig(min_degree=2, max_degree=3, candidate_count=4, seed=3)
    candidates = generate_candidates(config)

    assert len(candidates) == 4
    for candidate in candidates:
        assert candidate.parameters["degree"] in (2, 3)
        assert candidate.parameters["coefficients"][f"c{candidate.parameters['degree']}"] != 0.0


def test_generator_marks_candidates_as_unfitted_proposals():
    candidates = generate_candidates(
        HypothesisGenerationConfig(candidate_count=2, seed=5)
    )
    data = candidates[0].to_dict()

    assert data["status"] == "candidate"
    assert "fitting_metadata" not in data
    assert "uncertainty_metadata" not in data
    assert data["generation_metadata"]["initialization"] in {
        "deterministic_monomial_baseline",
        "seeded_uniform_random",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"candidate_count": 0},
        {"candidate_count": 1000},
        {"min_degree": 2, "max_degree": 1},
        {"min_degree": -1, "max_degree": 1},
        {"max_degree": HARD_MAX_POLYNOMIAL_DEGREE + 1},
        {"coefficient_bound": -1.0},
        {"coefficient_bound": float("nan")},
        {"seed": "seven"},
        {"max_attempts_per_candidate": 0},
        {"family_id": ""},
    ],
)
def test_generator_rejects_invalid_configuration(kwargs):
    with pytest.raises(CandidateGenerationError) as excinfo:
        CandidateGenerator(HypothesisGenerationConfig(**kwargs))
    assert excinfo.value.error.code == ERROR_INVALID_GENERATION_CONFIG


def test_generator_rejects_non_configuration_input():
    with pytest.raises(CandidateGenerationError) as excinfo:
        CandidateGenerator({"candidate_count": 3})
    assert excinfo.value.error.code == ERROR_INVALID_GENERATION_CONFIG


def test_generator_rejects_unknown_family():
    with pytest.raises(CandidateGenerationError) as excinfo:
        CandidateGenerator(HypothesisGenerationConfig(family_id="neural_network"))
    assert excinfo.value.error.code == ERROR_UNKNOWN_MODEL_FAMILY


def test_generator_rejects_degree_above_registry_limit():
    config = HypothesisGenerationConfig(max_degree=3, candidate_count=2, seed=0)
    with pytest.raises(CandidateGenerationError) as excinfo:
        CandidateGenerator(config, registry=default_registry(max_degree=2))
    assert excinfo.value.error.code == ERROR_UNSUPPORTED_DEGREE


def test_generator_reports_candidate_space_exhaustion():
    """With zero coefficient variation only the zero constant is reachable."""
    config = HypothesisGenerationConfig(
        min_degree=0,
        max_degree=0,
        candidate_count=2,
        coefficient_bound=0.0,
        seed=0,
        max_attempts_per_candidate=2,
    )
    with pytest.raises(CandidateGenerationError) as excinfo:
        CandidateGenerator(config).generate()

    error = excinfo.value.error
    assert error.code == ERROR_ATTEMPT_BUDGET_EXHAUSTED
    assert error.details["generated"] == 1
    assert error.details["requested"] == 2
    assert error.details["attempt_budget"] == 4
    assert "does not prove" in error.message


def test_generator_duplicate_detection_uses_canonical_specification():
    """The baseline candidate is not duplicated by an equal random draw."""
    config = HypothesisGenerationConfig(
        min_degree=0,
        max_degree=0,
        candidate_count=2,
        coefficient_bound=1.0,
        seed=0,
    )
    candidates = CandidateGenerator(config).generate()

    specifications = [candidate.parameters for candidate in candidates]
    assert len(specifications) == 2
    assert specifications[0] != specifications[1]


# ==============================================================================
# 5. Isolation from oracle and evaluator
# ==============================================================================

def test_generator_public_interface_excludes_oracle():
    parameters = set(inspect.signature(CandidateGenerator.__init__).parameters)
    assert parameters == {"self", "config", "registry"}


def test_generation_module_does_not_import_oracle_or_evaluator():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    program = (
        "import sys\n"
        "import app.domain.candidate_generator\n"
        "forbidden = [name for name in sys.modules "
        "if name.startswith('app.adapters') or name.startswith('app.evaluator')]\n"
        "assert not forbidden, forbidden\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=backend_dir, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
