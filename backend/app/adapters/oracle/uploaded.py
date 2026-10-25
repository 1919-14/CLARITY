"""Oracle adapter for researcher-supplied black boxes.

Supported artifact kinds (see ``domain.oracle_contracts``):

* ``python_function`` — a ``.py`` file exposing ``f(x) -> y``.
* ``joblib_model``    — a ``.joblib``/``.pkl`` object exposing ``predict``,
  or a pickled callable.
* ``onnx_model``      — an ONNX graph with one float input accepting a single
  feature (shape ``[1, 1]`` or ``[1]``) and one output.

Execution model: artifacts are loaded **in-process**, by design. Uploaded code
is therefore trusted: a pickle load or module import can run arbitrary code with
the backend's privileges. Every failure is contained: an exception raised by the
artifact becomes an ``ORACLE_FAILURE`` observation carrying a sanitized message,
never an exception that escapes into the discovery loop.
"""

from __future__ import annotations

import importlib.util
import math
import sys
import uuid
from typing import Any, Callable, List, Optional, Sequence

from ...domain.contracts import (
    ExperimentInput,
    Observation,
    ObservationStatus,
    OracleInterface,
    StructuredError,
    generate_id,
    utc_now_iso,
)
from ...domain.oracle_contracts import (
    OracleContractError,
    OracleKind,
    OracleSpec,
    ProbeSample,
    probe_points,
)
from ...domain.sanitization import sanitize_exception_text

Predictor = Callable[[float], float]

_ERROR_MESSAGE_LIMIT = 240


def _sanitized_message(exc: BaseException) -> str:
    text = sanitize_exception_text(str(exc)) or exc.__class__.__name__
    text = " ".join(str(text).split())
    if len(text) > _ERROR_MESSAGE_LIMIT:
        text = text[: _ERROR_MESSAGE_LIMIT - 1].rstrip() + "…"
    return text or "The uploaded artifact failed without a message."


def _coerce_scalar(value: Any) -> float:
    """Return a finite float from a scalar-like prediction result.

    Accepts Python numbers, numpy scalars/arrays and single-element sequences.
    Rejects booleans, complex values, multi-valued predictions and non-finite
    numbers, because none of those is a usable scalar response.
    """
    if isinstance(value, bool):
        raise OracleContractError("The artifact returned a boolean, not a number.")
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise OracleContractError(
                f"The artifact returned {len(value)} values; exactly one is required."
            )
        return _coerce_scalar(value[0])
    if hasattr(value, "shape") and hasattr(value, "reshape"):  # numpy array / scalar
        flat = value.reshape(-1) if getattr(value, "ndim", 0) > 0 else value
        if hasattr(flat, "__len__"):
            if len(flat) != 1:
                raise OracleContractError(
                    f"The artifact returned {len(flat)} values; exactly one is required."
                )
            value = flat[0]
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OracleContractError(
            f"The artifact returned {type(value).__name__}, not a real number."
        ) from exc
    if isinstance(value, complex) or not math.isfinite(number):
        raise OracleContractError("The artifact returned a non-finite value.")
    return number


def _load_python_callable(spec: OracleSpec) -> Predictor:
    module_name = f"clarity_uploaded_oracle_{uuid.uuid4().hex}"
    module_spec = importlib.util.spec_from_file_location(module_name, spec.file_path)
    if module_spec is None or module_spec.loader is None:
        raise OracleContractError("The uploaded Python file could not be loaded.")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_name] = module
    try:
        module_spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        sys.modules.pop(module_name, None)
        raise OracleContractError(
            f"The uploaded Python file failed to import: {_sanitized_message(exc)}"
        ) from exc

    function = getattr(module, spec.entrypoint or "", None)
    if function is None:
        raise OracleContractError(
            f"The uploaded Python file defines no function named '{spec.entrypoint}'."
        )
    if not callable(function):
        raise OracleContractError(
            f"'{spec.entrypoint}' exists in the uploaded file but is not callable."
        )
    return function


def _load_joblib_predictor(spec: OracleSpec) -> Predictor:
    try:
        import joblib
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise OracleContractError(
            "Joblib model support is unavailable: the backend is missing the 'joblib' package."
        ) from exc

    try:
        model = joblib.load(spec.file_path)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        raise OracleContractError(
            f"The uploaded model could not be loaded: {_sanitized_message(exc)}"
        ) from exc

    if hasattr(model, "predict") and callable(model.predict):
        predict = model.predict

        def call(x: float) -> float:
            return _coerce_scalar(predict([[float(x)]]))

        return call

    if callable(model):
        return lambda x: _coerce_scalar(model(float(x)))

    raise OracleContractError(
        "The uploaded model is neither callable nor an estimator exposing predict()."
    )


def _onnx_feature_width(input_shape: Sequence[Any]) -> int:
    """Infer how many features the graph expects after the batch dimension."""
    shape = list(input_shape or [])
    if not shape:
        return 1
    if len(shape) == 1:
        return 1
    width = shape[1]
    if isinstance(width, int):
        return width
    return 1


def _load_onnx_predictor(spec: OracleSpec) -> Predictor:
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise OracleContractError(
            "ONNX model support is unavailable: the backend is missing 'onnxruntime'."
        ) from exc

    try:
        session = ort.InferenceSession(spec.file_path, providers=["CPUExecutionProvider"])
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        raise OracleContractError(
            f"The uploaded ONNX model could not be loaded: {_sanitized_message(exc)}"
        ) from exc

    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise OracleContractError(
            f"The ONNX model exposes {len(inputs)} inputs; exactly one is supported."
        )
    input_meta = inputs[0]
    if _onnx_feature_width(input_meta.shape) != 1:
        raise OracleContractError(
            "The ONNX model expects more than one feature. A black box must map a "
            "single scalar input to a single scalar output."
        )

    rank = len(input_meta.shape or []) or 2
    feature_shape = (1, 1) if rank >= 2 else (1,)

    def call(x: float) -> float:
        value = np.array([[float(x)]], dtype=np.float32).reshape(feature_shape)
        outputs = session.run(None, {input_meta.name: value})
        if not outputs:
            raise OracleContractError("The ONNX model returned no output.")
        return _coerce_scalar(outputs[0])

    return call


def build_predictor(spec: OracleSpec) -> Predictor:
    """Load an artifact and return a scalar predictor for it."""
    if spec.kind is OracleKind.PYTHON_FUNCTION:
        return _load_python_callable(spec)
    if spec.kind is OracleKind.JOBLIB_MODEL:
        return _load_joblib_predictor(spec)
    if spec.kind is OracleKind.ONNX_MODEL:
        return _load_onnx_predictor(spec)
    raise OracleContractError(f"Unsupported oracle kind: {spec.kind}")


def probe_predictor(spec: OracleSpec, predictor: Optional[Predictor] = None) -> List[ProbeSample]:
    """Call the artifact at a few declared inputs to verify it answers at all."""
    predict = predictor if predictor is not None else build_predictor(spec)
    samples: List[ProbeSample] = []
    for x in probe_points(spec.min_value, spec.max_value):
        try:
            samples.append(ProbeSample(x=float(x), y=_coerce_scalar(predict(float(x)))))
        except Exception as exc:  # noqa: BLE001 - reported as a failed probe
            samples.append(ProbeSample(x=float(x), error=_sanitized_message(exc)))
    return samples


class UploadedOracle(OracleInterface):
    """OracleInterface implementation backed by a researcher-supplied artifact."""

    def __init__(
        self,
        spec: OracleSpec,
        predictor: Optional[Predictor] = None,
    ):
        self._spec = spec
        self._predictor = predictor if predictor is not None else build_predictor(spec)
        self._query_count = 0

    @property
    def spec(self) -> OracleSpec:
        return self._spec

    @property
    def domain(self) -> str:
        return self._spec.domain_name

    @property
    def query_count(self) -> int:
        return self._query_count

    def validate_input(self, experiment_input: ExperimentInput) -> Optional[StructuredError]:
        variable = self._spec.input_name
        if variable not in experiment_input.values:
            return StructuredError(
                code="MISSING_PARAMETER",
                message=f"Experiment input requires numeric variable '{variable}'.",
                details={"provided_keys": list(experiment_input.values.keys())},
            )

        value = experiment_input.values[variable]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return StructuredError(
                code="INVALID_PARAMETER_TYPE",
                message=f"Parameter '{variable}' must be a real number, got {type(value).__name__}.",
                details={variable: str(value)},
            )

        number = float(value)
        if not math.isfinite(number):
            return StructuredError(
                code="NON_FINITE_PARAMETER",
                message=f"Parameter '{variable}' must be finite.",
                details={variable: number},
            )
        if not (float(self._spec.min_value) <= number <= float(self._spec.max_value)):
            return StructuredError(
                code="OUT_OF_BOUNDS",
                message=(
                    f"Value {variable}={number} is outside the declared range "
                    f"[{self._spec.min_value}, {self._spec.max_value}]."
                ),
                details={
                    variable: number,
                    "min_value": float(self._spec.min_value),
                    "max_value": float(self._spec.max_value),
                },
            )
        return None

    def query(self, experiment_input: ExperimentInput) -> Observation:
        validation_error = self.validate_input(experiment_input)
        if validation_error is not None:
            return Observation(
                input_id=experiment_input.input_id,
                input_values=dict(experiment_input.values),
                output_values=None,
                status=ObservationStatus.INVALID_INPUT,
                error=validation_error,
                sequence_number=self._query_count + 1,
            )

        self._query_count += 1
        variable = self._spec.input_name
        x = float(experiment_input.values[variable])

        try:
            y = _coerce_scalar(self._predictor(x))
        except Exception as exc:  # noqa: BLE001 - contained artifact failure
            return Observation(
                observation_id=generate_id("obs"),
                input_id=experiment_input.input_id,
                input_values={variable: x},
                output_values=None,
                status=ObservationStatus.ORACLE_FAILURE,
                sequence_number=self._query_count,
                timestamp=utc_now_iso(),
                error=StructuredError(
                    code="ARTIFACT_EXECUTION_ERROR",
                    message=_sanitized_message(exc),
                    details={"kind": self._spec.kind.value},
                ),
            )

        return Observation(
            observation_id=generate_id("obs"),
            input_id=experiment_input.input_id,
            input_values={variable: x},
            output_values={self._spec.output_name: round(y, 6)},
            status=ObservationStatus.SUCCESS,
            sequence_number=self._query_count,
            timestamp=utc_now_iso(),
        )


__all__ = ["UploadedOracle", "build_predictor", "probe_predictor"]
