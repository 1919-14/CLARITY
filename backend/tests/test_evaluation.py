"""Fitting and evaluation pipeline tests for Issue #6.

Covers:
1. MAE/RMSE known answers and invalid-input handling.
2. Polynomial least-squares fitting: exact recovery, overdetermined fits,
   coefficient order, insufficient/rank-deficient data, configuration errors.
3. Observation validation and explicit exclusion accounting.
4. Train/held-out orchestration and held-out leakage invariants (A, B, E).
5. Failure isolation and standardized result serialization.
"""

import json
import math
import os
import subprocess
import sys

import pytest

from app.domain.contracts import (
    EvaluationRequest,
    EvaluationStatus,
    FittingStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    StructuredError,
)
from app.domain.evaluation import (
    ERROR_DATASET_LENGTH_MISMATCH,
    ERROR_EMPTY_DATASET,
    ERROR_INVALID_EVALUATION_REQUEST,
    ERROR_INVALID_METRIC_INPUT,
    ERROR_METRIC_COMPUTATION_FAILURE,
    ERROR_NON_FINITE_METRIC,
    ERROR_NON_FINITE_METRIC_INPUT,
    ERROR_PREDICTION_FAILURE,
    ERROR_UNEXPECTED_EVALUATION_ERROR,
    ERROR_UNSUPPORTED_METRIC,
    METRIC_DEFINITIONS,
    METRIC_VERSION,
    SUPPORTED_METRICS,
    EvaluationPipeline,
    compute_metrics,
    mean_absolute_error,
    root_mean_squared_error,
    select_samples,
)
from app.domain.fitting import (
    ERROR_DEGENERATE_FIT,
    ERROR_FITTING_NUMERICAL_FAILURE,
    ERROR_INSUFFICIENT_OBSERVATIONS,
    ERROR_INVALID_FITTING_CONFIG,
    ERROR_NON_FINITE_COEFFICIENT,
    ERROR_RANK_DEFICIENT_FIT,
    ERROR_UNSUPPORTED_FITTING_METHOD,
    ERROR_UNSUPPORTED_MODEL_FAMILY,
    ERROR_UNKNOWN_MODEL_FAMILY,
    FITTING_METHOD_LEAST_SQUARES,
    FamilyFitter,
    FittingConfig,
    ObservationSample,
    PolynomialLeastSquaresFitter,
    default_fitters,
    fit_candidate,
)
from app.domain.hypothesis_models import (
    ERROR_INVALID_PARAMETER_TYPE,
    ERROR_NON_FINITE_VALUE,
    POLYNOMIAL_FAMILY_ID,
    default_registry,
)


CUBIC_COEFFICIENTS = (3.0, -2.0, -1.0, 0.5)


def truth(x):
    """Hidden rule used only to generate test fixtures (never passed to fitting)."""
    return 3.0 - (2.0 * x) - (x ** 2) + (0.5 * (x ** 3))


def polynomial_candidate(degree=3, hypothesis_id="hyp-fit"):
    """Candidate with placeholder coefficients; fitting replaces them."""
    coefficients = [0.0] * (degree + 1)
    coefficients[degree] = 1.0
    specification = {
        "family": POLYNOMIAL_FAMILY_ID,
        "degree": degree,
        "coefficient_order": "ascending",
        "coefficients": {f"c{index}": value for index, value in enumerate(coefficients)},
        "input_variable": "x",
        "output_variable": "y",
    }
    return HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = placeholder",
        parameters=specification,
        complexity=degree + 1,
        hypothesis_id=hypothesis_id,
    )


def success_observation(observation_id, x, y):
    return Observation(
        observation_id=observation_id,
        input_id=f"inp-{observation_id}",
        input_values={"x": x},
        output_values={"y": y},
        status=ObservationStatus.SUCCESS,
        sequence_number=1,
    )


def failed_observation(observation_id, x):
    return Observation(
        observation_id=observation_id,
        input_id=f"inp-{observation_id}",
        input_values={"x": x},
        output_values=None,
        status=ObservationStatus.ORACLE_FAILURE,
        error=StructuredError(code="ORACLE_DOWN", message="oracle unavailable"),
        sequence_number=1,
    )


def build_observations(points, offset=0.0):
    """points: list of (x, y) -> observation mapping keyed by obs-<index>."""
    return {
        f"obs-{index}": success_observation(f"obs-{index}", x, y + offset)
        for index, (x, y) in enumerate(points)
    }


def make_request(candidate, training_ids, held_out_ids=None, metric_names=None):
    kwargs = {}
    if metric_names is not None:
        kwargs["metric_names"] = metric_names
    return EvaluationRequest(
        hypothesis=candidate,
        observation_ids=list(training_ids),
        held_out_observation_ids=list(held_out_ids or []),
        **kwargs,
    )


TRAINING_POINTS = [(-2.0, truth(-2.0)), (-1.0, truth(-1.0)), (0.0, truth(0.0)), (1.0, truth(1.0)), (2.0, truth(2.0))]
HELD_OUT_POINTS = [(-3.0, truth(-3.0)), (3.0, truth(3.0)), (4.0, truth(4.0))]
TRAINING_IDS = [f"obs-{index}" for index in range(len(TRAINING_POINTS))]
HELD_OUT_IDS = [f"obs-{len(TRAINING_POINTS) + index}" for index in range(len(HELD_OUT_POINTS))]


# ==============================================================================
# 1. Metrics
# ==============================================================================

def test_mae_known_answer():
    value, error = mean_absolute_error([3, 5], [2, 7])
    assert error is None
    assert value == pytest.approx(1.5)


def test_rmse_known_answer():
    value, error = root_mean_squared_error([3, 5], [2, 7])
    assert error is None
    assert value == pytest.approx(math.sqrt(2.5))
    assert value == pytest.approx(1.5811388300841898)


def test_perfect_predictions_have_zero_error():
    assert mean_absolute_error([1.0, -2.0], [1.0, -2.0])[0] == 0.0
    assert root_mean_squared_error([1.0, -2.0], [1.0, -2.0])[0] == 0.0


@pytest.mark.parametrize("metric", [mean_absolute_error, root_mean_squared_error])
def test_metrics_reject_empty_datasets(metric):
    value, error = metric([], [])
    assert value is None
    assert error.code == ERROR_EMPTY_DATASET


@pytest.mark.parametrize("metric", [mean_absolute_error, root_mean_squared_error])
def test_metrics_reject_length_mismatch(metric):
    value, error = metric([1.0, 2.0], [1.0])
    assert value is None
    assert error.code == ERROR_DATASET_LENGTH_MISMATCH


@pytest.mark.parametrize(
    "actuals,predictions",
    [
        ([True], [1.0]),
        ([1.0], ["1.0"]),
        ([None], [1.0]),
        ([1.0, 2.0], ["a", "b"]),
    ],
)
def test_metrics_reject_booleans_and_non_numeric_values(actuals, predictions):
    _, error = mean_absolute_error(actuals, predictions)
    assert error.code == ERROR_INVALID_METRIC_INPUT

    _, rmse_error = root_mean_squared_error(actuals, predictions)
    assert rmse_error.code == ERROR_INVALID_METRIC_INPUT


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
def test_metrics_reject_non_finite_inputs(bad_value):
    _, error = mean_absolute_error([bad_value], [1.0])
    assert error.code == ERROR_NON_FINITE_METRIC_INPUT

    _, prediction_error = root_mean_squared_error([1.0], [bad_value])
    assert prediction_error.code == ERROR_NON_FINITE_METRIC_INPUT


def test_metrics_reject_non_finite_overflow():
    _, mae_error = mean_absolute_error([1e308], [-1e308])
    assert mae_error.code == ERROR_NON_FINITE_METRIC

    _, rmse_error = root_mean_squared_error([1e308], [-1e308])
    assert rmse_error.code == ERROR_NON_FINITE_METRIC


def test_metrics_reject_non_sequence_inputs():
    _, error = mean_absolute_error(42, [1.0])
    assert error.code == ERROR_INVALID_METRIC_INPUT


def test_compute_metrics_known_answer_and_definitions():
    metrics, error = compute_metrics([3, 5], [2, 7], ["mae", "rmse"])
    assert error is None
    assert metrics["mae"] == pytest.approx(1.5)
    assert metrics["rmse"] == pytest.approx(math.sqrt(2.5))
    assert set(SUPPORTED_METRICS) == {"mae", "rmse"}
    assert set(METRIC_DEFINITIONS) == {"mae", "rmse"}


def test_compute_metrics_rejects_unknown_metric():
    metrics, error = compute_metrics([1.0], [1.0], ["accuracy"])
    assert metrics == {}
    assert error.code == ERROR_UNSUPPORTED_METRIC


def test_compute_metrics_rejects_empty_metric_names():
    metrics, error = compute_metrics([1.0], [1.0], [])
    assert metrics == {}
    assert error.code == ERROR_INVALID_EVALUATION_REQUEST


# ==============================================================================
# 2. Polynomial fitting
# ==============================================================================

def samples_from(points):
    return [
        ObservationSample(observation_id=f"obs-{index}", input_value=float(x), output_value=float(y))
        for index, (x, y) in enumerate(points)
    ]


def test_least_squares_recovers_exact_cubic_coefficients():
    candidate = polynomial_candidate(degree=3)
    result = fit_candidate(candidate, samples_from(TRAINING_POINTS))

    assert result.status == FittingStatus.SUCCESS
    assert result.error is None
    assert result.method == FITTING_METHOD_LEAST_SQUARES
    assert result.sample_count == len(TRAINING_POINTS)
    assert result.metadata["exactly_determined"] is False
    assert result.warnings == ()

    coefficients = result.specification["coefficients"]
    for index, expected in enumerate(CUBIC_COEFFICIENTS):
        assert coefficients[f"c{index}"] == pytest.approx(expected, abs=1e-9)
    assert result.specification["degree"] == 3
    assert result.specification["coefficient_order"] == "ascending"
    assert default_registry().validate_specification(result.specification) is None


def test_least_squares_overdetermined_linear_fit():
    """Points (0,1), (1,3), (2,4) give slope 1.5 and intercept 7/6."""
    candidate = polynomial_candidate(degree=1)
    result = fit_candidate(candidate, samples_from([(0.0, 1.0), (1.0, 3.0), (2.0, 4.0)]))

    assert result.status == FittingStatus.SUCCESS
    coefficients = result.specification["coefficients"]
    assert coefficients["c1"] == pytest.approx(1.5, abs=1e-12)
    assert coefficients["c0"] == pytest.approx(7.0 / 6.0, abs=1e-12)


def test_exactly_determined_fit_warns_but_succeeds():
    candidate = polynomial_candidate(degree=2)
    result = fit_candidate(candidate, samples_from([(0.0, 1.0), (1.0, 4.0), (2.0, 9.0)]))

    assert result.status == FittingStatus.SUCCESS
    assert result.metadata["exactly_determined"] is True
    assert [warning.code for warning in result.warnings] == ["EXACTLY_DETERMINED_FIT"]


def test_fit_requires_enough_observations():
    candidate = polynomial_candidate(degree=2)
    result = fit_candidate(candidate, samples_from([(0.0, 1.0), (1.0, 2.0)]))

    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_INSUFFICIENT_OBSERVATIONS
    assert result.error.details == {"required": 3, "received": 2, "degree": 2}
    assert result.specification is None


def test_fit_detects_rank_deficient_inputs():
    candidate = polynomial_candidate(degree=2)
    points = [(2.0, 4.0), (2.0, 5.0), (2.0, 6.0), (2.0, 7.0)]
    result = fit_candidate(candidate, samples_from(points))

    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_RANK_DEFICIENT_FIT
    assert result.specification is None


def test_fit_rejects_degenerate_zero_leading_coefficient():
    """A degree-1 fit of constant data must fail, not silently become degree 0."""
    candidate = polynomial_candidate(degree=1)
    result = fit_candidate(candidate, samples_from([(0.0, 5.0), (2.0, 5.0)]))

    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_DEGENERATE_FIT


def test_fit_rejects_unsupported_method():
    result = fit_candidate(
        polynomial_candidate(degree=1),
        samples_from([(0.0, 1.0), (1.0, 2.0)]),
        FittingConfig(method="gradient_descent"),
    )
    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_UNSUPPORTED_FITTING_METHOD


def test_fit_rejects_invalid_configuration():
    result = fit_candidate(
        polynomial_candidate(degree=1),
        samples_from([(0.0, 1.0), (1.0, 2.0)]),
        FittingConfig(rank_tolerance=0.0),
    )
    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_INVALID_FITTING_CONFIG


def test_fit_rejects_unknown_family():
    candidate = HypothesisRepresentation(
        model_family="unknown_family",
        formula="y = 1.0",
        parameters={"degree": 0},
        hypothesis_id="hyp-unknown-family",
    )
    result = fit_candidate(candidate, samples_from([(0.0, 1.0), (1.0, 2.0)]))

    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_UNKNOWN_MODEL_FAMILY


def test_fit_rejects_family_without_registered_fitter():
    result = fit_candidate(
        polynomial_candidate(degree=1),
        samples_from([(0.0, 1.0), (1.0, 2.0)]),
        fitters={},
    )
    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_UNSUPPORTED_MODEL_FAMILY


def test_fit_rejects_invalid_specification():
    candidate = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = invalid",
        parameters={"family": POLYNOMIAL_FAMILY_ID, "degree": 0, "coefficients": {"c0": "bad"}},
        hypothesis_id="hyp-bad-spec",
    )
    result = fit_candidate(candidate, samples_from([(0.0, 1.0)]))

    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_INVALID_PARAMETER_TYPE


def test_fit_rejects_non_finite_training_sample():
    result = fit_candidate(
        polynomial_candidate(degree=1),
        [
            ObservationSample("obs-a", 0.0, 1.0),
            ObservationSample("obs-b", 1.0, float("nan")),
        ],
    )
    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_NON_FINITE_VALUE


def test_fit_rejects_non_sample_objects():
    result = fit_candidate(polynomial_candidate(degree=1), [(0.0, 1.0), (1.0, 2.0)])
    assert result.status == FittingStatus.FAILED
    assert result.error.code == ERROR_INVALID_PARAMETER_TYPE


def test_fit_does_not_mutate_candidate():
    candidate = polynomial_candidate(degree=3)
    original = candidate.to_dict()
    fit_candidate(candidate, samples_from(TRAINING_POINTS))
    assert candidate.to_dict() == original


# ==============================================================================
# 3. Observation validation
# ==============================================================================

def test_select_samples_accepts_valid_and_records_exclusions():
    observations = build_observations([(0.0, 1.0), (1.0, 2.0)])
    observations["obs-bad-status"] = failed_observation("obs-bad-status", 2.0)
    observations["obs-nan"] = success_observation("obs-nan", 3.0, float("nan"))
    observations["obs-bool"] = success_observation("obs-bool", 4.0, True)
    observations["obs-missing-output"] = Observation(
        observation_id="obs-missing-output",
        input_id="inp-missing-output",
        input_values={"x": 5.0},
        output_values={},
        sequence_number=1,
    )

    selection = select_samples(
        [
            "obs-0",
            "obs-1",
            "obs-bad-status",
            "obs-nan",
            "obs-bool",
            "obs-missing-output",
            "obs-absent",
        ],
        observations,
        "x",
        "y",
    )

    assert selection.requested_count == 7
    assert selection.accepted_count == 2
    assert selection.excluded_count == 5
    assert dict(selection.exclusion_codes) == {
        "missing_observation": 1,
        "observation_oracle_failure": 1,
        "non_finite_output": 1,
        "invalid_output_type": 1,
        "missing_output_value": 1,
    }


def test_select_samples_rejects_boolean_and_non_finite_inputs():
    observations = {
        "obs-bool": success_observation("obs-bool", True, 1.0),
        "obs-nan": success_observation("obs-nan", float("inf"), 1.0),
        "obs-string": success_observation("obs-string", "1.0", 1.0),
    }
    selection = select_samples(list(observations), observations, "x", "y")

    assert selection.accepted_count == 0
    assert dict(selection.exclusion_codes) == {
        "invalid_input_type": 2,
        "non_finite_input": 1,
    }


# ==============================================================================
# 4. Evaluation pipeline and held-out separation
# ==============================================================================

def test_pipeline_computes_training_and_held_out_metrics_separately():
    candidate = polynomial_candidate(degree=3)
    observations = dict(build_observations(TRAINING_POINTS))
    # Held-out outputs are shifted by 2 so held-out metrics must reflect the
    # held-out data, not the training fit.
    for index, point in enumerate(HELD_OUT_POINTS):
        observation_id = HELD_OUT_IDS[index]
        observations[observation_id] = success_observation(
            observation_id, point[0], point[1] + 2.0
        )

    request = make_request(candidate, TRAINING_IDS, HELD_OUT_IDS)
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(request, observations, dataset_id="synthetic-cubic")

    assert result.status == EvaluationStatus.COMPLETED
    assert result.error is None
    assert result.model_family == POLYNOMIAL_FAMILY_ID
    assert result.fitting_status == FittingStatus.SUCCESS
    assert result.fitting_method == FITTING_METHOD_LEAST_SQUARES
    assert result.sample_count == len(TRAINING_POINTS)
    assert result.held_out_sample_count == len(HELD_OUT_POINTS)
    assert result.metrics["mae"] == pytest.approx(0.0, abs=1e-9)
    assert result.metrics["rmse"] == pytest.approx(0.0, abs=1e-9)
    assert result.held_out_metrics["mae"] == pytest.approx(2.0, abs=1e-6)
    assert result.held_out_metrics["rmse"] == pytest.approx(2.0, abs=1e-6)
    assert result.observation_counts == {
        "requested_training": 5,
        "accepted_training": 5,
        "excluded_training": 0,
        "requested_held_out": 3,
        "accepted_held_out": 3,
        "excluded_held_out": 0,
    }
    assert result.metric_version == METRIC_VERSION
    assert result.metric_definitions == {
        name: METRIC_DEFINITIONS[name] for name in ("mae", "rmse")
    }
    assert result.dataset_ids["dataset_id"] == "synthetic-cubic"
    assert result.dataset_ids["training_observation_ids"] == TRAINING_IDS
    assert result.complexity == 4
    assert result.fitted_specification["degree"] == 3
    assert result.request_id == request.request_id


def test_changing_held_out_outputs_does_not_change_fitted_parameters():
    """Invariant A: held-out targets cannot influence fitting."""
    candidate = polynomial_candidate(degree=3)
    training = build_observations(TRAINING_POINTS)

    def run(offset):
        observations = dict(training)
        for index, point in enumerate(HELD_OUT_POINTS):
            observation_id = HELD_OUT_IDS[index]
            observations[observation_id] = success_observation(
                observation_id, point[0], point[1] + offset
            )
        pipeline = EvaluationPipeline()
        return pipeline.evaluate(
            make_request(candidate, TRAINING_IDS, HELD_OUT_IDS), observations
        )

    baseline = run(0.0)
    shifted = run(100.0)

    assert baseline.status == EvaluationStatus.COMPLETED
    assert shifted.status == EvaluationStatus.COMPLETED
    assert baseline.fitted_specification == shifted.fitted_specification
    assert baseline.metrics == shifted.metrics
    assert baseline.held_out_metrics != shifted.held_out_metrics
    assert shifted.held_out_metrics["mae"] == pytest.approx(100.0, abs=1e-6)


class RecordingFitter(FamilyFitter):
    """Spy fitter recording exactly which observation IDs reach fitting."""

    family_id = POLYNOMIAL_FAMILY_ID

    def __init__(self):
        self.inner = PolynomialLeastSquaresFitter()
        self.calls = []

    def fit(self, specification, training_samples, config, family):
        self.calls.append([sample.observation_id for sample in training_samples])
        return self.inner.fit(specification, training_samples, config, family)


def test_fitter_receives_training_observations_only():
    """Invariant B: the fitter's input contains training IDs only."""
    spy = RecordingFitter()
    pipeline = EvaluationPipeline(fitters={POLYNOMIAL_FAMILY_ID: spy})
    observations = {
        **build_observations(TRAINING_POINTS),
        **{
            HELD_OUT_IDS[index]: success_observation(HELD_OUT_IDS[index], point[0], point[1])
            for index, point in enumerate(HELD_OUT_POINTS)
        },
    }

    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS, HELD_OUT_IDS), observations
    )

    assert result.status == EvaluationStatus.COMPLETED
    assert spy.calls == [TRAINING_IDS]
    assert set(spy.calls[0]).isdisjoint(HELD_OUT_IDS)


def test_held_out_evaluation_does_not_refit():
    """Invariant E: exactly one fit happens regardless of held-out size."""
    spy = RecordingFitter()
    pipeline = EvaluationPipeline(fitters={POLYNOMIAL_FAMILY_ID: spy})
    observations = dict(build_observations(TRAINING_POINTS))
    for index, point in enumerate(HELD_OUT_POINTS):
        observations[HELD_OUT_IDS[index]] = success_observation(
            HELD_OUT_IDS[index], point[0], point[1]
        )

    pipeline.evaluate(make_request(polynomial_candidate(degree=3), TRAINING_IDS, HELD_OUT_IDS), observations)

    assert len(spy.calls) == 1


def test_held_out_unavailable_is_reported_not_zero():
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS, []),
        build_observations(TRAINING_POINTS),
    )

    assert result.status == EvaluationStatus.COMPLETED
    assert result.held_out_metrics is None
    assert result.held_out_sample_count == 0
    assert [warning.code for warning in result.warnings] == ["HELD_OUT_UNAVAILABLE"]


def test_failed_fit_produces_no_metrics_or_fitted_specification():
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=2), ["obs-0", "obs-1"]),
        build_observations([(0.0, 1.0), (1.0, 2.0)]),
    )

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_INSUFFICIENT_OBSERVATIONS
    assert result.fitting_status == FittingStatus.FAILED
    assert result.metrics == {}
    assert result.held_out_metrics is None
    assert result.fitted_specification is None
    assert result.sample_count == 0


def test_empty_training_dataset_fails_explicitly():
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(make_request(polynomial_candidate(degree=0), []), {})

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_INSUFFICIENT_OBSERVATIONS
    assert result.metrics == {}


def test_invalid_predictions_fail_the_candidate():
    pipeline = EvaluationPipeline()
    observations = dict(build_observations(TRAINING_POINTS))
    observations["obs-extrapolation"] = success_observation("obs-extrapolation", 1e200, 0.0)

    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS, ["obs-extrapolation"]),
        observations,
    )

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_PREDICTION_FAILURE
    assert result.error.details["partition"] == "held_out"
    assert result.metrics == {}


def test_unsupported_metric_name_fails_before_fitting():
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(
        make_request(
            polynomial_candidate(degree=3),
            TRAINING_IDS,
            metric_names=["accuracy"],
        ),
        build_observations(TRAINING_POINTS),
    )

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_UNSUPPORTED_METRIC


def test_duplicate_training_observation_ids_are_rejected():
    pipeline = EvaluationPipeline()
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), ["obs-0", "obs-0", "obs-1"]),
        build_observations(TRAINING_POINTS),
    )

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_INVALID_EVALUATION_REQUEST


def test_excluded_observations_are_counted_not_silently_dropped():
    pipeline = EvaluationPipeline()
    observations = dict(build_observations(TRAINING_POINTS))
    observations["obs-failed"] = failed_observation("obs-failed", 3.0)
    observations["obs-nan"] = success_observation("obs-nan", 3.0, float("nan"))

    training_ids = TRAINING_IDS + ["obs-failed", "obs-nan", "obs-absent"]
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), training_ids), observations
    )

    assert result.status == EvaluationStatus.COMPLETED
    assert result.sample_count == len(TRAINING_POINTS)
    assert result.observation_counts["requested_training"] == len(training_ids)
    assert result.observation_counts["excluded_training"] == 3
    assert result.exclusion_codes == {
        "observation_oracle_failure": 1,
        "non_finite_output": 1,
        "missing_observation": 1,
    }


def test_arithmetic_error_inside_fitter_becomes_structured_failure():
    class ExplodingFitter(FamilyFitter):
        family_id = POLYNOMIAL_FAMILY_ID

        def fit(self, specification, training_samples, config, family):
            raise ZeroDivisionError("synthetic numerical failure")

    pipeline = EvaluationPipeline(fitters={POLYNOMIAL_FAMILY_ID: ExplodingFitter()})
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS),
        build_observations(TRAINING_POINTS),
    )

    assert result.status == EvaluationStatus.FAILED
    assert result.error.code == ERROR_FITTING_NUMERICAL_FAILURE


def test_one_candidate_failure_does_not_stop_others():
    invalid_candidate = HypothesisRepresentation(
        model_family=POLYNOMIAL_FAMILY_ID,
        formula="y = broken",
        parameters={"family": POLYNOMIAL_FAMILY_ID, "degree": 0, "coefficients": {"c0": "bad"}},
        hypothesis_id="hyp-invalid",
    )
    valid_candidate = polynomial_candidate(degree=3, hypothesis_id="hyp-valid")
    requests = [
        make_request(invalid_candidate, TRAINING_IDS),
        make_request(valid_candidate, TRAINING_IDS),
    ]

    results = EvaluationPipeline().evaluate_many(requests, build_observations(TRAINING_POINTS))

    assert [result.status for result in results] == [
        EvaluationStatus.FAILED,
        EvaluationStatus.COMPLETED,
    ]
    assert results[0].hypothesis_id == "hyp-invalid"
    assert results[1].hypothesis_id == "hyp-valid"
    assert results[1].metrics["mae"] == pytest.approx(0.0, abs=1e-9)


def test_unexpected_fitter_failure_is_isolated_per_candidate():
    class PickyFitter(FamilyFitter):
        family_id = POLYNOMIAL_FAMILY_ID

        def fit(self, specification, training_samples, config, family):
            if specification.get("degree") == 1:
                raise RuntimeError("synthetic programming failure")
            return PolynomialLeastSquaresFitter().fit(
                specification, training_samples, config, family
            )

    candidates = [
        polynomial_candidate(degree=1, hypothesis_id="hyp-explode"),
        polynomial_candidate(degree=3, hypothesis_id="hyp-survives"),
    ]
    pipeline = EvaluationPipeline(fitters={POLYNOMIAL_FAMILY_ID: PickyFitter()})
    results = pipeline.evaluate_many(
        [make_request(candidate, TRAINING_IDS) for candidate in candidates],
        build_observations(TRAINING_POINTS),
    )

    assert results[0].status == EvaluationStatus.FAILED
    assert results[0].error.code == ERROR_UNEXPECTED_EVALUATION_ERROR
    assert results[1].status == EvaluationStatus.COMPLETED


def test_evaluation_is_reproducible_for_same_data_and_config():
    observations = {
        **build_observations(TRAINING_POINTS),
        **{
            HELD_OUT_IDS[index]: success_observation(HELD_OUT_IDS[index], point[0], point[1])
            for index, point in enumerate(HELD_OUT_POINTS)
        },
    }
    request = make_request(polynomial_candidate(degree=3), TRAINING_IDS, HELD_OUT_IDS)

    first = EvaluationPipeline().evaluate(request, observations)
    second = EvaluationPipeline().evaluate(request, observations)

    assert first.fitted_specification == second.fitted_specification
    assert first.metrics == second.metrics
    assert first.held_out_metrics == second.held_out_metrics
    assert first.evaluation_id != second.evaluation_id


def test_successful_result_serializes_with_all_required_fields():
    observations = {
        **build_observations(TRAINING_POINTS),
        **{
            HELD_OUT_IDS[index]: success_observation(HELD_OUT_IDS[index], point[0], point[1])
            for index, point in enumerate(HELD_OUT_POINTS)
        },
    }
    result = EvaluationPipeline().evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS, HELD_OUT_IDS),
        observations,
        dataset_id="serialization-check",
    )
    payload = result.to_dict()

    assert payload["status"] == "completed"
    assert payload["fitting_status"] == "success"
    assert payload["model_family"] == POLYNOMIAL_FAMILY_ID
    assert payload["sample_count"] == len(TRAINING_POINTS)
    assert payload["held_out_sample_count"] == len(HELD_OUT_POINTS)
    assert payload["observation_counts"]["accepted_training"] == len(TRAINING_POINTS)
    assert payload["metric_version"] == METRIC_VERSION
    assert payload["dataset_ids"]["dataset_id"] == "serialization-check"
    assert payload["fitted_specification"]["degree"] == 3
    assert payload["complexity"] == 4
    json.dumps(payload)


def test_failed_result_serializes_without_favorable_metrics():
    result = EvaluationPipeline().evaluate(
        make_request(polynomial_candidate(degree=2), ["obs-0"]),
        build_observations([(1.0, 2.0)]),
    )
    payload = result.to_dict()

    assert payload["status"] == "failed"
    assert payload["metrics"] == {}
    assert payload["fitting_status"] == "failed"
    assert payload["error"]["code"] == ERROR_INSUFFICIENT_OBSERVATIONS
    assert "held_out_metrics" not in payload
    assert "fitted_specification" not in payload
    json.dumps(payload)


def test_fitting_config_is_recorded_on_the_result():
    pipeline = EvaluationPipeline(fitting_config=FittingConfig(rank_tolerance=1e-8))
    result = pipeline.evaluate(
        make_request(polynomial_candidate(degree=3), TRAINING_IDS),
        build_observations(TRAINING_POINTS),
    )

    assert result.fitting_config == {"method": "least_squares", "rank_tolerance": 1e-8}


def test_invalid_pipeline_configuration_is_rejected_early():
    with pytest.raises(ValueError, match="Invalid fitting configuration"):
        EvaluationPipeline(fitting_config=FittingConfig(rank_tolerance=-1.0))


def test_default_fitters_cover_polynomial_family():
    fitters = default_fitters()
    assert set(fitters) == {POLYNOMIAL_FAMILY_ID}
    assert isinstance(fitters[POLYNOMIAL_FAMILY_ID], PolynomialLeastSquaresFitter)


# ==============================================================================
# 5. Isolation
# ==============================================================================

def test_evaluation_module_does_not_import_oracle_or_evaluator():
    backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    program = (
        "import sys\n"
        "import app.domain.evaluation\n"
        "forbidden = [name for name in sys.modules "
        "if name.startswith('app.adapters') or name.startswith('app.evaluator')]\n"
        "assert not forbidden, forbidden\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=backend_dir, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
