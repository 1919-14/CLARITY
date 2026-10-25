"""API v1 endpoints for the uploaded black-box oracle library.

Endpoints:
  GET    /api/v1/oracles               List registered artifacts
  POST   /api/v1/oracles               Register an uploaded artifact (multipart/form-data)
  GET    /api/v1/oracles/<oracle_id>   Retrieve one artifact
  DELETE /api/v1/oracles/<oracle_id>   Delete an artifact no run references

Handlers stay thin: custody, probe verification and persistence live in
``services.oracle_service``. Artifact bytes and absolute server paths are never
serialized back to a client.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ...adapters.persistence.database import get_db_manager
from ...services.oracle_service import (
    OracleInUseError,
    OracleNotFoundError,
    OracleValidationError,
    create_oracle_service,
    default_storage_dir,
    delete_oracle_service,
    get_oracle_service,
    list_oracles_service,
    serialize_oracle,
)
from .shared import error_response, log_safe_error

oracles_v1_bp = Blueprint("oracles_v1", __name__)


def _storage_dir() -> str:
    return current_app.config.get("ORACLE_STORAGE_DIR") or default_storage_dir()


def _get_db():
    return get_db_manager(current_app.config.get("SQLALCHEMY_DATABASE_URI"))


@oracles_v1_bp.route("/oracles", methods=["GET"])
def list_oracles():
    """GET /api/v1/oracles — the reusable oracle library, newest first."""
    db = _get_db()
    try:
        with db.session_scope() as session:
            entries = list_oracles_service(session)
            payload = [serialize_oracle(artifact, count) for artifact, count in entries]
    except Exception as exc:  # noqa: BLE001
        log_safe_error("list oracles db error", exc)
        return error_response("An internal error occurred while listing oracles.", 500)

    return jsonify({"oracles": payload, "count": len(payload)}), 200


@oracles_v1_bp.route("/oracles", methods=["POST"])
def create_oracle():
    """POST /api/v1/oracles — register an uploaded artifact as a reusable oracle.

    Accepts ``multipart/form-data`` with a ``file`` part plus the declared
    contract: ``name``, ``kind`` (optional; inferred from the extension),
    ``entrypoint`` (required for Python artifacts), ``input_name``,
    ``output_name``, ``min_value`` and ``max_value``.
    """
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return error_response(
            "A file part named 'file' is required (multipart/form-data).", 400
        )

    form = request.form
    try:
        content = upload.read()
    except Exception as exc:  # noqa: BLE001
        log_safe_error("read oracle upload error", exc)
        return error_response("The uploaded file could not be read.", 400)

    db = _get_db()
    try:
        with db.session_scope() as session:
            artifact, probe = create_oracle_service(
                session,
                storage_dir=_storage_dir(),
                filename=upload.filename,
                content=content,
                name=form.get("name"),
                kind=form.get("kind") or None,
                entrypoint=form.get("entrypoint") or None,
                input_name=form.get("input_name") or "x",
                output_name=form.get("output_name") or "y",
                min_value=form.get("min_value", -10.0),
                max_value=form.get("max_value", 10.0),
                actor=form.get("actor") or "researcher",
            )
            payload = serialize_oracle(artifact, 0)
            payload["probe"] = probe
    except OracleValidationError as exc:
        return error_response(str(exc), 422, details=exc.details or None)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("create oracle db error", exc)
        return error_response("An internal error occurred while registering the oracle.", 500)

    return jsonify({"oracle": payload}), 201


@oracles_v1_bp.route("/oracles/<oracle_id>", methods=["GET"])
def get_oracle(oracle_id: str):
    """GET /api/v1/oracles/<oracle_id> — one artifact with its run count."""
    db = _get_db()
    try:
        with db.session_scope() as session:
            artifact, run_count = get_oracle_service(session, oracle_id)
            payload = serialize_oracle(artifact, run_count)
    except OracleNotFoundError as exc:
        return error_response(str(exc), 404)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("get oracle db error", exc)
        return error_response("An internal error occurred while retrieving the oracle.", 500)

    return jsonify({"oracle": payload}), 200


@oracles_v1_bp.route("/oracles/<oracle_id>", methods=["DELETE"])
def delete_oracle(oracle_id: str):
    """DELETE /api/v1/oracles/<oracle_id> — delete an artifact no run references.

    Query parameters:
      force=true  Detach any bound runs (set their oracle_id to NULL) before
                  deleting the artifact.  Run history records are preserved.
    """
    force = request.args.get("force", "").lower() in ("1", "true", "yes")
    db = _get_db()
    try:
        with db.session_scope() as session:
            artifact = delete_oracle_service(
                session, storage_dir=_storage_dir(), oracle_id=oracle_id, force=force
            )
            payload = serialize_oracle(artifact, 0)
    except OracleNotFoundError as exc:
        return error_response(str(exc), 404)
    except OracleInUseError as exc:
        return error_response(str(exc), 409)
    except OracleValidationError as exc:
        return error_response(str(exc), 422)
    except Exception as exc:  # noqa: BLE001
        log_safe_error("delete oracle db error", exc)
        return error_response("An internal error occurred while deleting the oracle.", 500)

    return jsonify({"oracle": payload, "message": "Oracle deleted."}), 200

