"""Reusable history retrieval service (Issue #17).

The service is the single read model over the append-only audit trail, the
immutable hypothesis revisions, and the evaluation records that reference them.
Route handlers stay thin: they validate request input, call one of these
functions, and serialize the result.

Ordering contract
-----------------
History is always ordered by ``(timestamp, event_id)`` ascending. ``event_id``
is the deterministic tie-breaker, so equal timestamps never produce an
ambiguous order. Cursor pagination compares strictly against the
``(timestamp, event_id)`` pair of the last record already returned, which keeps
page traversal stable while new events are appended between requests. Offset
pagination is supported for API compatibility with Issue #15's audit endpoint,
but it cannot make that guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    EvaluationResultModel,
    HypothesisModel,
    HypothesisRevisionModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.history_contracts import (
    decode_history_cursor,
    encode_history_cursor,
)

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


class HistoryNotFoundError(ValueError):
    """Raised when the run, hypothesis, or revision being queried does not exist.

    A resource that exists but has no history is *not* an error: callers get an
    empty list. Only a missing or out-of-scope resource raises.
    """


@dataclass(frozen=True)
class HistoryPage:
    """One page of chronological history plus explicit pagination metadata."""

    events: List[AuditEventModel]
    total: int
    limit: int
    offset: Optional[int]
    next_cursor: Optional[str]
    has_more: bool

    def to_dict(self) -> dict:
        return {
            "count": len(self.events),
            "total": self.total,
            "limit": self.limit,
            "offset": self.offset,
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


def normalize_page_size(limit: Optional[int]) -> int:
    """Clamp a page size into the supported range."""
    if limit is None:
        return DEFAULT_PAGE_SIZE
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("'limit' must be an integer.")
    if limit < 1:
        raise ValueError("'limit' must be >= 1.")
    return min(limit, MAX_PAGE_SIZE)


def get_run_or_raise(session: Session, run_id: str):
    """Resolve a run, raising ``HistoryNotFoundError`` when it does not exist."""
    run = ClarityRepository(session).get_run(run_id)
    if run is None:
        raise HistoryNotFoundError(f"Run '{run_id}' not found.")
    return run


def get_hypothesis_in_run(
    session: Session, run_id: str, hypothesis_id: str
) -> HypothesisModel:
    """Resolve a hypothesis and enforce that it belongs to ``run_id``.

    A hypothesis from another run is reported as not found rather than returned,
    so an unscoped identifier cannot be used to read another run's history.
    """
    hypothesis = ClarityRepository(session).get_hypothesis(hypothesis_id)
    if hypothesis is None or hypothesis.run_id != run_id:
        raise HistoryNotFoundError(
            f"Hypothesis '{hypothesis_id}' not found in run '{run_id}'."
        )
    return hypothesis


# ---------------------------------------------------------------------------
# Run action history
# ---------------------------------------------------------------------------


def list_run_history(
    session: Session,
    run_id: str,
    *,
    event_type: Optional[str] = None,
    actor: Optional[str] = None,
    hypothesis_id: Optional[str] = None,
    revision_id: Optional[str] = None,
    limit: Optional[int] = DEFAULT_PAGE_SIZE,
    offset: Optional[int] = None,
    cursor: Optional[str] = None,
) -> HistoryPage:
    """Return a run's meaningful actions in deterministic chronological order.

    ``cursor`` takes precedence over ``offset`` when both are supplied.
    """
    repo = ClarityRepository(session)
    get_run_or_raise(session, run_id)

    page_size = normalize_page_size(limit)
    filters = {
        "event_type": event_type,
        "actor": actor,
        "hypothesis_id": hypothesis_id,
        "revision_id": revision_id,
    }

    if cursor is not None:
        after = decode_history_cursor(cursor)
        if after is None:
            raise ValueError("Cursor must not be empty.")
        after_timestamp, after_event_id = after
        # Fetch one extra row so ``has_more`` is exact instead of guessed.
        rows = repo.list_audit_events_after(
            run_id=run_id,
            after_timestamp=after_timestamp,
            after_event_id=after_event_id,
            limit=page_size + 1,
            **filters,
        )
        has_more = len(rows) > page_size
        events = rows[:page_size]
        total = repo.count_audit_events_for_run(run_id=run_id, **filters)
        effective_offset = None
    else:
        effective_offset = 0 if offset is None else offset
        if not isinstance(effective_offset, int) or isinstance(effective_offset, bool):
            raise ValueError("'offset' must be an integer.")
        if effective_offset < 0:
            raise ValueError("'offset' must be >= 0.")
        events = repo.list_audit_events_for_run(
            run_id=run_id,
            limit=page_size,
            offset=effective_offset,
            **filters,
        )
        total = repo.count_audit_events_for_run(run_id=run_id, **filters)
        has_more = effective_offset + len(events) < total

    next_cursor = None
    if has_more and events:
        last = events[-1]
        next_cursor = encode_history_cursor(last.timestamp, last.event_id)

    return HistoryPage(
        events=events,
        total=total,
        limit=page_size,
        offset=effective_offset,
        next_cursor=next_cursor,
        has_more=has_more,
    )


# ---------------------------------------------------------------------------
# Revision history
# ---------------------------------------------------------------------------


def list_hypothesis_revisions(
    session: Session,
    hypothesis_id: str,
    *,
    run_id: Optional[str] = None,
) -> Tuple[HypothesisModel, List[HypothesisRevisionModel]]:
    """Return a hypothesis and every revision in ascending revision order."""
    repo = ClarityRepository(session)
    if run_id is not None:
        hypothesis = get_hypothesis_in_run(session, run_id, hypothesis_id)
    else:
        hypothesis = repo.get_hypothesis(hypothesis_id)
        if hypothesis is None:
            raise HistoryNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")
    revisions = repo.list_hypothesis_revisions(hypothesis_id)
    return hypothesis, revisions


def list_revision_evaluations(
    session: Session,
    revision_id: str,
    *,
    hypothesis_id: Optional[str] = None,
    limit: Optional[int] = DEFAULT_PAGE_SIZE,
    offset: Optional[int] = 0,
) -> Tuple[HypothesisRevisionModel, List[EvaluationResultModel], int, int, int]:
    """Return the revision and its evaluation history.

    Evaluations are returned oldest-first so the evolution of metrics over
    re-evaluations reads as a timeline. Returns
    ``(revision, evaluations, total, limit, offset)``.
    """
    repo = ClarityRepository(session)
    revision = repo.get_revision(revision_id)
    if revision is None:
        raise HistoryNotFoundError(f"Revision '{revision_id}' not found.")
    if hypothesis_id is not None and revision.hypothesis_id != hypothesis_id:
        raise HistoryNotFoundError(
            f"Revision '{revision_id}' does not belong to hypothesis '{hypothesis_id}'."
        )

    page_size = normalize_page_size(limit)
    effective_offset = 0 if offset is None else offset
    if not isinstance(effective_offset, int) or isinstance(effective_offset, bool):
        raise ValueError("'offset' must be an integer.")
    if effective_offset < 0:
        raise ValueError("'offset' must be >= 0.")

    evaluations = repo.list_evaluations_for_revision(
        revision_id,
        limit=page_size,
        offset=effective_offset,
        order="asc",
    )
    total = repo.count_evaluations_for_revision(revision_id)
    return revision, evaluations, total, page_size, effective_offset


def list_hypothesis_evaluations(
    session: Session,
    hypothesis_id: str,
    *,
    run_id: Optional[str] = None,
) -> Tuple[HypothesisModel, List[EvaluationResultModel]]:
    """Return every evaluation across every revision of a hypothesis."""
    repo = ClarityRepository(session)
    if run_id is not None:
        hypothesis = get_hypothesis_in_run(session, run_id, hypothesis_id)
    else:
        hypothesis = repo.get_hypothesis(hypothesis_id)
        if hypothesis is None:
            raise HistoryNotFoundError(f"Hypothesis '{hypothesis_id}' not found.")
    return hypothesis, repo.list_evaluations_for_hypothesis(hypothesis_id)
