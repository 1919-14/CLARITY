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
from datetime import datetime, timezone
from typing import Optional, Sequence, Tuple

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


def compute_dataset_snapshot_id(run_id: str, observation_ids: Sequence[str]) -> str:
    """Return a stable identifier for the exact observation set used.

    The identifier is a truncated SHA-256 over the run ID plus the sorted,
    de-duplicated observation IDs. It is derived from identity, not from a
    count: two different observation sets of the same size produce different
    snapshots, and the same set always produces the same value, so a historical
    evaluation keeps identifying its original dataset even after new
    observations arrive.

    The digest is an identity fingerprint, not a cryptographic signature or a
    tamper-proof commitment; it proves set equality against the same inputs.
    """
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id is required to compute a dataset snapshot id.")
    ordered = sorted({oid for oid in observation_ids if isinstance(oid, str)})
    payload = json.dumps(
        {"run_id": run_id, "observations": ordered},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{DATASET_SNAPSHOT_PREFIX}{digest}"


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
    "InvalidHistoryCursorError",
    "REVISION_SOURCE_RESEARCHER",
    "compute_dataset_snapshot_id",
    "decode_history_cursor",
    "encode_history_cursor",
]
