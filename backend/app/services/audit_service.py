"""Audit event service for append-only, sanitized run event logging (Issue #15/#17).

Invariants:
-----------
1. Append-only: audit events are immutable; existing events are never overwritten or deleted.
2. Sensitive data sanitization: credentials, tokens, API keys, and connection strings are scrubbed.
3. Provenance: each event records stable ID, run ID, event type, actor/source, and UTC timestamp,
   plus the structured resource references, before/after state and reason introduced by Issue #17.
4. Idempotency: duplicate submission of the same event_id is safe and does not duplicate records.
5. Corrections append a new event that references the event it supersedes; the original is kept.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

from ..adapters.persistence.models import AuditEventModel
from ..adapters.persistence.repository import ClarityRepository
from ..domain.history_contracts import AUDIT_AUDIT_EVENT_CORRECTED
from ..domain.sanitization import sanitize_metadata, sanitize_reason_text
from ..domain.usage_contracts import AuditEventConflictError


class AuditCorrectionTargetNotFound(ValueError):
    """Raised when a correction references an audit event that does not exist."""


def record_audit_event_service(
    session: Session,
    run_id: str,
    event_type: str,
    actor: str = "system",
    metadata: Optional[Mapping[str, Any]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    *,
    hypothesis_id: Optional[str] = None,
    revision_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    before: Optional[Mapping[str, Any]] = None,
    after: Optional[Mapping[str, Any]] = None,
    reason: Optional[str] = None,
    corrects_event_id: Optional[str] = None,
) -> AuditEventModel:
    """Append a structured, sanitized audit event for a run.

    Idempotent: if an event with event_id already exists and has identical content,
    it is returned. If existing content conflicts across runs, types, actors, or payloads,
    an AuditEventConflictError is raised to preserve append-only immutability.

    ``before`` and ``after`` are the state snapshots relevant to the action. They are
    sanitized like any other payload; whole ORM objects must never be passed in.
    """
    repo = ClarityRepository(session)
    eid = event_id or f"evt-{uuid.uuid4().hex[:12]}"

    sanitized_meta = sanitize_metadata(dict(metadata or {}))
    sanitized_before = sanitize_metadata(dict(before)) if before is not None else None
    sanitized_after = sanitize_metadata(dict(after)) if after is not None else None
    sanitized_reason = sanitize_reason_text(reason)
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
        hypothesis_id=hypothesis_id,
        revision_id=revision_id,
        actor_id=actor_id,
        correlation_id=correlation_id,
        before_json=sanitized_before,
        after_json=sanitized_after,
        reason=sanitized_reason,
        corrects_event_id=corrects_event_id,
    )
    repo.record_audit_event(event)
    session.flush()
    return event


def correct_audit_event_service(
    session: Session,
    run_id: str,
    corrects_event_id: str,
    reason: str,
    actor: str = "researcher",
    actor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    event_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> AuditEventModel:
    """Append a correction event for an earlier record without touching that record.

    The original event stays byte-for-byte intact; the correction points at it via
    ``corrects_event_id``, so a reader can follow the supersession chain.
    """
    repo = ClarityRepository(session)
    target = repo.get_audit_event(corrects_event_id)
    if target is None or target.run_id != run_id:
        raise AuditCorrectionTargetNotFound(
            f"Audit event '{corrects_event_id}' does not exist in run '{run_id}'."
        )
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("A correction requires a non-empty reason.")

    payload = dict(metadata or {})
    payload.setdefault("corrected_event_type", target.event_type)
    payload.setdefault("corrected_event_timestamp", target.timestamp.isoformat() if target.timestamp else None)

    return record_audit_event_service(
        session=session,
        run_id=run_id,
        event_type=AUDIT_AUDIT_EVENT_CORRECTED,
        actor=actor,
        actor_id=actor_id,
        correlation_id=correlation_id,
        metadata=payload,
        event_id=event_id,
        timestamp=timestamp,
        before={"event_id": target.event_id, "event_type": target.event_type},
        after={"event_id": target.event_id, "corrected": True},
        reason=reason,
        corrects_event_id=target.event_id,
    )


def list_audit_events_service(
    session: Session,
    run_id: str,
    event_type: Optional[str] = None,
    limit: Optional[int] = 50,
    offset: Optional[int] = 0,
    actor: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    revision_id: Optional[str] = None,
) -> Tuple[List[AuditEventModel], int]:
    """Retrieve chronological audit events for a run with optional filtering and pagination."""
    repo = ClarityRepository(session)
    events = repo.list_audit_events_for_run(
        run_id=run_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
        actor=actor,
        hypothesis_id=hypothesis_id,
        revision_id=revision_id,
    )
    total_count = repo.count_audit_events_for_run(
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        hypothesis_id=hypothesis_id,
        revision_id=revision_id,
    )
    return events, total_count


def get_audit_event_service(
    session: Session,
    event_id: str,
) -> Optional[AuditEventModel]:
    """Retrieve a single audit event by ID."""
    repo = ClarityRepository(session)
    return repo.get_audit_event(event_id)
