"""Contracts for user-supplied black-box oracles.

A researcher can attach their own simulator or trained model instead of the
built-in polynomial oracle. CLARITY then probes it through the same
``OracleInterface`` as any other black box: it submits inputs and records the
scalar responses. The artifact's internals are never inspected, and no
ground-truth formula is ever claimed for it.

Trust boundary
--------------
Artifacts are executed **inside the backend process** (a deliberate choice for
this feature: uploaded code is treated as trusted). Loading a pickled model or
importing a Python module can run arbitrary code with the backend user's
privileges. The validation here is about *shape and provenance* — kind, bounds,
identifier syntax, size — never about safety.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Tuple


class OracleKind(str, Enum):
    """How an uploaded artifact is invoked."""

    PYTHON_FUNCTION = "python_function"
    JOBLIB_MODEL = "joblib_model"
    ONNX_MODEL = "onnx_model"


KIND_LABELS: Dict[OracleKind, str] = {
    OracleKind.PYTHON_FUNCTION: "Python function",
    OracleKind.JOBLIB_MODEL: "Joblib / pickle model",
    OracleKind.ONNX_MODEL: "ONNX model",
}

# Extension → kind. Extensions are only a hint: the request may state the kind
# explicitly, but it must agree with the extension it was uploaded under.
EXTENSION_KINDS: Dict[str, OracleKind] = {
    ".py": OracleKind.PYTHON_FUNCTION,
    ".joblib": OracleKind.JOBLIB_MODEL,
    ".pkl": OracleKind.JOBLIB_MODEL,
    ".pickle": OracleKind.JOBLIB_MODEL,
    ".onnx": OracleKind.ONNX_MODEL,
}

MAX_ARTIFACT_BYTES = 25 * 1024 * 1024  # 25 MiB

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class OracleContractError(ValueError):
    """Raised when an uploaded artifact or its declared contract is invalid."""


def select_kind(filename: str) -> OracleKind:
    """Resolve the artifact kind from its filename extension."""
    extension = os.path.splitext(filename or "")[1].lower()
    kind = EXTENSION_KINDS.get(extension)
    if kind is None:
        supported = ", ".join(sorted(EXTENSION_KINDS))
        raise OracleContractError(
            f"Unsupported artifact type '{extension or 'unknown'}'. Supported extensions: {supported}."
        )
    return kind


def validate_identifier(value: str, field_name: str) -> str:
    """Validate a Python-level identifier (function name, variable name)."""
    text = (value or "").strip()
    if not IDENTIFIER_PATTERN.match(text):
        raise OracleContractError(
            f"{field_name} must be a valid identifier (letters, digits and underscores, "
            "not starting with a digit)."
        )
    return text


@dataclass(frozen=True)
class OracleSpec:
    """Everything needed to invoke one uploaded artifact.

    ``min_value``/``max_value`` are the search bounds the discovery engine may
    probe. They belong to the researcher's declaration: CLARITY does not widen
    them on its own.
    """

    kind: OracleKind
    file_path: str
    entrypoint: Optional[str] = None
    input_name: str = "x"
    output_name: str = "y"
    min_value: float = -10.0
    max_value: float = 10.0

    def __post_init__(self) -> None:
        if not isinstance(self.kind, OracleKind):
            raise OracleContractError("kind must be an OracleKind value.")
        if not self.file_path:
            raise OracleContractError("file_path is required.")
        validate_identifier(self.input_name, "input_name")
        validate_identifier(self.output_name, "output_name")
        if self.kind is OracleKind.PYTHON_FUNCTION:
            if not self.entrypoint:
                raise OracleContractError(
                    "A Python artifact must declare the function to call (entrypoint)."
                )
            validate_identifier(self.entrypoint, "entrypoint")
        for name, value in (("min_value", self.min_value), ("max_value", self.max_value)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise OracleContractError(f"{name} must be a real number.")
            if not math.isfinite(float(value)):
                raise OracleContractError(f"{name} must be finite.")
        if float(self.min_value) >= float(self.max_value):
            raise OracleContractError(
                "min_value must be strictly smaller than max_value; the search domain "
                "would otherwise contain no usable input."
            )

    @property
    def domain_name(self) -> str:
        return f"uploaded_{self.kind.value}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "entrypoint": self.entrypoint,
            "input_name": self.input_name,
            "output_name": self.output_name,
            "min_value": float(self.min_value),
            "max_value": float(self.max_value),
        }


@dataclass(frozen=True)
class ProbeSample:
    """One verification call made when an artifact is registered."""

    x: float
    y: Optional[float] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.y is not None

    def to_dict(self) -> Dict[str, Any]:
        return {"x": self.x, "y": self.y, "error": self.error}


def probe_points(min_value: float, max_value: float, count: int = 3) -> Tuple[float, ...]:
    """Deterministic probe inputs spread across the declared domain."""
    if count <= 0:
        return ()
    if count == 1:
        return (float(min_value),)
    span = float(max_value) - float(min_value)
    return tuple(
        float(min_value) + span * index / (count - 1) for index in range(count)
    )


def summarise_probe(samples: Iterable[ProbeSample]) -> Dict[str, Any]:
    """Reduce probe samples into a compact, honest summary."""
    items = list(samples)
    if not items:
        return {"samples": [], "ok_count": 0, "failed_count": 0}
    return {
        "samples": [sample.to_dict() for sample in items],
        "ok_count": sum(1 for sample in items if sample.ok),
        "failed_count": sum(1 for sample in items if not sample.ok),
    }


__all__ = [
    "KIND_LABELS",
    "MAX_ARTIFACT_BYTES",
    "OracleContractError",
    "OracleKind",
    "OracleSpec",
    "ProbeSample",
    "probe_points",
    "select_kind",
    "summarise_probe",
    "validate_identifier",
]
