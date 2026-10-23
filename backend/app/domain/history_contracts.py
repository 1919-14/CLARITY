"""Domain contracts for auditable researcher history (Issue #17).

This module owns the small, dependency-free pieces of the history feature:

* The actor categories recorded on audit events and hypothesis revisions.
* Event type names introduced by Issue #17 (existing Issue #15 names are
  reused for actions that already had one; nothing is renamed).
* The deterministic dataset snapshot identifier that gives every evaluation an
  exact, verifiable scientific input instead of a bare observation count.
* Opaque cursor encoding for deterministic history pagination.

Nothing here imports Flask, SQLAlchemy, or persistence models, so the domain
rules stay usable outside a request or database context.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

# ---------------------------------------------------------------------------
# Actor categories
# ---------------------------------------------------------------------------

ACTOR_RESEARCHER = "researcher"
ACTOR_AUTOMATED_AGENT = "automated_agent"
ACTOR_SYSTEM = "system"
ACTOR_WORKER = "worker"

# ---------------------------------------------------------------------------
# Event types introduced by Issue #17
#
# Issue #15 already defines HYPOTHESIS_PROPOSED / HYPOTHESIS_PINNED /
# HYPOTHESIS_UNPINNED / HYPOTHESIS_EXCLUDED / HYPOTHESIS_RESTORED /
# HYPOTHESIS_REEVALUATED / RUN_* in ``usage_contracts``. Those names are reused
# unchanged; only the actions that previously had no stable name are added here.
# ---------------------------------------------------------------------------

AUDIT_HYPOTHESIS_REVISED = "HYPOTHESIS_REVISED"
AUDIT_AUDIT_EVENT_CORRECTED = "AUDIT_EVENT_CORRECTED"

# Revision source recorded on researcher-authored revisions. Other documented
# values (``generator``, ``refit``, ``restoration``) are written by the producers
# that own them; see docs/researcher-history.md §4.
REVISION_SOURCE_RESEARCHER = "researcher"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppendOnlyViolationError(RuntimeError):
    """Raised when normal application code tries to mutate a historical record.

    Audit events and hypothesis revisions are append-only. Corrections are new
    records that reference the record they supersede.
    """


class InvalidHistoryCursorError(ValueError):
    """Raised when a pagination cursor is malformed or cannot be decoded."""


# ---------------------------------------------------------------------------
# Dataset provenance
# ---------------------------------------------------------------------------

DATASET_SNAPSHOT_PREFIX = "dset-"
DATASET_SNAPSHOT_SCHEMA_VERSION = 1
REVISION_REQUEST_FINGERPRINT_PREFIX = "req-"


def _canonical_value(value: Any, field: str) -> Any:
    """Normalize a stored value into a deterministic JSON-compatible form."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_value(item, field)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item, field) for item in value]
    enum_value = getattr(value, "value", None)
    if enum_value is not None and isinstance(enum_value, (bool, int, float, str)):
        return enum_value
    raise TypeError(
        f"Observation field {field!r} contains a value that cannot be canonicalized: "
        f"{type(value).__name__}."
    )


def _observation_field(observation: Any, name: str, default: Any = None) -> Any:
    if isinstance(observation, Mapping):
        return observation.get(name, default)
    return getattr(observation, name, default)


def canonical_observation_record(observation: Any) -> Dict[str, Any]:
    """Return the canonical scientific content of one observation.

    The record deliberately covers the fields that determine what was evaluated
    (inputs, outputs, status, partition and sequence), not just the identifier.
    Two observations with the same ID but different content therefore produce
    different records and different dataset snapshot ids.
    """
    if isinstance(observation, str):
        raise ValueError(
            "A dataset snapshot requires observation content, not a bare observation id. "
            "Pass the observation records that were evaluated."
        )
    observation_id = _observation_field(observation, "observation_id")
    if not isinstance(observation_id, str) or not observation_id:
        raise ValueError("Each observation in a dataset snapshot needs a non-empty id.")

    status = _observation_field(observation, "status")
    if status is not None and not isinstance(status, str):
        status = getattr(status, "value", status)

    sequence_number = _observation_field(observation, "sequence_number")
    if isinstance(sequence_number, bool) or not isinstance(sequence_number, (int, type(None))):
        raise TypeError(
            f"Observation {observation_id!r} has a non-integer sequence_number "
            f"({type(sequence_number).__name__})."
        )

    return {
        "observation_id": observation_id,
        "input_id": _canonical_value(_observation_field(observation, "input_id"), "input_id"),
        "input_values": _canonical_value(
            _observation_field(observation, "input_values"), "input_values"
        ),
        "output_values": _canonical_value(
            _observation_field(observation, "output_values"), "output_values"
        ),
        "status": status,
        "sequence_number": sequence_number,
        "is_held_out": bool(_observation_field(observation, "is_held_out", False)),
    }


@dataclass(frozen=True)
class DatasetSnapshot:
    """Content-addressed identity plus a reconstructable manifest of the data used."""

    snapshot_id: str
    manifest: Dict[str, Any]


def build_dataset_snapshot(run_id: str, observations: Iterable[Any]) -> DatasetSnapshot:
    """Fingerprint the canonical content of the exact observations an evaluation used.

    Unlike a hash of observation *ids*, this changes when any scientific field of
    an observation changes while its id stays the same. ``manifest`` carries the
    canonical records, so the evaluated inputs can be reconstructed and re-checked
    after the fact; it is persisted alongside the evaluation.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required to compute a dataset snapshot id.")

    by_id: Dict[str, Dict[str, Any]] = {}
    for observation in observations:
        record = canonical_observation_record(observation)
        existing = by_id.get(record["observation_id"])
        if existing is not None and existing != record:
            raise ValueError(
                f"Two different observations share observation id "
                f"{record['observation_id']!r}; the evaluated data is ambiguous."
            )
        by_id[record["observation_id"]] = record

    ordered = [by_id[observation_id] for observation_id in sorted(by_id)]
    payload = {
        "schema_version": DATASET_SNAPSHOT_SCHEMA_VERSION,
        "run_id": run_id,
        "observations": ordered,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    snapshot_id = f"{DATASET_SNAPSHOT_PREFIX}{digest}"
    manifest = dict(payload)
    manifest["snapshot_id"] = snapshot_id
    manifest["observation_count"] = len(ordered)
    return DatasetSnapshot(snapshot_id=snapshot_id, manifest=manifest)


def compute_dataset_snapshot_id(run_id: str, observations: Iterable[Any]) -> str:
    """Return the content-addressed snapshot id for the observations used.

    The digest covers the run ID plus the canonical scientific content of every
    observation, not merely its identifier: changing ``input_values``,
    ``output_values``, ``status``, ``is_held_out`` or ``sequence_number`` while
    keeping the id changes the snapshot. Ordering and duplicate ids are
    normalized, so the same content always yields the same id.

    The digest is an identity fingerprint, not a cryptographic signature or a
    tamper-proof commitment.
    """
    return build_dataset_snapshot(run_id, observations).snapshot_id


def compute_revision_request_fingerprint(
    formula: str,
    parameters: Optional[Mapping[str, Any]],
    complexity: Optional[int],
    model_family: Optional[str],
    creation_source: Optional[str],
) -> str:
    """Fingerprint every semantically relevant field of a revision request.

    Idempotency replay compares this canonical value, so reusing a request key
    with the same formula/parameters/complexity but a different model family is
    a conflict rather than a silent replay of the earlier revision.
    """
    payload = {
        "formula": formula,
        "parameters": _canonical_value(parameters, "parameters"),
        "complexity": complexity,
        "model_family": model_family,
        "creation_source": creation_source,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"{REVISION_REQUEST_FINGERPRINT_PREFIX}{digest}"


# ---------------------------------------------------------------------------
# Pagination cursor
#
# History is ordered by (timestamp, event_id). A cursor encodes the last record
# of the previous page so traversal uses a strict "strictly after this pair"
# comparison. That keeps pages stable when new events are appended between
# requests, which offset pagination cannot guarantee.
# ---------------------------------------------------------------------------


def encode_history_cursor(timestamp: datetime, event_id: str) -> str:
    """Encode a stable keyset cursor from an event's ordering pair."""
    if timestamp is None or not event_id:
        raise InvalidHistoryCursorError("A cursor requires a timestamp and an event id.")
    normalized = timestamp
    if normalized.tzinfo is None:
        normalized = normalized.replace(tzinfo=timezone.utc)
    payload = json.dumps(
        {"ts": normalized.astimezone(timezone.utc).isoformat(), "id": event_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_history_cursor(cursor: Optional[str]) -> Optional[Tuple[datetime, str]]:
    """Decode a cursor into ``(timestamp, event_id)``; ``None`` means first page."""
    if cursor is None or cursor == "":
        return None
    if not isinstance(cursor, str):
        raise InvalidHistoryCursorError("Cursor must be a string.")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parsed = json.loads(raw)
        ts = datetime.fromisoformat(parsed["ts"])
        event_id = parsed["id"]
    except (binascii.Error, UnicodeDecodeError, ValueError, KeyError, TypeError):
        raise InvalidHistoryCursorError(
            "Cursor is malformed. Omit 'cursor' to start from the first page."
        ) from None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if not isinstance(event_id, str) or not event_id:
        raise InvalidHistoryCursorError("Cursor is missing its event id.")
    return ts, event_id


__all__ = [
    "ACTOR_AUTOMATED_AGENT",
    "ACTOR_RESEARCHER",
    "ACTOR_SYSTEM",
    "ACTOR_WORKER",
    "AUDIT_AUDIT_EVENT_CORRECTED",
    "AUDIT_HYPOTHESIS_REVISED",
    "AppendOnlyViolationError",
    "DATASET_SNAPSHOT_PREFIX",
    "DATASET_SNAPSHOT_SCHEMA_VERSION",
    "DatasetSnapshot",
    "InvalidHistoryCursorError",
    "REVISION_REQUEST_FINGERPRINT_PREFIX",
    "REVISION_SOURCE_RESEARCHER",
    "build_dataset_snapshot",
    "canonical_observation_record",
    "compute_dataset_snapshot_id",
    "compute_revision_request_fingerprint",
    "decode_history_cursor",
    "encode_history_cursor",
]
