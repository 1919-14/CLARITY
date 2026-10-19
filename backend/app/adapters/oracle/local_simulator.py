"""Deterministic local oracle simulator and fixtures for CLARITY testing.

Implements OracleInterface using standard mathematical functions while strictly
keeping secret coefficients inside private attributes inaccessible to standard inspection.
"""

from typing import Dict, Any, Optional
import math
from ...domain.contracts import (
    OracleInterface,
    ExperimentInput,
    Observation,
    ObservationStatus,
    StructuredError,
    generate_id,
    utc_now_iso,
)


class PolynomialLocalOracle(OracleInterface):
    """Deterministic local simulator evaluating a polynomial function.
    
    Default hidden rule: y = 0.5 * x^3 - 1.0 * x^2 - 2.0 * x + 3.0
    Domain: 1D real numbers x in [-10.0, 10.0]
    
    Notice: Ground-truth parameters are kept in private attributes prefixed with double underscore,
    and are never returned in observations or metadata.
    """

    def __init__(
        self,
        domain_name: str = "1d_polynomial",
        min_x: float = -10.0,
        max_x: float = 10.0,
        coefficients: Optional[Dict[str, float]] = None,
        noise_std: float = 0.0,
    ):
        self._domain = domain_name
        self._min_x = min_x
        self._max_x = max_x
        self._noise_std = noise_std
        # Hidden ground-truth rule (kept isolated)
        self.__coefficients = coefficients or {
            "w3": 0.5,
            "w2": -1.0,
            "w1": -2.0,
            "w0": 3.0,
        }
        self._query_count = 0

    @property
    def domain(self) -> str:
        return self._domain

    @property
    def query_count(self) -> int:
        """Observable query counter for budget tracking."""
        return self._query_count

    def validate_input(self, experiment_input: ExperimentInput) -> Optional[StructuredError]:
        """Validate that input values contain scalar 'x' within bounds."""
        if "x" not in experiment_input.values:
            return StructuredError(
                code="MISSING_PARAMETER",
                message="Experiment input requires numeric variable 'x'.",
                details={"provided_keys": list(experiment_input.values.keys())},
            )

        val = experiment_input.values["x"]
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            return StructuredError(
                code="INVALID_PARAMETER_TYPE",
                message=f"Parameter 'x' must be a real number, got {type(val).__name__}.",
                details={"x": str(val)},
            )

        if not (self._min_x <= float(val) <= self._max_x):
            return StructuredError(
                code="OUT_OF_BOUNDS",
                message=f"Value x={val} is outside valid domain range [{self._min_x}, {self._max_x}].",
                details={"x": val, "min_x": self._min_x, "max_x": self._max_x},
            )

        return None

    def query(self, experiment_input: ExperimentInput) -> Observation:
        """Query the oracle with a candidate experiment input."""
        # 1. Validation check
        val_error = self.validate_input(experiment_input)
        if val_error is not None:
            return Observation(
                input_id=experiment_input.input_id,
                input_values=experiment_input.values,
                output_values=None,
                status=ObservationStatus.INVALID_INPUT,
                error=val_error,
                sequence_number=self._query_count + 1,
            )

        self._query_count += 1
        x = float(experiment_input.values["x"])

        # Compute output from hidden rule
        w3 = self.__coefficients.get("w3", 0.0)
        w2 = self.__coefficients.get("w2", 0.0)
        w1 = self.__coefficients.get("w1", 0.0)
        w0 = self.__coefficients.get("w0", 0.0)

        y = (w3 * (x ** 3)) + (w2 * (x ** 2)) + (w1 * x) + w0

        # Optional deterministic noise metadata representation
        noise_info = None
        if self._noise_std > 0.0:
            noise_info = {"noise_std": self._noise_std, "distribution": "gaussian"}

        return Observation(
            observation_id=generate_id("obs"),
            input_id=experiment_input.input_id,
            input_values={"x": x},
            output_values={"y": round(y, 6)},
            status=ObservationStatus.SUCCESS,
            sequence_number=self._query_count,
            timestamp=utc_now_iso(),
            noise_metadata=noise_info,
        )

    # Isolated method for testing/evaluator purposes only - NOT part of OracleInterface
    def _evaluator_get_ground_truth(self) -> Dict[str, Any]:
        """Exclusively accessible by isolated benchmark harness tests."""
        return {
            "domain": self._domain,
            "coefficients": dict(self.__coefficients),
            "formula": "0.5*x^3 - 1.0*x^2 - 2.0*x + 3.0",
        }
