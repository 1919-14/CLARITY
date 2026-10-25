"""Service for the reusable uploaded-oracle library.

Owns the custody of researcher-supplied artifacts: where the bytes live, what
was declared about them, and what the registration probe actually observed.
Routes stay thin; the discovery engine receives only an ``OracleSpec``.

Trust boundary: artifacts execute in-process (see
``adapters.oracle.uploaded``), so this module validates declared shape and
provenance — size, extension, identifiers, bounds, scalar response — not safety.
"""

from __future__ import annotations

import hashlib
import math
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from werkzeug.utils import secure_filename

from ..adapters.oracle.uploaded import build_predictor, probe_predictor
from ..adapters.persistence.models import OracleArtifactModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.oracle_contracts import (
    KIND_LABELS,
    MAX_ARTIFACT_BYTES,
    OracleContractError,
    OracleKind,
    OracleSpec,
    ProbeSample,
    select_kind,
    summarise_probe,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent


class OracleServiceError(Exception):
    """Base class for oracle library failures."""


class OracleNotFoundError(OracleServiceError):
    """Raised when an oracle id does not exist."""


class OracleInUseError(OracleServiceError):
    """Raised when an oracle still backs one or more runs."""


class OracleValidationError(OracleServiceError):
    """Raised when an upload is not a usable black box under its declared contract."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.details = details or {}


def default_storage_dir() -> str:
    """Directory holding uploaded artifacts, anchored to the backend package."""
    configured = os.environ.get("CLARITY_ORACLES_DIR")
    if configured:
        return os.path.abspath(configured)
    return (BACKEND_DIR / "oracle_uploads").as_posix()


def _safe_stored_filename(original: str) -> str:
    extension = os.path.splitext(original or "")[1].lower()
    base = secure_filename(original or "")
    if not base or not base.lower().endswith(extension):
        base = f"artifact{extension}"
    if len(base) > 120:
        stem, suffix = os.path.splitext(base)
        base = f"{stem[: max(1, 120 - len(suffix))]}{suffix}"
    return base


def _resolve_artifact_path(storage_dir: str, relative_path: str) -> str:
    root = os.path.abspath(storage_dir)
    candidate = os.path.abspath(os.path.join(root, relative_path))
    if os.path.commonpath([root, candidate]) != root:
        raise OracleValidationError("The artifact path escapes the oracle storage directory.")
    return candidate


def spec_from_model(model: OracleArtifactModel, storage_dir: str) -> OracleSpec:
    """Rebuild the invocation contract recorded for a stored artifact."""
    return OracleSpec(
        kind=OracleKind(model.kind),
        file_path=_resolve_artifact_path(storage_dir, model.storage_path),
        entrypoint=model.entrypoint,
        input_name=model.input_name,
        output_name=model.output_name,
        min_value=float(model.min_value),
        max_value=float(model.max_value),
    )


def _declared_kind(kind: Optional[str], filename: str) -> OracleKind:
    try:
        inferred = select_kind(filename)
    except OracleContractError as exc:
        raise OracleValidationError(str(exc)) from exc
    if not kind:
        return inferred
    try:
        declared = OracleKind(str(kind).strip())
    except ValueError as exc:
        supported = ", ".join(item.value for item in OracleKind)
        raise OracleValidationError(
            f"Unsupported oracle kind '{kind}'. Supported kinds: {supported}."
        ) from exc
    if declared is not inferred:
        raise OracleValidationError(
            f"The file extension indicates '{inferred.value}' but kind "
            f"'{declared.value}' was declared."
        )
    return declared


def _coerce_bound(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or value is None:
        raise OracleValidationError(f"{field_name} must be a real number.")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OracleValidationError(f"{field_name} must be a real number.") from exc
    if not math.isfinite(number):
        raise OracleValidationError(f"{field_name} must be finite.")
    return number


def create_oracle_service(
    session: Session,
    *,
    storage_dir: str,
    filename: str,
    content: bytes,
    name: Optional[str] = None,
    kind: Optional[str] = None,
    entrypoint: Optional[str] = None,
    input_name: str = "x",
    output_name: str = "y",
    min_value: Any = -10.0,
    max_value: Any = 10.0,
    actor: str = "researcher",
) -> Tuple[OracleArtifactModel, Dict[str, Any]]:
    """Register an uploaded artifact as a reusable oracle.

    The artifact is written to its own directory, then probed at three declared
    inputs. A registration is rejected when the declared contract cannot be
    built (wrong function name, incompatible model, non-scalar output) or when
    every probe call fails; partial failures are recorded honestly and kept.
    """
    if not filename:
        raise OracleValidationError("An artifact file is required.")
    if not content:
        raise OracleValidationError("The uploaded artifact is empty.")
    if len(content) > MAX_ARTIFACT_BYTES:
        raise OracleValidationError(
            f"The artifact is larger than the {MAX_ARTIFACT_BYTES // (1024 * 1024)} MiB limit."
        )

    resolved_kind = _declared_kind(kind, filename)
    resolved_min = _coerce_bound(min_value, "min_value")
    resolved_max = _coerce_bound(max_value, "max_value")

    oracle_id = f"oracle-{uuid.uuid4().hex[:12]}"
    stored_filename = _safe_stored_filename(filename)
    relative_path = f"{oracle_id}/{stored_filename}"
    target_path = _resolve_artifact_path(storage_dir, relative_path)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "wb") as handle:
        handle.write(content)

    try:
        spec = OracleSpec(
            kind=resolved_kind,
            file_path=target_path,
            entrypoint=(entrypoint or "").strip() or None,
            input_name=(input_name or "x").strip(),
            output_name=(output_name or "y").strip(),
            min_value=resolved_min,
            max_value=resolved_max,
        )
        predictor = build_predictor(spec)
        samples: List[ProbeSample] = probe_predictor(spec, predictor)
    except OracleContractError as exc:
        shutil.rmtree(os.path.dirname(target_path), ignore_errors=True)
        raise OracleValidationError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - converted into a 422, not a 500
        shutil.rmtree(os.path.dirname(target_path), ignore_errors=True)
        raise OracleValidationError(
            f"The artifact could not be registered: {exc.__class__.__name__}."
        ) from exc

    probe_summary = summarise_probe(samples)
    if probe_summary["ok_count"] == 0:
        shutil.rmtree(os.path.dirname(target_path), ignore_errors=True)
        errors = [sample.error for sample in samples if sample.error]
        raise OracleValidationError(
            "The artifact did not return a usable scalar at any probe input. "
            + (errors[0] if errors else ""),
            details={
                "probe": probe_summary,
                "expected": (
                    f"A single-number response for inputs between {resolved_min} "
                    f"and {resolved_max}."
                ),
            },
        )

    resolved_name = (name or "").strip() or os.path.splitext(filename)[0]
    model = OracleArtifactModel(
        oracle_id=oracle_id,
        name=resolved_name[:255],
        kind=resolved_kind.value,
        original_filename=filename[:255],
        stored_filename=stored_filename,
        storage_path=relative_path,
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        entrypoint=spec.entrypoint,
        input_name=spec.input_name,
        output_name=spec.output_name,
        min_value=float(spec.min_value),
        max_value=float(spec.max_value),
        probe_json=probe_summary,
        created_by=actor,
    )

    repo = ClarityRepository(session)
    try:
        repo.create_oracle_artifact(model)
    except Exception:
        shutil.rmtree(os.path.dirname(target_path), ignore_errors=True)
        raise

    return model, probe_summary


def get_oracle_service(
    session: Session, oracle_id: str
) -> Tuple[OracleArtifactModel, int]:
    """Return one artifact and the number of runs it backs."""
    repo = ClarityRepository(session)
    artifact = repo.get_oracle_artifact(oracle_id)
    if artifact is None:
        raise OracleNotFoundError(f"Oracle '{oracle_id}' not found.")
    return artifact, repo.count_runs_using_oracle(oracle_id)


def list_oracles_service(session: Session) -> List[Tuple[OracleArtifactModel, int]]:
    """Return every registered artifact, newest first, with its run count."""
    repo = ClarityRepository(session)
    return [
        (artifact, repo.count_runs_using_oracle(artifact.oracle_id))
        for artifact in repo.list_oracle_artifacts()
    ]


def delete_oracle_service(
    session: Session, *, storage_dir: str, oracle_id: str
) -> OracleArtifactModel:
    """Delete an artifact that no run references, including its stored bytes."""
    repo = ClarityRepository(session)
    artifact = repo.get_oracle_artifact(oracle_id)
    if artifact is None:
        raise OracleNotFoundError(f"Oracle '{oracle_id}' not found.")
    run_count = repo.count_runs_using_oracle(oracle_id)
    if run_count > 0:
        raise OracleInUseError(
            f"Oracle '{oracle_id}' is bound to {run_count} run(s) and its record cannot "
            "be deleted. Historical runs stay reproducible."
        )

    directory = os.path.dirname(_resolve_artifact_path(storage_dir, artifact.storage_path))
    repo.delete_oracle_artifact(oracle_id)
    shutil.rmtree(directory, ignore_errors=True)
    return artifact


def serialize_oracle(artifact: OracleArtifactModel, run_count: int) -> Dict[str, Any]:
    """Allowlist serialization: never exposes absolute server paths or bytes."""
    return {
        "oracle_id": artifact.oracle_id,
        "name": artifact.name,
        "kind": artifact.kind,
        "kind_label": KIND_LABELS.get(
            OracleKind(artifact.kind), artifact.kind.replace("_", " ")
        ),
        "original_filename": artifact.original_filename,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "entrypoint": artifact.entrypoint,
        "input_name": artifact.input_name,
        "output_name": artifact.output_name,
        "min_value": artifact.min_value,
        "max_value": artifact.max_value,
        "probe": artifact.probe_json
        or {"samples": [], "ok_count": 0, "failed_count": 0},
        "created_by": artifact.created_by,
        "created_at": artifact.created_at.isoformat() if artifact.created_at else None,
        "run_count": run_count,
    }


__all__ = [
    "OracleInUseError",
    "OracleNotFoundError",
    "OracleServiceError",
    "OracleValidationError",
    "create_oracle_service",
    "default_storage_dir",
    "delete_oracle_service",
    "get_oracle_service",
    "list_oracles_service",
    "serialize_oracle",
    "spec_from_model",
]
