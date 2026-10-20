"""Bounded, machine-readable hypothesis model families for CLARITY.

Issue #5 scope: one practical numerical family (bounded polynomials, degrees 0
through a configured maximum) behind a small registry so further families can be
added without touching the discovery loop.

Design rules enforced here:
- No ``eval``/``exec``, no arbitrary expressions, no dynamic code execution.
- Model specifications are plain data validated into typed models.
- Prediction is deterministic for a deterministic specification.
- Every failure is reported as a StructuredError; nothing is silently coerced,
  clipped, or replaced with a different model.
"""

from abc import ABC, abstractmethod
import json
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .contracts import StructuredError

# Stable family identifier used in specifications and API payloads.
POLYNOMIAL_FAMILY_ID = "polynomial"

# Coefficient convention: coefficients are ordered by ascending power, so
# coefficients[i] is the weight of x**i and coefficients[0] is the constant term.
COEFFICIENT_ORDER_ASCENDING = "ascending"

# Default and hard ceiling for the polynomial degree. The hard ceiling bounds the
# number of free parameters (degree + 1 <= 13) so misconfiguration cannot create
# an uncontrolled search space.
DEFAULT_MAX_POLYNOMIAL_DEGREE = 3
HARD_MAX_POLYNOMIAL_DEGREE = 12

# Structured error codes. Reused by the candidate generator.
ERROR_UNKNOWN_MODEL_FAMILY = "UNKNOWN_MODEL_FAMILY"
ERROR_UNSUPPORTED_MODEL_FAMILY = "UNSUPPORTED_MODEL_FAMILY"
ERROR_INVALID_SPECIFICATION = "INVALID_MODEL_SPECIFICATION"
ERROR_MISSING_PARAMETER = "MISSING_PARAMETER"
ERROR_INVALID_PARAMETER_TYPE = "INVALID_PARAMETER_TYPE"
ERROR_UNSUPPORTED_DEGREE = "UNSUPPORTED_DEGREE"
ERROR_NON_FINITE_VALUE = "NON_FINITE_VALUE"
ERROR_NON_FINITE_PREDICTION = "NON_FINITE_PREDICTION"

_SPECIFICATION_KEYS = frozenset(
    {
        "family",
        "degree",
        "coefficient_order",
        "coefficients",
        "input_variable",
        "output_variable",
    }
)


def _is_real_number(value: Any) -> bool:
    """True for int/float values, explicitly excluding bool."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_int(value: Any) -> bool:
    """True for integer values, explicitly excluding bool."""
    return isinstance(value, int) and not isinstance(value, bool)


def _format_number(value: float) -> str:
    """Format a coefficient for the human-readable summary without rounding loss."""
    if value != value or value in (float("inf"), float("-inf")):
        return repr(value)
    if value == int(value) and abs(value) < 1e16:
        return f"{int(value)}.0"
    return repr(value)


class ValidatedModel(ABC):
    """Model-neutral contract for a validated specification.

    Families produce models implementing this interface, so the registry, the
    candidate generator, and downstream consumers can treat any family's model
    uniformly without importing a family-specific class.
    """

    @abstractmethod
    def to_specification(self) -> Dict[str, Any]:
        """Canonical, JSON-serializable specification for this model."""

    @abstractmethod
    def canonical_key(self) -> str:
        """Stable key for duplicate detection of canonical specifications."""

    @abstractmethod
    def summarize(self) -> str:
        """Human-readable summary of this model."""

    @abstractmethod
    def complexity(self) -> int:
        """Documented complexity measure for this model."""


@dataclass(frozen=True)
class PolynomialModel(ValidatedModel):
    """Validated polynomial model: y = sum(coefficients[i] * x**i).

    ``coefficients`` always has ``degree + 1`` entries in ascending-power order.
    """
    degree: int
    coefficients: Tuple[float, ...]
    input_variable: str = "x"
    output_variable: str = "y"

    def coefficient(self, index: int) -> float:
        """Return the coefficient of x**index."""
        return self.coefficients[index]

    def to_specification(self) -> Dict[str, Any]:
        """Canonical, JSON-serializable specification for this model."""
        return {
            "family": POLYNOMIAL_FAMILY_ID,
            "degree": self.degree,
            "coefficient_order": COEFFICIENT_ORDER_ASCENDING,
            "coefficients": {f"c{i}": float(c) for i, c in enumerate(self.coefficients)},
            "input_variable": self.input_variable,
            "output_variable": self.output_variable,
        }

    def canonical_key(self) -> str:
        """Stable key for duplicate detection of canonical specifications.

        Only canonical-specification equality is detected. Two mathematically
        equivalent polynomials expressed with different degrees are rejected at
        validation time instead of being treated as equivalent (see docs).
        """
        return json.dumps(self.to_specification(), sort_keys=True, separators=(",", ":"))

    def summarize(self) -> str:
        terms = []
        for index, coefficient in enumerate(self.coefficients):
            magnitude = _format_number(abs(coefficient))
            if index == 0:
                term = magnitude
            elif index == 1:
                term = f"{magnitude}*{self.input_variable}"
            else:
                term = f"{magnitude}*{self.input_variable}^{index}"
            if not terms:
                terms.append(f"-{term}" if coefficient < 0 else term)
            else:
                terms.append(f"- {term}" if coefficient < 0 else f"+ {term}")
        return f"{self.output_variable} = " + " ".join(terms)

    def complexity(self) -> int:
        """Complexity = number of free coefficients (degree + 1)."""
        return self.degree + 1


class HypothesisModelFamily(ABC):
    """Interface every registered model family implements.

    Families are model-neutral: they validate specifications into
    ValidatedModel instances and predict from specifications. Summaries and
    complexity live on the validated model, so a non-polynomial family never has
    to fit into polynomial-shaped method signatures.
    """

    family_id: str

    @property
    @abstractmethod
    def max_complexity(self) -> int:
        """Largest complexity score this family instance can produce."""

    @abstractmethod
    def parse_specification(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[ValidatedModel], Optional[StructuredError]]:
        """Validate a specification, returning a validated model or a structured error."""

    @abstractmethod
    def predict(
        self, specification: Mapping[str, Any], input_values: Mapping[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[StructuredError]]:
        """Predict outputs for input values using the specification."""

    def validate_specification(self, specification: Mapping[str, Any]) -> Optional[StructuredError]:
        """Validate a specification; return None when valid."""
        return self.parse_specification(specification)[1]

    def numeric_task_variables(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[Tuple[str, str]], Optional[StructuredError]]:
        """Declare the input/output variable names for scalar numeric prediction.

        Families that are not scalar numeric tasks return a structured error
        instead of guessing, so evaluation never assumes a task shape it cannot
        support. Model-neutral orchestration only needs these two names.
        """
        return None, StructuredError(
            code=ERROR_UNSUPPORTED_MODEL_FAMILY,
            message=(
                f"Family '{self.family_id}' does not declare numeric scalar task variables."
            ),
            details={"family": self.family_id},
        )


class PolynomialFamily(HypothesisModelFamily):
    """Bounded polynomial family covering constant, linear, and higher degrees.

    Degree 0 (constant), degree 1 (linear), and degrees 2..max_degree all share
    this implementation, so the family is one coherent abstraction.

    Rule: for degree >= 1 the leading coefficient must be non-zero, so the
    declared degree describes a genuine degree-d polynomial. A candidate that
    wants fewer terms must declare a lower degree. This keeps "degree" and
    "complexity" honest and makes canonical-specification equality a safe
    duplicate rule. Degree 0 may use any finite constant, including 0.0.
    """

    family_id = POLYNOMIAL_FAMILY_ID

    def __init__(
        self,
        max_degree: int = DEFAULT_MAX_POLYNOMIAL_DEGREE,
        input_variable: str = "x",
        output_variable: str = "y",
    ):
        if not _is_int(max_degree):
            raise ValueError("max_degree must be an integer.")
        if max_degree < 0 or max_degree > HARD_MAX_POLYNOMIAL_DEGREE:
            raise ValueError(
                f"max_degree must be between 0 and {HARD_MAX_POLYNOMIAL_DEGREE}, got {max_degree}."
            )
        if not input_variable or not isinstance(input_variable, str):
            raise ValueError("input_variable must be a non-empty string.")
        if not output_variable or not isinstance(output_variable, str):
            raise ValueError("output_variable must be a non-empty string.")
        self._max_degree = max_degree
        self._input_variable = input_variable
        self._output_variable = output_variable

    @property
    def max_degree(self) -> int:
        return self._max_degree

    @property
    def max_complexity(self) -> int:
        return self._max_degree + 1

    @property
    def input_variable(self) -> str:
        return self._input_variable

    @property
    def output_variable(self) -> str:
        return self._output_variable

    def numeric_task_variables(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[Tuple[str, str]], Optional[StructuredError]]:
        model, error = self.parse_specification(specification)
        if error is not None:
            return None, error
        return (model.input_variable, model.output_variable), None

    def parse_specification(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[PolynomialModel], Optional[StructuredError]]:
        if not isinstance(specification, Mapping):
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message="Polynomial specification must be a mapping.",
                details={"received_type": type(specification).__name__},
            )

        unknown_keys = sorted(set(specification.keys()) - _SPECIFICATION_KEYS)
        if unknown_keys:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=f"Unsupported specification keys: {unknown_keys}.",
                details={"unknown_keys": unknown_keys},
            )

        family = specification.get("family", self.family_id)
        if family != self.family_id:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=f"Specification family must be '{self.family_id}', got {family!r}.",
                details={"family": family},
            )

        if "degree" not in specification:
            return None, StructuredError(
                code=ERROR_MISSING_PARAMETER,
                message="Polynomial specification requires 'degree'.",
                details={"missing": ["degree"]},
            )
        degree = specification["degree"]
        if not _is_int(degree):
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=f"'degree' must be an integer, got {type(degree).__name__}.",
                details={"degree": str(degree)},
            )
        if degree < 0 or degree > self._max_degree:
            return None, StructuredError(
                code=ERROR_UNSUPPORTED_DEGREE,
                message=f"Degree {degree} is outside the supported range 0..{self._max_degree}.",
                details={"degree": degree, "max_degree": self._max_degree},
            )

        coefficient_order = specification.get("coefficient_order", COEFFICIENT_ORDER_ASCENDING)
        if coefficient_order != COEFFICIENT_ORDER_ASCENDING:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=(
                    f"'coefficient_order' must be '{COEFFICIENT_ORDER_ASCENDING}', "
                    f"got {coefficient_order!r}."
                ),
                details={"coefficient_order": coefficient_order},
            )

        input_variable = specification.get("input_variable", self._input_variable)
        output_variable = specification.get("output_variable", self._output_variable)
        if input_variable != self._input_variable:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=(
                    f"Family '{self.family_id}' is configured for input variable "
                    f"'{self._input_variable}', got {input_variable!r}."
                ),
                details={"input_variable": input_variable},
            )
        if output_variable != self._output_variable:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=(
                    f"Family '{self.family_id}' is configured for output variable "
                    f"'{self._output_variable}', got {output_variable!r}."
                ),
                details={"output_variable": output_variable},
            )

        coefficients, error = self._parse_coefficients(specification, degree)
        if error is not None:
            return None, error

        if degree >= 1 and coefficients[degree] == 0.0:
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message=(
                    f"Leading coefficient c{degree} must be non-zero for degree {degree}; "
                    "use a lower degree for a model with fewer terms."
                ),
                details={"degree": degree},
            )

        return (
            PolynomialModel(
                degree=degree,
                coefficients=coefficients,
                input_variable=self._input_variable,
                output_variable=self._output_variable,
            ),
            None,
        )

    def _parse_coefficients(
        self, specification: Mapping[str, Any], degree: int
    ) -> Tuple[Optional[Tuple[float, ...]], Optional[StructuredError]]:
        if "coefficients" not in specification:
            return None, StructuredError(
                code=ERROR_MISSING_PARAMETER,
                message="Polynomial specification requires 'coefficients'.",
                details={"missing": ["coefficients"]},
            )

        raw = specification["coefficients"]
        ordered: list[Optional[float]] = [None] * (degree + 1)

        if isinstance(raw, Mapping):
            for key, value in raw.items():
                if not isinstance(key, str) or not key.startswith("c") or not key[1:].isdigit():
                    return None, StructuredError(
                        code=ERROR_INVALID_SPECIFICATION,
                        message=f"Coefficient key {key!r} must use the form 'c<index>'.",
                        details={"coefficient_key": str(key)},
                    )
                index = int(key[1:])
                if index > degree:
                    return None, StructuredError(
                        code=ERROR_INVALID_SPECIFICATION,
                        message=(
                            f"Coefficient {key!r} exceeds the declared degree {degree}; "
                            "degree and coefficients disagree."
                        ),
                        details={"coefficient_key": key, "degree": degree},
                    )
                if ordered[index] is not None:
                    return None, StructuredError(
                        code=ERROR_INVALID_SPECIFICATION,
                        message=f"Duplicate coefficient index c{index}.",
                        details={"coefficient_key": key},
                    )
                ordered[index] = value
        elif isinstance(raw, (list, tuple)):
            if len(raw) != degree + 1:
                return None, StructuredError(
                    code=ERROR_INVALID_SPECIFICATION,
                    message=(
                        f"'coefficients' list must contain {degree + 1} entries for degree "
                        f"{degree}, got {len(raw)}."
                    ),
                    details={"expected": degree + 1, "received": len(raw)},
                )
            ordered = list(raw)
        else:
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=(
                    "'coefficients' must be a mapping of c<index> keys or a list, "
                    f"got {type(raw).__name__}."
                ),
                details={"coefficients_type": type(raw).__name__},
            )

        missing = [f"c{i}" for i, value in enumerate(ordered) if value is None]
        if missing:
            return None, StructuredError(
                code=ERROR_MISSING_PARAMETER,
                message=f"Missing coefficients for degree {degree}: {missing}.",
                details={"missing": missing, "degree": degree},
            )

        coefficients = []
        for index, value in enumerate(ordered):
            if not _is_real_number(value):
                return None, StructuredError(
                    code=ERROR_INVALID_PARAMETER_TYPE,
                    message=(
                        f"Coefficient c{index} must be int or float, got {type(value).__name__}."
                    ),
                    details={"coefficient": f"c{index}", "received_type": type(value).__name__},
                )
            number = float(value)
            if not math.isfinite(number):
                return None, StructuredError(
                    code=ERROR_NON_FINITE_VALUE,
                    message=f"Coefficient c{index} must be finite, got {number!r}.",
                    details={"coefficient": f"c{index}"},
                )
            coefficients.append(number)

        return tuple(coefficients), None

    def predict(
        self, specification: Mapping[str, Any], input_values: Mapping[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[StructuredError]]:
        model, error = self.parse_specification(specification)
        if error is not None:
            return None, error

        if not isinstance(input_values, Mapping):
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=f"Prediction input must be a mapping, got {type(input_values).__name__}.",
                details={"received_type": type(input_values).__name__},
            )

        variable = model.input_variable
        if variable not in input_values:
            return None, StructuredError(
                code=ERROR_MISSING_PARAMETER,
                message=f"Prediction input requires numeric variable '{variable}'.",
                details={"provided_keys": list(input_values.keys())},
            )

        raw = input_values[variable]
        if not _is_real_number(raw):
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=(
                    f"Prediction input '{variable}' must be a real number, "
                    f"got {type(raw).__name__}."
                ),
                details={"variable": variable, "received_type": type(raw).__name__},
            )

        x = float(raw)
        if not math.isfinite(x):
            return None, StructuredError(
                code=ERROR_NON_FINITE_VALUE,
                message=f"Prediction input '{variable}' must be finite, got {x!r}.",
                details={"variable": variable},
            )

        try:
            y = 0.0
            for coefficient in reversed(model.coefficients):
                y = (y * x) + coefficient
        except (OverflowError, ValueError) as exc:
            return None, StructuredError(
                code=ERROR_NON_FINITE_PREDICTION,
                message=f"Prediction failed for {variable}={x!r}: {exc}",
                details={"variable": variable, "degree": model.degree},
            )

        if not math.isfinite(y):
            return None, StructuredError(
                code=ERROR_NON_FINITE_PREDICTION,
                message=(
                    f"Prediction for {variable}={x!r} is not finite; "
                    "refusing to return NaN or infinity."
                ),
                details={"variable": variable, "degree": model.degree},
            )

        return {model.output_variable: y}, None


class ModelFamilyRegistry:
    """Small registry mapping stable family identifiers to implementations."""

    def __init__(self, families: Iterable[HypothesisModelFamily] = ()):
        self._families: Dict[str, HypothesisModelFamily] = {}
        for family in families:
            self.register(family)

    def register(self, family: HypothesisModelFamily) -> None:
        family_id = getattr(family, "family_id", None)
        if not family_id or not isinstance(family_id, str):
            raise ValueError("Model family must expose a non-empty string family_id.")
        if family_id in self._families:
            raise ValueError(f"Model family '{family_id}' is already registered.")
        self._families[family_id] = family

    def get(self, family_id: str) -> Optional[HypothesisModelFamily]:
        return self._families.get(family_id)

    def family_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._families))

    def _resolve_family(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[HypothesisModelFamily], Optional[StructuredError]]:
        """Resolve the family for a specification, returning structured errors only.

        The family identifier is type-checked before any dict lookup, because an
        unhashable value (list/dict) would otherwise raise a raw TypeError.
        """
        if not isinstance(specification, Mapping):
            return None, StructuredError(
                code=ERROR_INVALID_SPECIFICATION,
                message="Model specification must be a mapping.",
                details={"received_type": type(specification).__name__},
            )
        family_id = specification.get("family")
        if family_id is None:
            return None, StructuredError(
                code=ERROR_MISSING_PARAMETER,
                message="Model specification requires a 'family' identifier.",
                details={"missing": ["family"]},
            )
        if not isinstance(family_id, str) or not family_id:
            return None, StructuredError(
                code=ERROR_INVALID_PARAMETER_TYPE,
                message=(
                    "Model family identifier must be a non-empty string, got "
                    f"{type(family_id).__name__}."
                ),
                details={"received_type": type(family_id).__name__},
            )
        family = self.get(family_id)
        if family is None:
            return None, StructuredError(
                code=ERROR_UNKNOWN_MODEL_FAMILY,
                message=f"Model family {family_id!r} is not registered.",
                details={"family": family_id, "available_families": list(self.family_ids())},
            )
        return family, None

    def parse_specification(
        self, specification: Mapping[str, Any]
    ) -> Tuple[Optional[ValidatedModel], Optional[StructuredError]]:
        family, error = self._resolve_family(specification)
        if error is not None:
            return None, error
        return family.parse_specification(specification)

    def validate_specification(self, specification: Mapping[str, Any]) -> Optional[StructuredError]:
        return self.parse_specification(specification)[1]

    def predict(
        self, specification: Mapping[str, Any], input_values: Mapping[str, Any]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[StructuredError]]:
        family, error = self._resolve_family(specification)
        if error is not None:
            return None, error
        return family.predict(specification, input_values)


def default_registry(max_degree: int = DEFAULT_MAX_POLYNOMIAL_DEGREE) -> ModelFamilyRegistry:
    """Registry with the supported default model families."""
    return ModelFamilyRegistry([PolynomialFamily(max_degree=max_degree)])
