"""Fitting/evaluation orchestration, observation validation, and metrics (Issue #6).

Training data fits the model; held-out data only tests generalization.

Data flow enforced by this module:
  1. Validate the candidate and the evaluation request.
  2. Partition observations by the request's explicit training and held-out IDs.
  3. Build validated numeric samples for each partition.
  4. Fit using *only* the training samples (fitters never receive held-out data).
  5. Predict on training samples, then on held-out samples, using the same fitted
     specification. Held-out evaluation never refits or mutates the model.
  6. Build a standardized EvaluationResult.

This module intentionally has no concept of choosing the best candidate: ranking
on held-out scores belongs to a later issue. Its separation is a verifiable data
flow guarantee, not a security sandbox.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    FittingStatus,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    StructuredError,
)
from .fitting import (
    ERROR_FITTING_NUMERICAL_FAILURE,
    FamilyFitter,
    FittingConfig,
    FittingResult,
    ObservationSample,
    default_fitters,
    fit_candidate,
)
from .hypothesis_models import (
    ERROR_UNKNOWN_MODEL_FAMILY,
    ModelFamilyRegistry,
    default_registry,
)

METRIC_MAE = "mae"
METRIC_RMSE = "rmse"
METRIC_VERSION = "1.0"

METRIC_DEFINITIONS: Dict[str, str] = {
    METRIC_MAE: "MAE v1.0: mean(|actual - predicted|) over accepted observations.",
    METRIC_RMSE: "RMSE v1.0: sqrt(mean((actual - predicted)^2)) over accepted observations.",
}

ERROR_INVALID_EVALUATION_REQUEST = "INVALID_EVALUATION_REQUEST"
ERROR_UNSUPPORTED_METRIC = "UNSUPPORTED_METRIC"
ERROR_EMPTY_DATASET = "EMPTY_DATASET"
ERROR_DATASET_LENGTH_MISMATCH = "DATASET_LENGTH_MISMATCH"
ERROR_INVALID_METRIC_INPUT = "INVALID_METRIC_INPUT"
ERROR_NON_FINITE_METRIC_INPUT = "NON_FINITE_METRIC_INPUT"
ERROR_NON_FINITE_METRIC = "NON_FINITE_METRIC"
ERROR_PREDICTION_FAILURE = "PREDICTION_FAILURE"
ERROR_METRIC_COMPUTATION_FAILURE = "METRIC_COMPUTATION_FAILURE"
ERROR_UNEXPECTED_EVALUATION_ERROR = "UNEXPECTED_EVALUATION_ERROR"

WARNING_HELD_OUT_UNAVAILABLE = "HELD_OUT_UNAVAILABLE"


def _validate_metric_pairs(
    actuals: Sequence[float], predictions: Sequence[float]
) -> Tuple[Optional[List[float]], Optional[List[float]], Optional[StructuredError]]:
    """Validate paired numeric inputs for a metric without coercing them."""
    for label, values in (("actuals", actuals), ("predictions", predictions)):
        if (
            not isinstance(values, Sequence)
            or isinstance(values, (str, bytes, bytearray))
            or isinstance(values, Mapping)
        ):
            return None, None, StructuredError(
                code=ERROR_INVALID_METRIC_INPUT,
                message=f"Metric {label} must be a sequence of numbers.",
                details={"argument": label, "received_type": type(values).__name__},
            )

    actual_values = list(actuals)
    predicted_values = list(predictions)
    if not actual_values or not predicted_values:
        return None, None, StructuredError(
            code=ERROR_EMPTY_DATASET,
            message="Metric requires at least one observation; empty datasets have no metric.",
            details={
                "actual_count": len(actual_values),
                "prediction_count": len(predicted_values),
            },
        )
    if len(actual_values) != len(predicted_values):
        return None, None, StructuredError(
            code=ERROR_DATASET_LENGTH_MISMATCH,
            message="Actual and predicted sequences must have the same length.",
            details={
                "actual_count": len(actual_values),
                "prediction_count": len(predicted_values),
            },
        )

    for index, (actual, predicted) in enumerate(zip(actual_values, predicted_values)):
        for label, value in (("actual", actual), ("predicted", predicted)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, None, StructuredError(
                    code=ERROR_INVALID_METRIC_INPUT,
                    message=(
                        f"Metric {label} value at index {index} must be a real number, "
                        f"got {type(value).__name__}."
                    ),
                    details={"index": index, "argument": label},
                )
            try:
                converted = float(value)
            except OverflowError:
                return None, None, StructuredError(
                    code=ERROR_NON_FINITE_METRIC_INPUT,
                    message=(
                        f"Metric {label} value at index {index} overflows float; "
                        "refusing to report a metric."
                    ),
                    details={"index": index, "argument": label},
                )
            if not math.isfinite(converted):
                return None, None, StructuredError(
                    code=ERROR_NON_FINITE_METRIC_INPUT,
                    message=f"Metric {label} value at index {index} must be finite.",
                    details={"index": index, "argument": label},
                )

    try:
        floated_actuals = [float(value) for value in actual_values]
        floated_predictions = [float(value) for value in predicted_values]
    except OverflowError:
        return None, None, StructuredError(
            code=ERROR_NON_FINITE_METRIC_INPUT,
            message="A metric value overflows float; refusing to report a metric.",
            details={},
        )
    return floated_actuals, floated_predictions, None


def mean_absolute_error(
    actuals: Sequence[float], predictions: Sequence[float]
) -> Tuple[Optional[float], Optional[StructuredError]]:
    """MAE = mean(|actual - predicted|) over valid paired observations."""
    actual_values, predicted_values, error = _validate_metric_pairs(actuals, predictions)
    if error is not None:
        return None, error

    try:
        differences = [
            abs(actual - predicted)
            for actual, predicted in zip(actual_values, predicted_values)
        ]
    except OverflowError:
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="Absolute error overflows float; refusing to report a metric.",
            details={"sample_count": len(actual_values)},
        )
    if any(not math.isfinite(difference) for difference in differences):
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="Absolute error is not finite; refusing to report a metric.",
            details={"sample_count": len(differences)},
        )
    value = math.fsum(differences) / len(differences)
    if not math.isfinite(value):
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="MAE is not finite; refusing to report a metric.",
            details={"sample_count": len(differences)},
        )
    return value, None


def root_mean_squared_error(
    actuals: Sequence[float], predictions: Sequence[float]
) -> Tuple[Optional[float], Optional[StructuredError]]:
    """RMSE = sqrt(mean((actual - predicted)^2)) over valid paired observations."""
    actual_values, predicted_values, error = _validate_metric_pairs(actuals, predictions)
    if error is not None:
        return None, error

    try:
        squared_errors = [
            (actual - predicted) ** 2
            for actual, predicted in zip(actual_values, predicted_values)
        ]
    except OverflowError:
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="Squared error overflows float; refusing to report a metric.",
            details={"sample_count": len(actual_values)},
        )
    if any(not math.isfinite(value) for value in squared_errors):
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="Squared error is not finite; refusing to report a metric.",
            details={"sample_count": len(squared_errors)},
        )
    value = math.sqrt(math.fsum(squared_errors) / len(squared_errors))
    if not math.isfinite(value):
        return None, StructuredError(
            code=ERROR_NON_FINITE_METRIC,
            message="RMSE is not finite; refusing to report a metric.",
            details={"sample_count": len(squared_errors)},
        )
    return value, None


MetricFunction = Callable[
    [Sequence[float], Sequence[float]],
    Tuple[Optional[float], Optional[StructuredError]],
]

SUPPORTED_METRICS: Dict[str, MetricFunction] = {
    METRIC_MAE: mean_absolute_error,
    METRIC_RMSE: root_mean_squared_error,
}


def compute_metrics(
    actuals: Sequence[float],
    predictions: Sequence[float],
    metric_names: Sequence[str],
) -> Tuple[Dict[str, float], Optional[StructuredError]]:
    """Compute the requested metrics; any failure produces no partial scores."""
    if (
        not isinstance(metric_names, Sequence)
        or isinstance(metric_names, (str, bytes))
        or not metric_names
    ):
        return {}, StructuredError(
            code=ERROR_INVALID_EVALUATION_REQUEST,
            message="metric_names must be a non-empty list of metric identifiers.",
            details={"received_type": type(metric_names).__name__},
        )

    unknown = [name for name in metric_names if name not in SUPPORTED_METRICS]
    if unknown:
        return {}, StructuredError(
            code=ERROR_UNSUPPORTED_METRIC,
            message=f"Unsupported metric(s): {unknown}.",
            details={"unsupported": unknown, "supported": sorted(SUPPORTED_METRICS)},
        )

    results: Dict[str, float] = {}
    for name in metric_names:
        value, error = SUPPORTED_METRICS[name](actuals, predictions)
        if error is not None:
            return {}, StructuredError(
                code=ERROR_METRIC_COMPUTATION_FAILURE,
                message=f"Metric '{name}' could not be computed: {error.message}",
                details={"metric": name, "cause": error.to_dict()},
            )
        results[name] = value
    return results, None


@dataclass(frozen=True)
class SampleSelection:
    """Validated numeric samples plus exclusion accounting for one partition."""
    samples: Tuple[ObservationSample, ...]
    requested_count: int
    exclusion_codes: Mapping[str, int]

    @property
    def accepted_count(self) -> int:
        return len(self.samples)

    @property
    def excluded_count(self) -> int:
        return self.requested_count - len(self.samples)


def select_samples(
    observation_ids: Sequence[str],
    observations: Mapping[str, Observation],
    input_variable: str,
    output_variable: str,
) -> SampleSelection:
    """Select numeric samples for one partition.

    Only successful observations with finite, non-boolean numeric input and
    output values are accepted. Everything else is excluded and counted with a
    reason code; exclusions are never silently dropped or treated as zeros.
    """
    accepted: List[ObservationSample] = []
    codes: Counter = Counter()

    for observation_id in observation_ids:
        observation = observations.get(observation_id) if isinstance(observations, Mapping) else None
        if observation is None:
            codes["missing_observation"] += 1
            continue
        if not isinstance(observation, Observation):
            codes["invalid_observation_object"] += 1
            continue
        if observation.status != ObservationStatus.SUCCESS:
            codes[f"observation_{observation.status.value}"] += 1
            continue

        input_values = observation.input_values
        if not isinstance(input_values, Mapping):
            codes["invalid_input_payload"] += 1
            continue
        if input_variable not in input_values:
            codes["missing_input_value"] += 1
            continue
        raw_input = input_values[input_variable]
        if isinstance(raw_input, bool) or not isinstance(raw_input, (int, float)):
            codes["invalid_input_type"] += 1
            continue
        try:
            numeric_input = float(raw_input)
        except OverflowError:
            codes["input_overflow"] += 1
            continue
        if not math.isfinite(numeric_input):
            codes["non_finite_input"] += 1
            continue

        output_values = observation.output_values
        if not isinstance(output_values, Mapping):
            codes["invalid_output_payload"] += 1
            continue
        if output_variable not in output_values:
            codes["missing_output_value"] += 1
            continue
        raw_output = output_values[output_variable]
        if isinstance(raw_output, bool) or not isinstance(raw_output, (int, float)):
            codes["invalid_output_type"] += 1
            continue
        try:
            numeric_output = float(raw_output)
        except OverflowError:
            codes["output_overflow"] += 1
            continue
        if not math.isfinite(numeric_output):
            codes["non_finite_output"] += 1
            continue

        accepted.append(
            ObservationSample(
                observation_id=observation_id,
                input_value=numeric_input,
                output_value=numeric_output,
            )
        )

    return SampleSelection(
        samples=tuple(accepted),
        requested_count=len(observation_ids),
        exclusion_codes=MappingProxyType(dict(codes)),
    )


class EvaluationPipeline:
    """Model-neutral fitting/evaluation orchestration for candidate hypotheses."""

    def __init__(
        self,
        registry: Optional[ModelFamilyRegistry] = None,
        fitters: Optional[Mapping[str, FamilyFitter]] = None,
        fitting_config: Optional[FittingConfig] = None,
    ):
        self._registry = registry if registry is not None else default_registry()
        self._fitters: Dict[str, FamilyFitter] = (
            default_fitters() if fitters is None else dict(fitters)
        )
        self._fitting_config = fitting_config if fitting_config is not None else FittingConfig()
        config_error = self._fitting_config.validate()
        if config_error is not None:
            raise ValueError(
                f"Invalid fitting configuration: [{config_error.code}] {config_error.message}"
            )

    @property
    def fitting_config(self) -> FittingConfig:
        return self._fitting_config

    def evaluate(
        self,
        request: EvaluationRequest,
        observations: Mapping[str, Observation],
        dataset_id: Optional[str] = None,
    ) -> EvaluationResult:
        """Fit and evaluate one candidate. Expected failures return FAILED results."""
        if not isinstance(request, EvaluationRequest):
            return self._failure(
                None,
                StructuredError(
                    code=ERROR_INVALID_EVALUATION_REQUEST,
                    message=(
                        "evaluate requires an EvaluationRequest, got "
                        f"{type(request).__name__}."
                    ),
                    details={"received_type": type(request).__name__},
                ),
                request=None,
            )
        candidate = request.hypothesis
        if not isinstance(candidate, HypothesisRepresentation):
            return self._failure(
                candidate if isinstance(candidate, HypothesisRepresentation) else None,
                StructuredError(
                    code=ERROR_INVALID_EVALUATION_REQUEST,
                    message="EvaluationRequest.hypothesis must be a HypothesisRepresentation.",
                    details={"received_type": type(candidate).__name__},
                ),
                request=request,
            )
        if not isinstance(observations, Mapping):
            return self._failure(
                candidate,
                StructuredError(
                    code=ERROR_INVALID_EVALUATION_REQUEST,
                    message="observations must be a mapping of observation_id to Observation.",
                    details={"received_type": type(observations).__name__},
                ),
                request=request,
            )

        training_ids = list(request.observation_ids)
        held_out_ids = list(request.held_out_observation_ids or [])

        duplicate_error = self._validate_observation_ids(training_ids, held_out_ids)
        if duplicate_error is not None:
            return self._failure(candidate, duplicate_error, request=request)

        metric_error = self._validate_metric_names(request.metric_names)
        if metric_error is not None:
            return self._failure(candidate, metric_error, request=request)

        family = self._registry.get(candidate.model_family)
        if family is None:
            return self._failure(
                candidate,
                StructuredError(
                    code=ERROR_UNKNOWN_MODEL_FAMILY,
                    message=f"Model family {candidate.model_family!r} is not registered.",
                    details={"available_families": list(self._registry.family_ids())},
                ),
                request=request,
            )

        _, specification_error = family.parse_specification(candidate.parameters)
        if specification_error is not None:
            return self._failure(candidate, specification_error, request=request)

        variables, variable_error = family.numeric_task_variables(candidate.parameters)
        if variable_error is not None:
            return self._failure(candidate, variable_error, request=request)
        input_variable, output_variable = variables

        training = select_samples(training_ids, observations, input_variable, output_variable)
        held_out = select_samples(held_out_ids, observations, input_variable, output_variable)
        counts = {
            "requested_training": training.requested_count,
            "accepted_training": training.accepted_count,
            "excluded_training": training.excluded_count,
            "requested_held_out": held_out.requested_count,
            "accepted_held_out": held_out.accepted_count,
            "excluded_held_out": held_out.excluded_count,
        }
        exclusion_codes = self._merge_exclusions(training, held_out)
        dataset_ids = self._dataset_identifiers(dataset_id, training_ids, held_out_ids)

        # Fit with training samples only. The fitter never sees held-out data.
        try:
            fitting = fit_candidate(
                candidate,
                training.samples,
                self._fitting_config,
                self._registry,
                self._fitters,
            )
        except ArithmeticError as exc:
            fitting = FittingResult(
                status=FittingStatus.FAILED,
                family_id=candidate.model_family,
                error=StructuredError(
                    code=ERROR_FITTING_NUMERICAL_FAILURE,
                    message=f"Numerical failure while fitting: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
                method=self._fitting_config.method,
                metadata={"config": self._fitting_config.to_dict()},
            )

        if fitting.status != FittingStatus.SUCCESS:
            return self._failure(
                candidate,
                fitting.error,
                request=request,
                fitting_status=FittingStatus.FAILED,
                fitting_method=fitting.method or self._fitting_config.method,
                counts=counts,
                exclusions=exclusion_codes,
                dataset_ids=dataset_ids,
                warnings=fitting.warnings,
            )

        fitted_specification = fitting.specification
        actuals, predictions, prediction_error = self._predict_samples(
            family,
            fitted_specification,
            training.samples,
            input_variable,
            output_variable,
            partition="training",
        )
        if prediction_error is not None:
            return self._failure(
                candidate,
                prediction_error,
                request=request,
                counts=counts,
                exclusions=exclusion_codes,
                dataset_ids=dataset_ids,
                warnings=fitting.warnings,
            )

        metrics, metric_failure = compute_metrics(actuals, predictions, request.metric_names)
        if metric_failure is not None:
            return self._failure(
                candidate,
                metric_failure,
                request=request,
                counts=counts,
                exclusions=exclusion_codes,
                dataset_ids=dataset_ids,
                warnings=fitting.warnings,
            )

        warnings = list(fitting.warnings)
        held_out_metrics: Optional[Dict[str, float]] = None
        if held_out.samples:
            held_actuals, held_predictions, held_error = self._predict_samples(
                family,
                fitted_specification,
                held_out.samples,
                input_variable,
                output_variable,
                partition="held_out",
            )
            if held_error is not None:
                return self._failure(
                    candidate,
                    held_error,
                    request=request,
                    counts=counts,
                    exclusions=exclusion_codes,
                    dataset_ids=dataset_ids,
                    warnings=fitting.warnings,
                )
            held_out_metrics, held_metric_failure = compute_metrics(
                held_actuals, held_predictions, request.metric_names
            )
            if held_metric_failure is not None:
                return self._failure(
                    candidate,
                    held_metric_failure,
                    request=request,
                    counts=counts,
                    exclusions=exclusion_codes,
                    dataset_ids=dataset_ids,
                    warnings=fitting.warnings,
                )
        else:
            warnings.append(
                StructuredError(
                    code=WARNING_HELD_OUT_UNAVAILABLE,
                    message=(
                        "No valid held-out observations were available, so held-out "
                        "metrics are unavailable (not zero)."
                    ),
                    details={
                        "requested_held_out": held_out.requested_count,
                        "excluded_held_out": held_out.excluded_count,
                    },
                )
            )

        fitted_model = fitting.model
        return EvaluationResult(
            hypothesis_id=candidate.hypothesis_id,
            status=EvaluationStatus.COMPLETED,
            metrics=metrics,
            sample_count=training.accepted_count,
            held_out_metrics=held_out_metrics,
            model_family=candidate.model_family,
            fitting_status=FittingStatus.SUCCESS,
            fitting_method=fitting.method,
            fitting_config=self._fitting_config.to_dict(),
            fitted_specification=fitted_specification,
            complexity=fitted_model.complexity() if fitted_model is not None else candidate.complexity,
            held_out_sample_count=held_out.accepted_count,
            observation_counts=counts,
            exclusion_codes=exclusion_codes,
            metric_definitions={
                name: METRIC_DEFINITIONS[name] for name in request.metric_names
            },
            metric_version=METRIC_VERSION,
            dataset_ids=dataset_ids,
            request_id=request.request_id,
            warnings=tuple(warnings) or None,
        )

    def evaluate_many(
        self,
        requests: Sequence[EvaluationRequest],
        observations: Mapping[str, Observation],
        dataset_id: Optional[str] = None,
    ) -> List[EvaluationResult]:
        """Evaluate candidates independently; one failure never stops the rest."""
        results: List[EvaluationResult] = []
        for request in requests:
            try:
                results.append(self.evaluate(request, observations, dataset_id))
            except Exception as exc:  # noqa: BLE001 - unexpected failures are isolated per candidate
                results.append(self._unexpected_failure(request, exc))
        return results

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _validate_observation_ids(
        self, training_ids: Sequence[str], held_out_ids: Sequence[str]
    ) -> Optional[StructuredError]:
        for label, ids in (("observation_ids", training_ids), ("held_out_observation_ids", held_out_ids)):
            if len(set(ids)) != len(ids):
                duplicated = sorted({item for item in ids if ids.count(item) > 1})
                return StructuredError(
                    code=ERROR_INVALID_EVALUATION_REQUEST,
                    message=f"{label} contains duplicate observation IDs.",
                    details={"argument": label, "duplicates": duplicated},
                )
        overlap = sorted(set(training_ids).intersection(held_out_ids))
        if overlap:
            return StructuredError(
                code=ERROR_INVALID_EVALUATION_REQUEST,
                message="Training and held-out observation IDs must be disjoint.",
                details={"overlap": overlap},
            )
        return None

    def _validate_metric_names(self, metric_names: Sequence[str]) -> Optional[StructuredError]:
        if (
            not isinstance(metric_names, Sequence)
            or isinstance(metric_names, (str, bytes))
            or not metric_names
        ):
            return StructuredError(
                code=ERROR_INVALID_EVALUATION_REQUEST,
                message="metric_names must be a non-empty list of metric identifiers.",
                details={"received_type": type(metric_names).__name__},
            )
        unknown = [name for name in metric_names if name not in SUPPORTED_METRICS]
        if unknown:
            return StructuredError(
                code=ERROR_UNSUPPORTED_METRIC,
                message=f"Unsupported metric(s): {unknown}.",
                details={"unsupported": unknown, "supported": sorted(SUPPORTED_METRICS)},
            )
        return None

    def _predict_samples(
        self,
        family,
        specification: Mapping[str, Any],
        samples: Sequence[ObservationSample],
        input_variable: str,
        output_variable: str,
        partition: str,
    ) -> Tuple[Optional[List[float]], Optional[List[float]], Optional[StructuredError]]:
        actuals: List[float] = []
        predictions: List[float] = []
        for sample in samples:
            prediction, error = family.predict(
                specification, {input_variable: sample.input_value}
            )
            if error is not None:
                return None, None, StructuredError(
                    code=ERROR_PREDICTION_FAILURE,
                    message=(
                        f"Prediction failed on {partition} observation "
                        f"{sample.observation_id!r}: {error.message}"
                    ),
                    details={
                        "partition": partition,
                        "observation_id": sample.observation_id,
                        "cause": error.to_dict(),
                    },
                )
            value = prediction.get(output_variable)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                return None, None, StructuredError(
                    code=ERROR_PREDICTION_FAILURE,
                    message=(
                        f"Prediction for {partition} observation "
                        f"{sample.observation_id!r} is not a finite number."
                    ),
                    details={"partition": partition, "observation_id": sample.observation_id},
                )
            actuals.append(sample.output_value)
            predictions.append(float(value))
        return actuals, predictions, None

    def _merge_exclusions(
        self, training: SampleSelection, held_out: SampleSelection
    ) -> Dict[str, int]:
        merged: Counter = Counter()
        merged.update(training.exclusion_codes)
        merged.update(held_out.exclusion_codes)
        return dict(merged)

    def _dataset_identifiers(
        self,
        dataset_id: Optional[str],
        training_ids: Sequence[str],
        held_out_ids: Sequence[str],
    ) -> Dict[str, Any]:
        identifiers: Dict[str, Any] = {
            "training_observation_ids": list(training_ids),
            "held_out_observation_ids": list(held_out_ids),
        }
        if dataset_id is not None:
            identifiers["dataset_id"] = dataset_id
        return identifiers

    def _failure(
        self,
        candidate: Optional[HypothesisRepresentation],
        error: Optional[StructuredError],
        request: Optional[EvaluationRequest],
        fitting_status: Optional[FittingStatus] = None,
        fitting_method: Optional[str] = None,
        counts: Optional[Mapping[str, int]] = None,
        exclusions: Optional[Mapping[str, int]] = None,
        dataset_ids: Optional[Mapping[str, Any]] = None,
        warnings: Sequence[StructuredError] = (),
    ) -> EvaluationResult:
        if error is None:
            raise ValueError("Evaluation failure requires a structured error.")
        return EvaluationResult(
            hypothesis_id=candidate.hypothesis_id if candidate is not None else "unknown",
            status=EvaluationStatus.FAILED,
            metrics={},
            sample_count=0,
            error=error,
            model_family=candidate.model_family if candidate is not None else None,
            fitting_status=fitting_status,
            fitting_method=fitting_method,
            fitting_config=self._fitting_config.to_dict(),
            observation_counts=counts,
            exclusion_codes=exclusions,
            dataset_ids=dataset_ids,
            request_id=request.request_id if request is not None else None,
            warnings=tuple(warnings) or None,
        )

    def _unexpected_failure(self, request: Any, exc: Exception) -> EvaluationResult:
        candidate = getattr(request, "hypothesis", None)
        if not isinstance(candidate, HypothesisRepresentation):
            candidate = None
        return self._failure(
            candidate,
            StructuredError(
                code=ERROR_UNEXPECTED_EVALUATION_ERROR,
                message=(
                    "Unexpected evaluation failure "
                    f"({type(exc).__name__}); this candidate was skipped and the run "
                    "continued."
                ),
                details={"exception_type": type(exc).__name__},
            ),
            request=request if isinstance(request, EvaluationRequest) else None,
        )
