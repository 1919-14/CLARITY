"""Audit event service for append-only, sanitized run event logging (Issue #15).

Invariants:
-----------
1. Append-only: audit events are immutable; existing events are never overwritten or deleted.
2. Sensitive data sanitization: credentials, tokens, API keys, and connection strings are scrubbed.
3. Provenance: each event records stable ID, run ID, event type, actor/source, and UTC timestamp.
4. Idempotency: duplicate submission of the same event_id is safe and does not duplicate records.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from sqlalchemy.orm import Session

from ..adapters.persistence.models import AuditEventModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.sanitization import sanitize_exception_text, sanitize_metadata
from ..domain.usage_contracts import AuditEventConflictError


def record_audit_event_service(
    session: Session,
    run_id: str,
    event_type: str,
    actor: str = "system",
    metadata: Optional[Mapping[str, Any]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> AuditEventModel:
    """Append a structured, sanitized audit event for a run.

    Idempotent: if an event with event_id already exists and has identical content,
    it is returned. If existing content conflicts across runs, types, actors, or payloads,
    an AuditEventConflictError is raised to preserve append-only immutability.
    """
    repo = ClarityRepository(session)
    eid = event_id or f"evt-{uuid.uuid4().hex[:12]}"

    sanitized_meta = sanitize_metadata(dict(metadata or {}))
    event_time = timestamp or datetime.now(timezone.utc)

    existing = repo.get_audit_event(eid)
    if existing:
        conflicts = []
        if existing.run_id != run_id:
            conflicts.append(f"run_id mismatch ('{existing.run_id}' != '{run_id}')")
        if existing.event_type != event_type:
            conflicts.append(f"event_type mismatch ('{existing.event_type}' != '{event_type}')")
        if existing.actor != actor:
            conflicts.append(f"actor mismatch ('{existing.actor}' != '{actor}')")
        if (existing.metadata_json or {}) != (sanitized_meta or {}):
            conflicts.append("metadata payload mismatch")
        if timestamp is not None and existing.timestamp is not None:
            t_exist = existing.timestamp.replace(tzinfo=timezone.utc) if existing.timestamp.tzinfo is None else existing.timestamp
            t_req = event_time.replace(tzinfo=timezone.utc) if event_time.tzinfo is None else event_time
            if abs((t_exist - t_req).total_seconds()) > 0.001:
                conflicts.append(f"timestamp mismatch ('{t_exist.isoformat()}' != '{t_req.isoformat()}')")

        if conflicts:
            raise AuditEventConflictError(
                f"Conflicting audit event reuse for event_id '{eid}': {', '.join(conflicts)}."
            )
        return existing

    event = AuditEventModel(
        event_id=eid,
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        metadata_json=sanitized_meta,
        timestamp=event_time,
    )
    repo.record_audit_event(event)
    session.flush()
    return event


def list_audit_events_service(
    session: Session,
    run_id: str,
    event_type: Optional[str] = None,
    limit: Optional[int] = 50,
    offset: Optional[int] = 0,
) -> Tuple[List[AuditEventModel], int]:
    """Retrieve chronological audit events for a run with optional filtering and pagination."""
    repo = ClarityRepository(session)
    events = repo.list_audit_events_for_run(
        run_id=run_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
    total_count = repo.count_audit_events_for_run(run_id=run_id, event_type=event_type)
    return events, total_count


def get_audit_event_service(
    session: Session,
    event_id: str,
) -> Optional[AuditEventModel]:
    """Retrieve a single audit event by ID."""
    repo = ClarityRepository(session)
    return repo.get_audit_event(event_id)
