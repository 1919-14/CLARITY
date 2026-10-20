"""Common fitting interface and family-specific fitters (Issue #6).

Fitting turns a candidate *specification* into fitted parameters using only the
training observations supplied to the fitter. The fitter never receives held-out
data, an oracle, or evaluator-only information.

Numerical policy for the polynomial family:
- Ordinary least squares over the training observations.
- Coefficients remain in the Issue #5 ascending convention (c0..c_degree).
- The Vandermonde system is built in a scaled input variable and converted back
  to the original input basis, which keeps conditioning reasonable for
  degrees up to the hard ceiling.
- Rank deficiency, insufficient observations, non-finite coefficients, and
  degenerate (zero leading coefficient) fits are explicit structured failures.
  Degrees are never silently reduced and coefficients are never invented.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import math
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .contracts import FittingStatus, HypothesisRepresentation, StructuredError
from .hypothesis_models import (
    COEFFICIENT_ORDER_ASCENDING,
    ERROR_INVALID_PARAMETER_TYPE,
    ERROR_INVALID_SPECIFICATION,
    ERROR_NON_FINITE_VALUE,
    ERROR_UNKNOWN_MODEL_FAMILY,
    ERROR_UNSUPPORTED_MODEL_FAMILY,
    POLYNOMIAL_FAMILY_ID,
    HypothesisModelFamily,
    ModelFamilyRegistry,
    ValidatedModel,
    default_registry,
)

FITTING_METHOD_LEAST_SQUARES = "least_squares"
SUPPORTED_FITTING_METHODS = (FITTING_METHOD_LEAST_SQUARES,)

ERROR_INVALID_FITTING_CONFIG = "INVALID_FITTING_CONFIG"
ERROR_UNSUPPORTED_FITTING_METHOD = "UNSUPPORTED_FITTING_METHOD"
ERROR_INSUFFICIENT_OBSERVATIONS = "INSUFFICIENT_OBSERVATIONS"
ERROR_RANK_DEFICIENT_FIT = "RANK_DEFICIENT_FIT"
ERROR_DEGENERATE_FIT = "DEGENERATE_FIT"
ERROR_NON_FINITE_COEFFICIENT = "NON_FINITE_COEFFICIENT"
ERROR_FIT_SPECIFICATION_INVALID = "FIT_SPECIFICATION_INVALID"
ERROR_FITTING_NUMERICAL_FAILURE = "FITTING_NUMERICAL_FAILURE"

WARNING_EXACTLY_DETERMINED_FIT = "EXACTLY_DETERMINED_FIT"


@dataclass(frozen=True)
class ObservationSample:
    """One validated numeric observation supplied to a fitter or metric.

    Evaluation builds these only from successful, finite, non-boolean
    observations, and each keeps its observation ID for traceability.
    """
    observation_id: str
    input_value: float
    output_value: float


@dataclass(frozen=True)
class FittingConfig:
    """All configuration that can affect a fit.

    Fields:
      method: Fitting method identifier. Only ``least_squares`` is implemented.
      rank_tolerance: Relative threshold on the QR diagonal below which the
        design matrix is considered rank deficient. Must be finite and > 0.
    """
    method: str = FITTING_METHOD_LEAST_SQUARES
    rank_tolerance: float = 1e-10

    def to_dict(self) -> Dict[str, Any]:
        return {"method": self.method, "rank_tolerance": self.rank_tolerance}

    def validate(self) -> Optional[StructuredError]:
        if not isinstance(self.method, str) or not self.method:
            return StructuredError(
                code=ERROR_INVALID_FITTING_CONFIG,
                message="Fitting method must be a non-empty string.",
                details={"method": str(self.method)},
            )
        if self.method not in SUPPORTED_FITTING_METHODS:
            return StructuredError(
                code=ERROR_UNSUPPORTED_FITTING_METHOD,
                message=(
                    f"Fitting method {self.method!r} is not supported; "
                    f"supported methods: {list(SUPPORTED_FITTING_METHODS)}."
                ),
                details={"method": self.method, "supported_methods": list(SUPPORTED_FITTING_METHODS)},
            )
        if (
            isinstance(self.rank_tolerance, bool)
            or not isinstance(self.rank_tolerance, (int, float))
            or not math.isfinite(float(self.rank_tolerance))
            or float(self.rank_tolerance) <= 0.0
        ):
            return StructuredError(
                code=ERROR_INVALID_FITTING_CONFIG,
                message="rank_tolerance must be a finite number greater than 0.",
                details={"rank_tolerance": str(self.rank_tolerance)},
            )
        return None


@dataclass(frozen=True)
class FittingResult:
    """Outcome of fitting one candidate specification.

    Fields:
      status: SUCCESS or FAILED.
      family_id: Model family that produced the fit.
      specification: Fitted canonical specification on success.
      model: Validated fitted model on success.
      sample_count: Training observations used.
      method: Fitting method actually used.
      metadata: Fit metadata (configuration, exactly-determined flag, etc.).
      error: Structured failure explanation for FAILED results.
      warnings: Non-fatal structured warnings.
    """
    status: FittingStatus
    family_id: str
    specification: Optional[Mapping[str, Any]] = None
    model: Optional[ValidatedModel] = None
    sample_count: int = 0
    method: Optional[str] = None
    metadata: Optional[Mapping[str, Any]] = None
    error: Optional[StructuredError] = None
    warnings: Tuple[StructuredError, ...] = ()

    def __post_init__(self):
        if self.status == FittingStatus.FAILED:
            if self.error is None:
                raise ValueError("Failed fitting result requires a structured error.")
            if self.specification is not None or self.model is not None:
                raise ValueError("Failed fitting result must not include a fitted model.")
        elif self.status == FittingStatus.SUCCESS:
            if self.error is not None:
                raise ValueError("Successful fitting result must not include an error.")
            if self.specification is None or self.model is None:
                raise ValueError("Successful fitting result requires a fitted specification.")


class FamilyFitter(ABC):
    """Model-family-specific fitting strategy behind a common interface."""

    family_id: str

    @abstractmethod
    def fit(
        self,
        specification: Mapping[str, Any],
        training_samples: Sequence[ObservationSample],
        config: FittingConfig,
        family: HypothesisModelFamily,
    ) -> FittingResult:
        """Fit the specification using only the supplied training samples."""


class _FittingFailure(Exception):
    """Internal structured failure used to unwind a fitter without side effects."""

    def __init__(self, error: StructuredError):
        super().__init__(error.message)
        self.error = error


def _failed(
    family_id: str,
    error: StructuredError,
    config: Optional[FittingConfig] = None,
    sample_count: int = 0,
) -> FittingResult:
    return FittingResult(
        status=FittingStatus.FAILED,
        family_id=family_id,
        sample_count=sample_count,
        method=config.method if config is not None else None,
        metadata={"config": config.to_dict()} if config is not None else None,
        error=error,
    )


def _require_finite_sample(sample: ObservationSample) -> None:
    if not isinstance(sample, ObservationSample):
        raise _FittingFailure(
            StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=(
                    "Training samples must be ObservationSample instances, got "
                    f"{type(sample).__name__}."
                ),
                details={"received_type": type(sample).__name__},
            )
        )
    for field_name, value in (
        ("input_value", sample.input_value),
        ("output_value", sample.output_value),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _FittingFailure(
                StructuredError(
                    code=ERROR_INVALID_PARAMETER_TYPE,
                    message=(
                        f"Training sample {sample.observation_id!r} {field_name} must be a "
                        f"real number, got {type(value).__name__}."
                    ),
                    details={"observation_id": sample.observation_id, "field": field_name},
                )
            )
        if not math.isfinite(float(value)):
            raise _FittingFailure(
                StructuredError(
                    code=ERROR_NON_FINITE_VALUE,
                    message=(
                        f"Training sample {sample.observation_id!r} {field_name} must be "
                        "finite."
                    ),
                    details={"observation_id": sample.observation_id, "field": field_name},
                )
            )


def _minimum_polynomial_observations(degree: int) -> int:
    """At least as many observations as free coefficients are required.

    Exactly determined systems (n == degree + 1) are allowed and reported via a
    warning; underdetermined systems are structured failures.
    """
    return degree + 1


def _householder_least_squares(
    matrix: Sequence[Sequence[float]],
    targets: Sequence[float],
    rank_tolerance: float,
) -> list:
    """Solve min ||A x - b|| via Householder QR without external dependencies."""
    row_count = len(matrix)
    column_count = len(matrix[0])
    r = [list(row) for row in matrix]
    y = list(targets)

    for k in range(column_count):
        column_norm = math.hypot(*(r[i][k] for i in range(k, row_count)))
        if column_norm == 0.0:
            continue
        alpha = -math.copysign(column_norm, r[k][k]) if r[k][k] != 0.0 else -column_norm
        v = [0.0] * row_count
        v[k] = r[k][k] - alpha
        for i in range(k + 1, row_count):
            v[i] = r[i][k]
        norm_squared = math.fsum(v[i] * v[i] for i in range(k, row_count))
        if norm_squared == 0.0:
            continue

        for j in range(k, column_count):
            dot = math.fsum(v[i] * r[i][j] for i in range(k, row_count))
            factor = 2.0 * dot / norm_squared
            for i in range(k, row_count):
                r[i][j] -= factor * v[i]
        dot = math.fsum(v[i] * y[i] for i in range(k, row_count))
        factor = 2.0 * dot / norm_squared
        for i in range(k, row_count):
            y[i] -= factor * v[i]

        r[k][k] = alpha
        for i in range(k + 1, row_count):
            r[i][k] = 0.0

    diagonal = [abs(r[i][i]) for i in range(column_count)]
    largest = max(diagonal)
    deficient = [i for i, value in enumerate(diagonal) if largest == 0.0 or value <= rank_tolerance * largest]
    if deficient:
        raise _FittingFailure(
            StructuredError(
                code=ERROR_RANK_DEFICIENT_FIT,
                message=(
                    "Training observations do not determine all polynomial coefficients; "
                    f"rank-deficient at coefficient index {deficient[0]}."
                ),
                details={
                    "rank_deficient_indices": deficient,
                    "rank_tolerance": rank_tolerance,
                    "largest_pivot": largest,
                },
            )
        )

    solution = [0.0] * column_count
    for i in range(column_count - 1, -1, -1):
        residual = y[i] - math.fsum(r[i][j] * solution[j] for j in range(i + 1, column_count))
        solution[i] = residual / r[i][i]
    return solution


def _scaled_basis_coefficients(
    scaled_coefficients: Sequence[float], center: float, scale: float
) -> list:
    """Convert coefficients of t=(x-center)/scale back to the x basis."""
    degree = len(scaled_coefficients) - 1
    coefficients = [0.0] * (degree + 1)
    for k, value in enumerate(scaled_coefficients):
        factor = value / (scale ** k)
        for i in range(k + 1):
            coefficients[i] += factor * math.comb(k, i) * ((-center) ** (k - i))
    return coefficients


class PolynomialLeastSquaresFitter(FamilyFitter):
    """Ordinary least squares for the bounded polynomial family."""

    family_id = POLYNOMIAL_FAMILY_ID

    def fit(
        self,
        specification: Mapping[str, Any],
        training_samples: Sequence[ObservationSample],
        config: FittingConfig,
        family: HypothesisModelFamily,
    ) -> FittingResult:
        config_error = config.validate()
        if config_error is not None:
            return _failed(family.family_id, config_error, config)

        if not isinstance(training_samples, (list, tuple)):
            return _failed(
                family.family_id,
                StructuredError(
                    code=ERROR_INVALID_PARAMETER_TYPE,
                    message="training_samples must be a list or tuple of ObservationSample.",
                    details={"received_type": type(training_samples).__name__},
                ),
                config,
            )

        model, spec_error = family.parse_specification(specification)
        if spec_error is not None:
            return _failed(family.family_id, spec_error, config)

        required = _minimum_polynomial_observations(model.degree)
        if len(training_samples) < required:
            return _failed(
                family.family_id,
                StructuredError(
                    code=ERROR_INSUFFICIENT_OBSERVATIONS,
                    message=(
                        f"Polynomial degree {model.degree} requires at least {required} "
                        f"training observations, got {len(training_samples)}."
                    ),
                    details={
                        "required": required,
                        "received": len(training_samples),
                        "degree": model.degree,
                    },
                ),
                config,
                sample_count=len(training_samples),
            )

        try:
            for sample in training_samples:
                _require_finite_sample(sample)

            inputs = [float(sample.input_value) for sample in training_samples]
            outputs = [float(sample.output_value) for sample in training_samples]

            center = 0.0
            scale = 1.0
            if model.degree >= 1:
                low, high = min(inputs), max(inputs)
                # Halve before subtracting to avoid overflow on extreme finite ranges.
                center = (low / 2.0) + (high / 2.0)
                scale = (high / 2.0) - (low / 2.0)
                if not math.isfinite(center) or not math.isfinite(scale):
                    raise _FittingFailure(
                        StructuredError(
                            code=ERROR_NON_FINITE_VALUE,
                            message="Training input range is too large to scale safely.",
                            details={"input_min": low, "input_max": high},
                        )
                    )
                if scale == 0.0:
                    raise _FittingFailure(
                        StructuredError(
                            code=ERROR_RANK_DEFICIENT_FIT,
                            message=(
                                "All training inputs are identical, so a degree "
                                f"{model.degree} polynomial is not identifiable."
                            ),
                            details={"degree": model.degree, "input_value": low},
                        )
                    )

            scaled_inputs = []
            for value in inputs:
                scaled = (value - center) / scale if model.degree >= 1 else 0.0
                if not math.isfinite(scaled):
                    raise _FittingFailure(
                        StructuredError(
                            code=ERROR_NON_FINITE_VALUE,
                            message="Scaled training input is not finite.",
                            details={"input_value": value},
                        )
                    )
                scaled_inputs.append(scaled)

            design = [
                [scaled ** power for power in range(model.degree + 1)] for scaled in scaled_inputs
            ]
            solution = _householder_least_squares(design, outputs, float(config.rank_tolerance))
            coefficients = _scaled_basis_coefficients(solution, center, scale)
        except _FittingFailure as failure:
            return _failed(family.family_id, failure.error, config, sample_count=len(training_samples))
        except (ArithmeticError, ValueError) as exc:
            return _failed(
                family.family_id,
                StructuredError(
                    code=ERROR_FITTING_NUMERICAL_FAILURE,
                    message=f"Numerical failure while fitting: {exc}",
                    details={"exception_type": type(exc).__name__},
                ),
                config,
                sample_count=len(training_samples),
            )

        for index, coefficient in enumerate(coefficients):
            if not math.isfinite(coefficient):
                return _failed(
                    family.family_id,
                    StructuredError(
                        code=ERROR_NON_FINITE_COEFFICIENT,
                        message=f"Fitted coefficient c{index} is not finite.",
                        details={"coefficient": f"c{index}"},
                    ),
                    config,
                    sample_count=len(training_samples),
                )

        fitted_specification = {
            "family": POLYNOMIAL_FAMILY_ID,
            "degree": model.degree,
            "coefficient_order": COEFFICIENT_ORDER_ASCENDING,
            "coefficients": {f"c{index}": float(value) for index, value in enumerate(coefficients)},
            "input_variable": model.input_variable,
            "output_variable": model.output_variable,
        }
        fitted_model, validation_error = family.parse_specification(fitted_specification)
        if validation_error is not None:
            if (
                validation_error.code == ERROR_INVALID_SPECIFICATION
                and "Leading coefficient" in validation_error.message
            ):
                validation_error = StructuredError(
                    code=ERROR_DEGENERATE_FIT,
                    message=(
                        "The least-squares solution has a zero leading coefficient, so it "
                        "describes a lower-degree model. The declared degree was not reduced; "
                        "propose a lower-degree candidate instead."
                    ),
                    details={"degree": model.degree},
                )
            else:
                validation_error = StructuredError(
                    code=ERROR_FIT_SPECIFICATION_INVALID,
                    message=(
                        "Fitted coefficients do not form a valid specification: "
                        f"{validation_error.message}"
                    ),
                    details=validation_error.details,
                )
            return _failed(
                family.family_id,
                validation_error,
                config,
                sample_count=len(training_samples),
            )

        exactly_determined = len(training_samples) == required
        warnings = ()
        if exactly_determined:
            warnings = (
                StructuredError(
                    code=WARNING_EXACTLY_DETERMINED_FIT,
                    message=(
                        "Number of training observations equals the number of free "
                        "coefficients; the fit interpolates the data and provides no "
                        "residual degrees of freedom."
                    ),
                    details={"observations": len(training_samples), "degree": model.degree},
                ),
            )

        return FittingResult(
            status=FittingStatus.SUCCESS,
            family_id=family.family_id,
            specification=fitted_specification,
            model=fitted_model,
            sample_count=len(training_samples),
            method=config.method,
            metadata={
                "config": config.to_dict(),
                "degree": model.degree,
                "exactly_determined": exactly_determined,
                "input_scaling": {"center": center, "scale": scale},
            },
            warnings=warnings,
        )


def default_fitters() -> Dict[str, FamilyFitter]:
    """Fitters registered for the supported model families."""
    fitter = PolynomialLeastSquaresFitter()
    return {fitter.family_id: fitter}


def fit_candidate(
    candidate: HypothesisRepresentation,
    training_samples: Sequence[ObservationSample],
    config: Optional[FittingConfig] = None,
    registry: Optional[ModelFamilyRegistry] = None,
    fitters: Optional[Mapping[str, FamilyFitter]] = None,
) -> FittingResult:
    """Fit a candidate using training samples only.

    The fitter receives only ``training_samples``. Held-out observations are not
    accepted by this function, so they cannot influence fitted parameters.

    Expected failures are returned as a FAILED FittingResult with a structured
    error. Unexpected exceptions from a fitter are not swallowed here; the
    evaluation pipeline isolates them per candidate.
    """
    if not isinstance(candidate, HypothesisRepresentation):
        return _failed(
            "unknown",
            StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=(
                    "fit_candidate requires a HypothesisRepresentation, got "
                    f"{type(candidate).__name__}."
                ),
                details={"received_type": type(candidate).__name__},
            ),
        )

    resolved_config = config if config is not None else FittingConfig()
    config_error = resolved_config.validate()
    if config_error is not None:
        return _failed(candidate.model_family, config_error, resolved_config)

    resolved_registry = registry if registry is not None else default_registry()
    family = resolved_registry.get(candidate.model_family)
    if family is None:
        return _failed(
            candidate.model_family,
            StructuredError(
                code=ERROR_UNKNOWN_MODEL_FAMILY,
                message=f"Model family {candidate.model_family!r} is not registered.",
                details={"available_families": list(resolved_registry.family_ids())},
            ),
            resolved_config,
        )

    available_fitters = fitters if fitters is not None else default_fitters()
    fitter = available_fitters.get(candidate.model_family)
    if fitter is None:
        return _failed(
            candidate.model_family,
            StructuredError(
                code=ERROR_UNSUPPORTED_MODEL_FAMILY,
                message=f"No fitter is registered for model family {candidate.model_family!r}.",
                details={"family": candidate.model_family},
            ),
            resolved_config,
        )

    return fitter.fit(candidate.parameters, training_samples, resolved_config, family)
