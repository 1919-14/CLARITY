"""Persistence-backed per-run budget manager (Issue #14).

Enforcement model
-----------------
Admission is a single conditional ``UPDATE`` against the run's counter row::

    UPDATE run_budget_usage
       SET queries_reserved = queries_reserved + 1
     WHERE run_id = :run_id AND queries_reserved < :limit

The database decides whether capacity exists, so the invariant
``admitted <= configured limit`` holds across threads, processes, and service
instances rather than only inside one Python process. No Python lock is used as
an enforcement mechanism, and no transaction is held open across an oracle or
remote call: reservations are committed in short transactions, the work runs
outside any transaction, and the outcome is recorded in another short
transaction.

Reservation semantics
---------------------
* A reservation is durable and keyed by ``(run_id, resource, operation_id)``;
  duplicate delivery of the same logical operation is idempotent.
* Capacity is returned only when work was *provably never dispatched*
  (``released``). A timed-out or crashed dispatch stays charged
  (``ambiguous``/``reserved``) because the oracle may already have run.
* Completed observations and evaluations are never rolled back by exhaustion.

Cost honesty
------------
``nodus-compute 1.0.27`` exposes no verified pre-execution cost estimate,
per-run spending cap, or settled billing callback. The configured spend ceiling
is therefore application-enforced and estimate-based. It stops *future*
scheduling; it does not terminate already-running remote work and it is never
reported as a provider guarantee.
"""

from __future__ import annotations

import math
import threading
import uuid
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..adapters.persistence.models import (
    AuditEventModel,
    BudgetReservationModel,
    ComputeTaskModel,
    RunBudgetUsageModel,
    RunModel,
    UsageRecordModel,
)
from ..adapters.persistence.repository import ClarityRepository
from ..domain.budget_contracts import (
    CONFIG_FIELDS,
    BudgetAdmissionDenied,
    BudgetLimitStatus,
    BudgetNotFoundError,
    BudgetResource,
    BudgetStatus,
    BudgetStopReason,
    BudgetValidationError,
    CostDataState,
    RunBudgetConfig,
    ZERO_SEMANTICS,
)

# ==============================================================================
# Constants
# ==============================================================================

RESERVATION_RESERVED = "reserved"
RESERVATION_COMMITTED = "committed"
RESERVATION_RELEASED = "released"
RESERVATION_AMBIGUOUS = "ambiguous"

OUTCOME_COMPLETED = "completed"
OUTCOME_FAILED = "failed"
OUTCOME_RELEASED = "released"
OUTCOME_AMBIGUOUS = "ambiguous"
VALID_OUTCOMES = frozenset(
    {OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_RELEASED, OUTCOME_AMBIGUOUS}
)

TERMINAL_RUN_STATUSES = frozenset({"completed", "budget_exhausted", "cancelled", "failed", "stopped_by_budget"})

# ``queued`` is the canonical lifecycle state for a run that has been created but
# not started (see ``RunLifecycleState``); it is still active for admission.
RUNNING_RUN_STATUSES = frozenset({"pending", "queued", "running"})
PROMOTABLE_RUN_STATUSES = frozenset({"pending", "queued"})

DEFAULT_STALE_RESERVATION_SECONDS = 900

CANCELLED_TASK_STATUSES = frozenset({"pending", "running"})

_COST_UNAVAILABLE_LIMITATION = (
    "Provider cost data is unavailable or not yet settled; monetary values are reported as "
    "null, never as zero, and the spend ceiling cannot be verified against a provider bill."
)
_NO_PROVIDER_CAP_LIMITATION = (
    "nodus-compute 1.0.27 exposes no verified per-run spending cap. The spend ceiling is an "
    "application-side estimate gate: it stops future scheduling and never guarantees a "
    "provider-side maximum charge."
)
_RUNTIME_LIMITATION = (
    "The runtime limit stops admission of new work. It cannot interrupt an already-running "
    "blocking call or remote provider work that has already been submitted."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize a persisted timestamp (SQLite may drop tzinfo) to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ==============================================================================
# Configuration resolution
# ==============================================================================

def resolve_budget_config(run: RunModel) -> RunBudgetConfig:
    """Resolve the authoritative budget config for *run*.

    New runs store a canonical snapshot. Legacy runs (created before Issue #14)
    are resolved from the existing nullable columns, with omitted values taking
    the documented defaults so every run still has an explicit budget.
    """
    snapshot = run.budget_config_json
    if isinstance(snapshot, Mapping):
        payload = {key: snapshot[key] for key in CONFIG_FIELDS if key in snapshot}
        try:
            return RunBudgetConfig.from_mapping(payload)
        except BudgetValidationError:
            # A corrupt snapshot must not silently widen the budget; fall back
            # to the conservative documented defaults.
            return RunBudgetConfig()

    legacy: Dict[str, Any] = {}
    if run.max_queries is not None:
        legacy["max_oracle_queries"] = run.max_queries
    if run.max_evaluations is not None:
        legacy["max_candidate_evaluations"] = run.max_evaluations
    if run.max_runtime_seconds is not None:
        legacy["max_runtime_seconds"] = run.max_runtime_seconds
    if run.spend_ceiling_usd is not None:
        legacy["max_compute_spend_usd"] = run.spend_ceiling_usd
    if run.max_concurrent_remote_tasks is not None:
        legacy["max_concurrent_remote_tasks"] = run.max_concurrent_remote_tasks
    if run.max_generated_hypotheses is not None:
        legacy["max_generated_hypotheses"] = run.max_generated_hypotheses

    try:
        return RunBudgetConfig.from_mapping(legacy)
    except BudgetValidationError:
        return RunBudgetConfig()


def _audit(
    session: Session,
    run_id: str,
    event_type: str,
    metadata: Mapping[str, Any],
    *,
    actor: str = "system",
) -> AuditEventModel:
    event = AuditEventModel(
        event_id=_new_id("evt"),
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        metadata_json=dict(metadata),
    )
    session.add(event)
    return event


def initialize_run_budget(
    session: Session,
    run: RunModel,
    config: Optional[RunBudgetConfig] = None,
    *,
    now: Optional[datetime] = None,
    actor: str = "system",
) -> RunBudgetUsageModel:
    """Persist *config* on *run* and create its counter row if absent.

    Idempotent: existing counters are never reset, so calling this again after
    a restart cannot reopen spent capacity.
    """
    resolved = config if config is not None else resolve_budget_config(run)
    if not isinstance(resolved, RunBudgetConfig):
        raise BudgetValidationError("config must be a RunBudgetConfig.")

    # The budget is immutable once recorded; a second initialization with a
    # different config is a programmer/API error rather than a silent rewrite.
    existing_snapshot = run.budget_config_json
    if isinstance(existing_snapshot, Mapping) and existing_snapshot:
        current = RunBudgetConfig.from_mapping(
            {key: existing_snapshot[key] for key in CONFIG_FIELDS if key in existing_snapshot}
        )
        if current != resolved:
            raise BudgetValidationError(
                "The run budget is immutable once initialized.",
                [
                    {
                        "field": "budget",
                        "code": "IMMUTABLE_BUDGET",
                        "message": "A run's budget cannot be changed after it is created.",
                    }
                ],
            )

    run.max_queries = resolved.max_oracle_queries
    run.max_evaluations = resolved.max_candidate_evaluations
    run.max_runtime_seconds = resolved.max_runtime_seconds
    run.spend_ceiling_usd = resolved.max_compute_spend_usd
    run.max_concurrent_remote_tasks = resolved.max_concurrent_remote_tasks
    run.max_generated_hypotheses = resolved.max_generated_hypotheses
    run.budget_config_json = resolved.to_dict()

    usage = session.get(RunBudgetUsageModel, run.run_id)
    if usage is None:
        usage = RunBudgetUsageModel(
            run_id=run.run_id,
            queries_reserved=0,
            queries_completed=0,
            queries_failed=0,
            queries_released=0,
            evaluations_reserved=0,
            evaluations_completed=0,
            evaluations_failed=0,
            evaluations_released=0,
            hypotheses_generated=0,
            hypotheses_released=0,
            active_remote_tasks=0,
            estimated_cost_usd=None,
            accruing_cost_usd=None,
            settled_cost_usd=None,
            cost_data_state=CostDataState.NOT_STARTED.value,
            is_exhausted=False,
            stop_reason=None,
        )
        session.add(usage)
        session.flush()
        _audit(
            session,
            run.run_id,
            "BUDGET_CONFIGURED",
            {
                "budget": resolved.to_dict(),
                "execution_mode": run.execution_mode,
                "enforcement": {
                    "oracle_queries": "application",
                    "candidate_evaluations": "application",
                    "remote_concurrency": "application",
                    "runtime": "application",
                    "compute_spend": resolved.cost_enforcement(run.execution_mode).value,
                },
            },
            actor=actor,
        )
        session.flush()

    return usage


def _require_run(session: Session, run_id: str) -> RunModel:
    run = session.get(RunModel, run_id)
    if run is None:
        raise BudgetNotFoundError(f"Run '{run_id}' not found.")
    return run


def _resolve(
    session: Session, run_id: str
) -> tuple[RunModel, RunBudgetConfig, RunBudgetUsageModel]:
    run = _require_run(session, run_id)
    config = resolve_budget_config(run)
    usage = session.get(RunBudgetUsageModel, run_id)
    if usage is None:
        usage = initialize_run_budget(session, run, config)
    return run, config, usage


# ==============================================================================
# Runtime clock and cost helpers
# ==============================================================================

def _start_run_clock(
    run: RunModel, config: RunBudgetConfig, now: datetime
) -> None:
    """Lazily start the run's clock when its first unit of work is admitted."""
    if run.started_at is None:
        run.started_at = now
        if run.status in PROMOTABLE_RUN_STATUSES:
            run.status = "running"
    if run.deadline_at is None:
        started = _as_utc(run.started_at) or now
        run.deadline_at = started + timedelta(seconds=config.max_runtime_seconds)


def _known_cost(usage: RunBudgetUsageModel) -> Optional[float]:
    """Best-known monetary outlook, or None when no reliable figure exists."""
    parts = [
        usage.settled_cost_usd,
        usage.estimated_cost_usd,
        usage.accruing_cost_usd,
    ]
    if all(part is None for part in parts):
        return None
    settled = float(usage.settled_cost_usd or 0.0)
    # An estimate and accruing figure can describe the same usage; take the
    # larger rather than summing them and double-counting.
    accruing = max(
        float(usage.estimated_cost_usd or 0.0),
        float(usage.accruing_cost_usd or 0.0),
    )
    return settled + accruing


def _denial(
    reason: BudgetStopReason,
    resource: BudgetResource,
    message: str,
    *,
    limit: Optional[int] = None,
    used: Optional[int] = None,
) -> BudgetAdmissionDenied:
    return BudgetAdmissionDenied(
        reason=reason,
        resource=resource,
        message=message,
        limit=limit,
        used=used,
    )


def _admission_denial(
    run: RunModel,
    config: RunBudgetConfig,
    usage: RunBudgetUsageModel,
    now: datetime,
) -> Optional[BudgetAdmissionDenied]:
    """Return a denial if no further work may be admitted.

    Terminal admission limits are re-checked atomically by the reservation
    itself; this function covers lifecycle, recorded exhaustion, runtime, and
    cost gates.
    """
    if run.status == "cancelled":
        return _denial(
            BudgetStopReason.CANCELLED,
            BudgetResource.ORACLE_QUERY,
            "The run is cancelled; no additional work may be admitted.",
        )
    if run.status == "failed":
        return _denial(
            BudgetStopReason.FAILED,
            BudgetResource.ORACLE_QUERY,
            "The run has failed; no additional work may be admitted.",
        )
    if usage.is_exhausted:
        stored = BudgetStopReason.from_value(usage.stop_reason)
        if stored is None:
            stored = BudgetStopReason.FAILED
        return _denial(
            stored,
            BudgetResource.ORACLE_QUERY,
            f"The run budget is exhausted ({usage.stop_reason or 'unknown reason'}).",
        )
    if run.status not in RUNNING_RUN_STATUSES:
        return _denial(
            BudgetStopReason.FAILED,
            BudgetResource.ORACLE_QUERY,
            f"The run is not active (status '{run.status}'); no work may be admitted.",
        )

    _start_run_clock(run, config, now)
    deadline = _as_utc(run.deadline_at)
    if config.max_runtime_seconds == 0:
        return _denial(
            BudgetStopReason.TIME_LIMIT_EXCEEDED,
            BudgetResource.ORACLE_QUERY,
            "The configured runtime limit is zero, so no work may start.",
            limit=0,
            used=0,
        )
    if deadline is not None and now >= deadline:
        return _denial(
            BudgetStopReason.TIME_LIMIT_EXCEEDED,
            BudgetResource.ORACLE_QUERY,
            "The run runtime limit has been reached; no additional work is scheduled.",
            limit=config.max_runtime_seconds,
        )

    ceiling = config.max_compute_spend_usd
    if ceiling is not None and run.execution_mode not in {"local", "mock"}:
        if ceiling == 0.0:
            return _denial(
                BudgetStopReason.COMPUTE_BUDGET_EXHAUSTED,
                BudgetResource.REMOTE_TASK,
                "The compute spend ceiling is zero, so no potentially billable remote work "
                "may be admitted.",
                limit=0,
                used=0,
            )
        known = _known_cost(usage)
        if known is not None and known >= ceiling:
            return _denial(
                BudgetStopReason.COMPUTE_BUDGET_EXHAUSTED,
                BudgetResource.REMOTE_TASK,
                "The application-side compute spend estimate has reached its ceiling; new work "
                "is no longer scheduled.",
                limit=0,
                used=int(known * 100),
            )
    return None


def _exhaust(
    session: Session,
    run: RunModel,
    config: RunBudgetConfig,
    usage: RunBudgetUsageModel,
    reason: BudgetStopReason,
    now: datetime,
    *,
    details: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Atomically record exhaustion; the first terminal reason wins.

    Returns True only for the call that recorded the transition. Later signals
    are ignored so a cancellation or an earlier limit reason is never
    overwritten by a concurrent one.
    """
    _start_run_clock(run, config, now)
    result = session.execute(
        update(RunBudgetUsageModel)
        .where(
            RunBudgetUsageModel.run_id == run.run_id,
            RunBudgetUsageModel.is_exhausted.is_(False),
        )
        .values(
            is_exhausted=True,
            stop_reason=reason.value,
            exhausted_at=now,
            updated_at=now,
        )
    )
    if result.rowcount != 1:
        session.refresh(usage)
        return False

    session.refresh(usage)
    if run.stop_reason is None:
        run.stop_reason = reason.value
    if run.status not in TERMINAL_RUN_STATUSES:
        run.status = reason.run_status
        if reason.is_terminal:
            run.completed_at = now

    metadata: Dict[str, Any] = {
        "reason": reason.value,
        "run_status": run.status,
        "budget": config.to_dict(),
        "usage": {
            "queries_reserved": usage.queries_reserved,
            "evaluations_reserved": usage.evaluations_reserved,
            "hypotheses_generated": usage.hypotheses_generated,
            "active_remote_tasks": usage.active_remote_tasks,
        },
    }
    if details:
        metadata.update(dict(details))
    _audit(session, run.run_id, "BUDGET_EXHAUSTED", metadata)
    _sync_legacy_usage(session, usage)

    # Outstanding application-side tasks are cancelled as part of the same
    # decision. Provider-side cancellation needs an execution adapter that this
    # service does not own; affected provider IDs are recorded in the audit
    # event. Cleanup runs in a savepoint so a cancellation failure can never
    # erase the exhaustion reason that was just recorded.
    try:
        with session.begin_nested():
            cancel_outstanding_run_tasks(session, run.run_id, stop_reason=reason, now=now)
    except Exception:  # noqa: BLE001 - cleanup must not undo exhaustion
        pass

    session.flush()
    return True


def _sync_legacy_usage(session: Session, usage: RunBudgetUsageModel) -> None:
    """Keep the pre-Issue-14 usage snapshot consistent with the counters."""
    record: Optional[UsageRecordModel] = ClarityRepository(session).get_latest_usage_for_run(
        usage.run_id
    )
    if record is None:
        return
    record.queries_executed = usage.queries_reserved
    record.evaluations_scheduled = usage.evaluations_reserved
    record.evaluations_completed = usage.evaluations_completed
    record.estimated_cost_usd = usage.estimated_cost_usd
    record.live_accruing_cost_usd = usage.accruing_cost_usd
    record.settled_cost_usd = usage.settled_cost_usd
    record.is_budget_exhausted = usage.is_exhausted
    record.exhaustion_reason = usage.stop_reason


def persist_exhaustion(
    session: Session,
    run_id: str,
    reason: BudgetStopReason,
    *,
    now: Optional[datetime] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Idempotently persist a terminal exhaustion decision in this transaction."""
    run, config, usage = _resolve(session, run_id)
    return _exhaust(session, run, config, usage, reason, now or _utc_now(), details=details)


# ==============================================================================
# Reservation primitives
# ==============================================================================

def _next_state(outcome: str) -> str:
    return {
        OUTCOME_COMPLETED: RESERVATION_COMMITTED,
        OUTCOME_FAILED: RESERVATION_COMMITTED,
        OUTCOME_RELEASED: RESERVATION_RELEASED,
        OUTCOME_AMBIGUOUS: RESERVATION_AMBIGUOUS,
    }[outcome]


def _reservation_denial(
    resource: BudgetResource, limit: int, used: int
) -> BudgetAdmissionDenied:
    if resource is BudgetResource.ORACLE_QUERY:
        return _denial(
            BudgetStopReason.QUERY_BUDGET_EXHAUSTED,
            resource,
            "The configured oracle query budget is exhausted; no additional query was sent.",
            limit=limit,
            used=used,
        )
    if resource is BudgetResource.CANDIDATE_EVALUATION:
        return _denial(
            BudgetStopReason.EVALUATION_LIMIT_REACHED,
            resource,
            "The configured candidate evaluation budget is exhausted.",
            limit=limit,
            used=used,
        )
    if resource is BudgetResource.HYPOTHESIS:
        return _denial(
            BudgetStopReason.HYPOTHESIS_LIMIT_REACHED,
            resource,
            "The configured hypothesis generation limit has been reached.",
            limit=limit,
            used=used,
        )
    return _denial(
        BudgetStopReason.CONCURRENCY_LIMIT_REACHED,
        resource,
        "The per-run remote concurrency limit is currently saturated.",
        limit=limit,
        used=used,
    )


def _create_reservation(
    session: Session,
    run_id: str,
    resource: BudgetResource,
    operation_id: str,
    quantity: int,
    now: datetime,
    details: Optional[Mapping[str, Any]] = None,
) -> tuple[BudgetReservationModel, bool]:
    """Insert a reservation or return the existing idempotent one."""
    repo = ClarityRepository(session)
    existing = repo.get_budget_reservation(run_id, resource.value, operation_id)
    if existing is not None:
        return existing, False

    reservation = BudgetReservationModel(
        reservation_id=_new_id("res"),
        run_id=run_id,
        resource=resource.value,
        operation_id=operation_id,
        state=RESERVATION_RESERVED,
        quantity=quantity,
        details_json=dict(details) if details else None,
        expires_at=now + timedelta(seconds=DEFAULT_STALE_RESERVATION_SECONDS),
    )
    try:
        with session.begin_nested():
            session.add(reservation)
            session.flush()
    except IntegrityError:
        existing = repo.get_budget_reservation(run_id, resource.value, operation_id)
        if existing is None:
            raise
        return existing, False
    return reservation, True


def _reserve(
    session: Session,
    run: RunModel,
    config: RunBudgetConfig,
    usage: RunBudgetUsageModel,
    resource: BudgetResource,
    quantity: int,
    operation_id: str,
    now: datetime,
    *,
    details: Optional[Mapping[str, Any]] = None,
) -> tuple[BudgetReservationModel, bool]:
    """Atomically admit *quantity* units of *resource*.

    Raises :class:`BudgetAdmissionDenied`; the caller's transaction is expected
    to roll back so an un-admitted reservation leaves no counter behind.
    """
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValueError("quantity must be an integer >= 1")

    reservation, created = _create_reservation(
        session, run.run_id, resource, operation_id, quantity, now, details
    )
    if not created:
        # Duplicate delivery of the same logical operation: never consume
        # capacity twice. The caller treats a non-reserved state as settled.
        return reservation, False

    limit = config.limit_for(resource)
    run_id = run.run_id

    if resource is BudgetResource.ORACLE_QUERY:
        assert limit is not None
        stmt = (
            update(RunBudgetUsageModel)
            .where(
                RunBudgetUsageModel.run_id == run_id,
                RunBudgetUsageModel.queries_reserved < limit,
                RunBudgetUsageModel.is_exhausted.is_(False),
            )
            .values(
                queries_reserved=RunBudgetUsageModel.queries_reserved + quantity,
                updated_at=now,
            )
        )
        used_column = "queries_reserved"
    elif resource is BudgetResource.CANDIDATE_EVALUATION:
        assert limit is not None
        stmt = (
            update(RunBudgetUsageModel)
            .where(
                RunBudgetUsageModel.run_id == run_id,
                RunBudgetUsageModel.evaluations_reserved < limit,
                RunBudgetUsageModel.is_exhausted.is_(False),
            )
            .values(
                evaluations_reserved=RunBudgetUsageModel.evaluations_reserved + quantity,
                updated_at=now,
            )
        )
        used_column = "evaluations_reserved"
    elif resource is BudgetResource.REMOTE_TASK:
        assert limit is not None
        stmt = (
            update(RunBudgetUsageModel)
            .where(
                RunBudgetUsageModel.run_id == run_id,
                RunBudgetUsageModel.active_remote_tasks + quantity <= limit,
                RunBudgetUsageModel.is_exhausted.is_(False),
            )
            .values(
                active_remote_tasks=RunBudgetUsageModel.active_remote_tasks + quantity,
                updated_at=now,
            )
        )
        used_column = "active_remote_tasks"
    else:  # HYPOTHESIS (limit may be disabled)
        if limit is None:
            stmt = (
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.is_exhausted.is_(False),
                )
                .values(
                    hypotheses_generated=RunBudgetUsageModel.hypotheses_generated + quantity,
                    updated_at=now,
                )
            )
        else:
            stmt = (
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.hypotheses_generated < limit,
                    RunBudgetUsageModel.is_exhausted.is_(False),
                )
                .values(
                    hypotheses_generated=RunBudgetUsageModel.hypotheses_generated + quantity,
                    updated_at=now,
                )
            )
        used_column = "hypotheses_generated"

    session.refresh(usage)
    current_used = int(getattr(usage, used_column))
    result = session.execute(stmt)
    if result.rowcount != 1:
        raise _reservation_denial(
            resource,
            limit if limit is not None else current_used + quantity,
            current_used,
        )
    session.refresh(usage)
    return reservation, True


def _record_outcome(
    session: Session,
    reservation_id: str,
    expected_resource: BudgetResource,
    outcome: str,
    now: datetime,
) -> None:
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {', '.join(sorted(VALID_OUTCOMES))}, got {outcome!r}"
        )
    reservation = session.get(BudgetReservationModel, reservation_id)
    if reservation is None:
        raise BudgetNotFoundError(f"Budget reservation '{reservation_id}' not found.")
    if reservation.resource != expected_resource.value:
        raise ValueError(
            f"Reservation '{reservation_id}' is for resource "
            f"'{reservation.resource}', not '{expected_resource.value}'."
        )
    if reservation.state != RESERVATION_RESERVED:
        # Idempotent: duplicate outcomes never double-count or double-release.
        return

    quantity = reservation.quantity
    run_id = reservation.run_id
    usage = session.get(RunBudgetUsageModel, run_id)

    if outcome == OUTCOME_RELEASED:
        if expected_resource is BudgetResource.ORACLE_QUERY:
            session.execute(
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.queries_reserved >= quantity,
                )
                .values(
                    queries_reserved=RunBudgetUsageModel.queries_reserved - quantity,
                    queries_released=RunBudgetUsageModel.queries_released + quantity,
                    updated_at=now,
                )
            )
        elif expected_resource is BudgetResource.CANDIDATE_EVALUATION:
            session.execute(
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.evaluations_reserved >= quantity,
                )
                .values(
                    evaluations_reserved=RunBudgetUsageModel.evaluations_reserved - quantity,
                    evaluations_released=RunBudgetUsageModel.evaluations_released + quantity,
                    updated_at=now,
                )
            )
        elif expected_resource is BudgetResource.HYPOTHESIS:
            session.execute(
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.hypotheses_generated >= quantity,
                )
                .values(
                    hypotheses_generated=RunBudgetUsageModel.hypotheses_generated - quantity,
                    hypotheses_released=RunBudgetUsageModel.hypotheses_released + quantity,
                    updated_at=now,
                )
            )
        elif expected_resource is BudgetResource.REMOTE_TASK:
            session.execute(
                update(RunBudgetUsageModel)
                .where(
                    RunBudgetUsageModel.run_id == run_id,
                    RunBudgetUsageModel.active_remote_tasks >= quantity,
                )
                .values(
                    active_remote_tasks=RunBudgetUsageModel.active_remote_tasks - quantity,
                    updated_at=now,
                )
            )
    elif outcome == OUTCOME_COMPLETED:
        if expected_resource is BudgetResource.ORACLE_QUERY:
            session.execute(
                update(RunBudgetUsageModel)
                .where(RunBudgetUsageModel.run_id == run_id)
                .values(
                    queries_completed=RunBudgetUsageModel.queries_completed + quantity,
                    updated_at=now,
                )
            )
        elif expected_resource is BudgetResource.CANDIDATE_EVALUATION:
            session.execute(
                update(RunBudgetUsageModel)
                .where(RunBudgetUsageModel.run_id == run_id)
                .values(
                    evaluations_completed=RunBudgetUsageModel.evaluations_completed + quantity,
                    updated_at=now,
                )
            )
    elif outcome == OUTCOME_FAILED:
        if expected_resource is BudgetResource.ORACLE_QUERY:
            session.execute(
                update(RunBudgetUsageModel)
                .where(RunBudgetUsageModel.run_id == run_id)
                .values(
                    queries_failed=RunBudgetUsageModel.queries_failed + quantity,
                    updated_at=now,
                )
            )
        elif expected_resource is BudgetResource.CANDIDATE_EVALUATION:
            session.execute(
                update(RunBudgetUsageModel)
                .where(RunBudgetUsageModel.run_id == run_id)
                .values(
                    evaluations_failed=RunBudgetUsageModel.evaluations_failed + quantity,
                    updated_at=now,
                )
            )

    reservation.state = _next_state(outcome)
    reservation.updated_at = now
    if usage is not None:
        session.refresh(usage)
        _sync_legacy_usage(session, usage)
    session.flush()


# ==============================================================================
# Module-level budget operations (used inside an existing transaction)
# ==============================================================================

def reserve_oracle_query(
    session: Session, run_id: str, operation_id: str, *, now: Optional[datetime] = None
) -> tuple[str, bool]:
    """Reserve one oracle invocation. Returns ``(reservation_id, newly_admitted)``."""
    moment = now or _utc_now()
    run, config, usage = _resolve(session, run_id)
    denial = _admission_denial(run, config, usage, moment)
    if denial is not None:
        raise denial
    reservation, created = _reserve(
        session,
        run,
        config,
        usage,
        BudgetResource.ORACLE_QUERY,
        1,
        operation_id,
        moment,
    )
    return reservation.reservation_id, created


def record_oracle_query_outcome(
    session: Session,
    reservation_id: str,
    outcome: str,
    *,
    now: Optional[datetime] = None,
) -> None:
    _record_outcome(
        session, reservation_id, BudgetResource.ORACLE_QUERY, outcome, now or _utc_now()
    )


def reserve_candidate_evaluations(
    session: Session,
    run_id: str,
    count: int,
    operation_id: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[str, bool]:
    moment = now or _utc_now()
    run, config, usage = _resolve(session, run_id)
    denial = _admission_denial(run, config, usage, moment)
    if denial is not None:
        raise denial
    reservation, created = _reserve(
        session,
        run,
        config,
        usage,
        BudgetResource.CANDIDATE_EVALUATION,
        count,
        operation_id,
        moment,
    )
    return reservation.reservation_id, created


def complete_candidate_evaluation(
    session: Session,
    reservation_id: str,
    succeeded: bool,
    *,
    now: Optional[datetime] = None,
) -> None:
    _record_outcome(
        session,
        reservation_id,
        BudgetResource.CANDIDATE_EVALUATION,
        OUTCOME_COMPLETED if succeeded else OUTCOME_FAILED,
        now or _utc_now(),
    )


def release_candidate_evaluation(
    session: Session,
    reservation_id: str,
    *,
    now: Optional[datetime] = None,
) -> None:
    _record_outcome(
        session,
        reservation_id,
        BudgetResource.CANDIDATE_EVALUATION,
        OUTCOME_RELEASED,
        now or _utc_now(),
    )


def reserve_hypotheses(
    session: Session,
    run_id: str,
    count: int,
    operation_id: str,
    *,
    now: Optional[datetime] = None,
) -> tuple[str, bool]:
    moment = now or _utc_now()
    run, config, usage = _resolve(session, run_id)
    denial = _admission_denial(run, config, usage, moment)
    if denial is not None:
        raise denial
    reservation, created = _reserve(
        session,
        run,
        config,
        usage,
        BudgetResource.HYPOTHESIS,
        count,
        operation_id,
        moment,
    )
    return reservation.reservation_id, created


def complete_hypothesis_generation(
    session: Session,
    reservation_id: str,
    succeeded: bool,
    *,
    now: Optional[datetime] = None,
) -> None:
    _record_outcome(
        session,
        reservation_id,
        BudgetResource.HYPOTHESIS,
        OUTCOME_COMPLETED if succeeded else OUTCOME_RELEASED,
        now or _utc_now(),
    )


# ==============================================================================
# Compute cost accounting
# ==============================================================================

# Serializes short budget transactions inside one process. The database
# conditional UPDATE remains the cross-process admission authority; this guard
# only prevents avoidable lock contention (notably SQLite's single-writer
# model) between threads of the same service.
_PROCESS_WRITE_GUARD = threading.RLock()

_UNSET = object()


def record_compute_cost(
    session: Session,
    run_id: str,
    *,
    estimated_cost_usd: Any = _UNSET,
    accruing_cost_usd: Any = _UNSET,
    settled_cost_usd: Any = _UNSET,
    data_state: Optional[CostDataState] = None,
    now: Optional[datetime] = None,
) -> RunBudgetUsageModel:
    """Record cost data without ever conflating unavailable with zero.

    ``None`` passed explicitly means "unavailable" for that tier. Omitted
    arguments leave the existing value untouched.
    """
    moment = now or _utc_now()
    run, config, usage = _resolve(session, run_id)

    def _validate(name: str, value: Any) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a finite number >= 0 or null.")
        as_float = float(value)
        if not math.isfinite(as_float) or as_float < 0.0:
            raise ValueError(f"{name} must be a finite number >= 0 or null.")
        return as_float

    if estimated_cost_usd is not _UNSET:
        usage.estimated_cost_usd = _validate("estimated_cost_usd", estimated_cost_usd)
        if usage.estimated_cost_usd is not None and not data_state:
            data_state = CostDataState.ESTIMATED_ONLY
    if accruing_cost_usd is not _UNSET:
        usage.accruing_cost_usd = _validate("accruing_cost_usd", accruing_cost_usd)
        if usage.accruing_cost_usd is not None and not data_state:
            data_state = CostDataState.PENDING_SETTLEMENT
    if settled_cost_usd is not _UNSET:
        usage.settled_cost_usd = _validate("settled_cost_usd", settled_cost_usd)
        if usage.settled_cost_usd is not None and not data_state:
            data_state = CostDataState.SETTLED
    if data_state is not None:
        usage.cost_data_state = data_state.value
    usage.updated_at = moment

    ceiling = config.max_compute_spend_usd
    if ceiling is not None and run.execution_mode not in {"local", "mock"}:
        known = _known_cost(usage)
        reached = ceiling == 0.0 and (known or 0.0) > 0.0
        if not reached and ceiling > 0.0 and known is not None:
            reached = known >= ceiling
        if reached and not usage.is_exhausted:
            _exhaust(
                session,
                run,
                config,
                usage,
                BudgetStopReason.COMPUTE_BUDGET_EXHAUSTED,
                moment,
                details={"known_cost_usd": known, "ceiling_usd": ceiling},
            )

    _sync_legacy_usage(session, usage)
    session.flush()
    return usage


def mark_cost_unavailable(
    session: Session, run_id: str, *, now: Optional[datetime] = None
) -> RunBudgetUsageModel:
    """Mark monetary data as unavailable; values become null, never zero."""
    run, config, usage = _resolve(session, run_id)
    usage.estimated_cost_usd = None
    usage.accruing_cost_usd = None
    usage.settled_cost_usd = None
    usage.cost_data_state = CostDataState.UNAVAILABLE.value
    usage.updated_at = now or _utc_now()
    _sync_legacy_usage(session, usage)
    session.flush()
    return usage


# ==============================================================================
# Status summary
# ==============================================================================

def _limit_status(
    resource: BudgetResource,
    limit: Optional[int],
    used: int,
) -> BudgetLimitStatus:
    if limit is None:
        return BudgetLimitStatus(
            resource=resource, limit=None, used=used, remaining=None, exhausted=False
        )
    remaining = max(0, limit - used)
    return BudgetLimitStatus(
        resource=resource,
        limit=limit,
        used=used,
        remaining=remaining,
        exhausted=remaining == 0,
    )


def get_budget_status(
    session: Session, run_id: str, *, now: Optional[datetime] = None
) -> BudgetStatus:
    """Return the consistent budget summary consumed by the API and dashboard."""
    moment = now or _utc_now()
    run, config, usage = _resolve(session, run_id)

    started = _as_utc(run.started_at)
    deadline = _as_utc(run.deadline_at)
    elapsed: Optional[float] = None
    remaining_seconds: Optional[float] = None
    if started is not None:
        elapsed = max(0.0, (moment - started).total_seconds())
    if deadline is not None:
        remaining_seconds = max(0.0, (deadline - moment).total_seconds())

    raw_state = usage.cost_data_state or CostDataState.NOT_STARTED.value
    try:
        cost_state = CostDataState(raw_state)
    except ValueError:
        cost_state = CostDataState.UNAVAILABLE

    parsed_stop = BudgetStopReason.from_value(usage.stop_reason or run.stop_reason)

    limitations: List[str] = []
    if run.execution_mode in {"local", "mock"}:
        if config.max_compute_spend_usd is not None:
            limitations.append(
                "Local/mock execution incurs no provider compute charge; the spend ceiling is "
                "recorded but has no provider bill to cap."
            )
    else:
        limitations.append(_NO_PROVIDER_CAP_LIMITATION)
        limitations.append(_RUNTIME_LIMITATION)
    if cost_state in {CostDataState.UNAVAILABLE, CostDataState.PENDING_SETTLEMENT}:
        limitations.append(_COST_UNAVAILABLE_LIMITATION)
    ambiguous = session.scalar(
        select(BudgetReservationModel.reservation_id).where(
            BudgetReservationModel.run_id == run_id,
            BudgetReservationModel.state == RESERVATION_AMBIGUOUS,
        )
    )
    if ambiguous is not None:
        limitations.append(
            "At least one reservation has an unknown outcome (for example a remote timeout) "
            "and remains charged conservatively."
        )

    return BudgetStatus(
        run_id=run.run_id,
        config=config,
        execution_mode=run.execution_mode,
        status=run.status,
        oracle_queries=_limit_status(
            BudgetResource.ORACLE_QUERY,
            config.max_oracle_queries,
            usage.queries_reserved,
        ),
        candidate_evaluations=_limit_status(
            BudgetResource.CANDIDATE_EVALUATION,
            config.max_candidate_evaluations,
            usage.evaluations_reserved,
        ),
        generated_hypotheses=_limit_status(
            BudgetResource.HYPOTHESIS,
            config.max_generated_hypotheses,
            usage.hypotheses_generated,
        ),
        query_counts={
            "completed": usage.queries_completed,
            "failed": usage.queries_failed,
            "released": usage.queries_released,
        },
        evaluation_counts={
            "completed": usage.evaluations_completed,
            "failed": usage.evaluations_failed,
            "released": usage.evaluations_released,
        },
        active_remote_tasks=usage.active_remote_tasks,
        max_concurrent_remote_tasks=config.max_concurrent_remote_tasks,
        run_started_at=started.isoformat() if started else None,
        deadline_at=deadline.isoformat() if deadline else None,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining_seconds,
        estimated_cost_usd=usage.estimated_cost_usd,
        accruing_cost_usd=usage.accruing_cost_usd,
        settled_cost_usd=usage.settled_cost_usd,
        cost_data_state=cost_state,
        cost_enforcement=config.cost_enforcement(run.execution_mode),
        provider_hard_cap_supported=False,
        is_exhausted=usage.is_exhausted,
        stop_reason=parsed_stop,
        raw_stop_reason=usage.stop_reason or run.stop_reason,
        zero_semantics=ZERO_SEMANTICS,
        limitations=limitations,
    )


# ==============================================================================
# Cancellation and recovery
# ==============================================================================

def cancel_outstanding_run_tasks(
    session: Session,
    run_id: str,
    *,
    stop_reason: BudgetStopReason,
    now: Optional[datetime] = None,
) -> List[str]:
    """Mark outstanding compute tasks cancelled and release their slots.

    Provider-side cancellation is *not* performed here: the returned provider
    task identifiers are for a caller that owns an execution adapter. A
    cancellation failure must not erase the recorded exhaustion reason.
    """
    moment = now or _utc_now()
    tasks = list(
        session.scalars(
            select(ComputeTaskModel).where(
                ComputeTaskModel.run_id == run_id,
                ComputeTaskModel.status.in_(CANCELLED_TASK_STATUSES),
            )
        ).all()
    )
    provider_ids: List[str] = []
    for task in tasks:
        task.status = "cancelled"
        task.completed_at = moment
        metadata = dict(task.error_metadata or {})
        metadata.update(
            {"code": "CANCELLED_BY_BUDGET", "stop_reason": stop_reason.value}
        )
        task.error_metadata = metadata
        if task.provider_task_id:
            provider_ids.append(task.provider_task_id)

    reservations = list(
        session.scalars(
            select(BudgetReservationModel).where(
                BudgetReservationModel.run_id == run_id,
                BudgetReservationModel.resource == BudgetResource.REMOTE_TASK.value,
                BudgetReservationModel.state == RESERVATION_RESERVED,
            )
        ).all()
    )
    for reservation in reservations:
        _record_outcome(
            session,
            reservation.reservation_id,
            BudgetResource.REMOTE_TASK,
            OUTCOME_RELEASED,
            moment,
        )

    if tasks:
        _audit(
            session,
            run_id,
            "RUN_TASKS_CANCELLED",
            {
                "stop_reason": stop_reason.value,
                "cancelled_task_count": len(tasks),
                "provider_task_ids": provider_ids,
                "provider_cancellation_confirmed": False,
            },
        )
    session.flush()
    return provider_ids


def recover_stale_reservations(
    session: Session,
    run_id: str,
    *,
    now: Optional[datetime] = None,
    stale_after_seconds: int = DEFAULT_STALE_RESERVATION_SECONDS,
) -> Dict[str, int]:
    """Reconcile reservations whose owning worker never reported back.

    Remote-task slots are released because the task is no longer active from
    the application's perspective. Query/evaluation/hypothesis reservations are
    marked ``ambiguous`` and stay charged: the oracle may already have executed
    the request, so releasing them could bypass accounting.
    """
    moment = now or _utc_now()
    usage = session.get(RunBudgetUsageModel, run_id)
    if usage is None:
        return {"released": 0, "ambiguous": 0}

    cutoff = moment - timedelta(seconds=stale_after_seconds)
    reservations = list(
        session.scalars(
            select(BudgetReservationModel).where(
                BudgetReservationModel.run_id == run_id,
                BudgetReservationModel.state == RESERVATION_RESERVED,
            )
        ).all()
    )
    released = 0
    ambiguous = 0
    for reservation in reservations:
        expires = _as_utc(reservation.expires_at)
        if expires is not None:
            stale = expires <= moment
        else:
            created = _as_utc(reservation.created_at)
            stale = created is not None and created <= cutoff
        if not stale:
            continue
        if reservation.resource == BudgetResource.REMOTE_TASK.value:
            _record_outcome(
                session,
                reservation.reservation_id,
                BudgetResource.REMOTE_TASK,
                OUTCOME_RELEASED,
                moment,
            )
            released += 1
        else:
            _record_outcome(
                session,
                reservation.reservation_id,
                BudgetResource(reservation.resource),
                OUTCOME_AMBIGUOUS,
                moment,
            )
            ambiguous += 1
    session.flush()
    return {"released": released, "ambiguous": ambiguous}


def detect_expired_runs(
    session: Session, *, now: Optional[datetime] = None
) -> List[str]:
    """Persist TIME_LIMIT_EXCEEDED for active runs whose deadline has passed.

    Intended for startup/periodic recovery so a restart cannot leave an expired
    run admitting work forever.
    """
    moment = now or _utc_now()
    runs = list(
        session.scalars(
            select(RunModel).where(
                RunModel.status.in_(RUNNING_RUN_STATUSES),
                RunModel.deadline_at.is_not(None),
            )
        ).all()
    )
    expired: List[str] = []
    for run in runs:
        deadline = _as_utc(run.deadline_at)
        if deadline is not None and moment >= deadline:
            config = resolve_budget_config(run)
            usage = session.get(RunBudgetUsageModel, run.run_id)
            if usage is None:
                usage = initialize_run_budget(session, run, config)
            if _exhaust(
                session,
                run,
                config,
                usage,
                BudgetStopReason.TIME_LIMIT_EXCEEDED,
                moment,
            ):
                expired.append(run.run_id)
    return expired


# ==============================================================================
# Transaction-scoped manager used by execution code
# ==============================================================================

class RunBudgetManager:
    """Short-transaction facade over the budget operations.

    ``session_scope`` must be a context manager factory returning a fresh
    :class:`Session` (for example ``DatabaseManager.session_scope``). Each call
    commits before the unit of work runs, so no database transaction is held
    open across an oracle call, an LLM call, or a remote Nodus task.
    """

    def __init__(
        self,
        session_scope: Callable[[], AbstractContextManager[Session]],
        run_id: str,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not callable(session_scope):
            raise TypeError("session_scope must be a callable returning a context manager.")
        self._session_scope = session_scope
        self._run_id = run_id
        self._clock = clock

    @property
    def run_id(self) -> str:
        return self._run_id

    @contextmanager
    def _locked_scope(self):
        """Short transaction with in-process write serialization."""
        with _PROCESS_WRITE_GUARD:
            with self._session_scope() as session:
                yield session

    @property
    def max_concurrent_remote_tasks(self) -> Optional[int]:
        """Configured per-run remote concurrency, or None when unreadable."""
        try:
            return self.status().max_concurrent_remote_tasks
        except Exception:  # noqa: BLE001 - diagnostic only
            return None

    # -- admission ------------------------------------------------------

    def _admit(
        self,
        resource: BudgetResource,
        operation_id: str,
        quantity: int = 1,
    ) -> tuple[Optional[BudgetAdmissionDenied], Optional[str]]:
        denial: Optional[BudgetAdmissionDenied] = None
        token: Optional[str] = None
        now = self._clock()

        with self._locked_scope() as session:
            try:
                with session.begin_nested():
                    run, config, usage = _resolve(session, self._run_id)
                    denial = _admission_denial(run, config, usage, now)
                    if denial is not None:
                        raise denial
                    reservation, _created = _reserve(
                        session,
                        run,
                        config,
                        usage,
                        resource,
                        quantity,
                        operation_id,
                        now,
                    )
                    token = reservation.reservation_id
            except BudgetAdmissionDenied as exc:
                denial = exc
                if exc.is_terminal:
                    # The reservation savepoint was rolled back; record the
                    # terminal decision in this (still open) transaction so it
                    # is committed by session_scope.
                    run, config, usage = _resolve(session, self._run_id)
                    _exhaust(session, run, config, usage, exc.reason, now)
                else:
                    raise

        if denial is not None:
            raise denial
        return denial, token

    def ensure_work_allowed(self, operation_id: str = "work") -> None:
        denial: Optional[BudgetAdmissionDenied] = None
        now = self._clock()
        with self._locked_scope() as session:
            run, config, usage = _resolve(session, self._run_id)
            denial = _admission_denial(run, config, usage, now)
            if denial is not None and denial.is_terminal:
                _exhaust(session, run, config, usage, denial.reason, now)
        if denial is not None:
            raise denial

    # -- oracle queries -------------------------------------------------

    def reserve_oracle_query(self, operation_id: str) -> str:
        _denial, token = self._admit(BudgetResource.ORACLE_QUERY, operation_id)
        assert token is not None
        return token

    def record_oracle_query_outcome(self, reservation_id: str, outcome: str) -> None:
        with self._locked_scope() as session:
            record_oracle_query_outcome(
                session, reservation_id, outcome, now=self._clock()
            )

    def release_oracle_query(self, reservation_id: str) -> None:
        self.record_oracle_query_outcome(reservation_id, OUTCOME_RELEASED)

    # -- candidate evaluations -----------------------------------------

    def reserve_candidate_evaluations(self, count: int, operation_id: str) -> str:
        _denial, token = self._admit(
            BudgetResource.CANDIDATE_EVALUATION, operation_id, count
        )
        assert token is not None
        return token

    def complete_candidate_evaluation(
        self, reservation_id: str, succeeded: bool
    ) -> None:
        with self._locked_scope() as session:
            complete_candidate_evaluation(
                session, reservation_id, succeeded, now=self._clock()
            )

    def release_candidate_evaluations(self, reservation_id: str) -> None:
        with self._locked_scope() as session:
            release_candidate_evaluation(session, reservation_id, now=self._clock())

    def release_candidate_evaluation(self, reservation_id: str) -> None:
        with self._locked_scope() as session:
            release_candidate_evaluation(session, reservation_id, now=self._clock())

    # -- hypotheses -----------------------------------------------------

    def reserve_hypotheses(self, count: int, operation_id: str) -> str:
        _denial, token = self._admit(BudgetResource.HYPOTHESIS, operation_id, count)
        assert token is not None
        return token

    def complete_hypothesis_generation(
        self, reservation_id: str, succeeded: bool
    ) -> None:
        with self._locked_scope() as session:
            complete_hypothesis_generation(
                session, reservation_id, succeeded, now=self._clock()
            )

    # -- remote concurrency --------------------------------------------

    def try_acquire_remote_slots(
        self, count: int, operation_id: str
    ) -> Optional[Sequence[str]]:
        """Acquire all *count* slots or none; ``None`` means backpressure."""
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("count must be an integer >= 1")
        now = self._clock()
        denial: Optional[BudgetAdmissionDenied] = None
        tokens: Optional[List[str]] = None

        with self._locked_scope() as session:
            try:
                with session.begin_nested():
                    run, config, usage = _resolve(session, self._run_id)
                    denial = _admission_denial(run, config, usage, now)
                    if denial is not None:
                        raise denial

                    expected = [f"{operation_id}:{index}" for index in range(count)]
                    existing = list(
                        session.scalars(
                            select(BudgetReservationModel).where(
                                BudgetReservationModel.run_id == self._run_id,
                                BudgetReservationModel.resource
                                == BudgetResource.REMOTE_TASK.value,
                                BudgetReservationModel.operation_id.in_(expected),
                            )
                        ).all()
                    )
                    if existing:
                        if len(existing) == count and all(
                            item.state == RESERVATION_RESERVED for item in existing
                        ):
                            log_tokens = [item.reservation_id for item in existing]
                            tokens = log_tokens
                        else:
                            raise ValueError(
                                "Inconsistent idempotency state for remote task reservations."
                            )
                    else:
                        result = session.execute(
                            update(RunBudgetUsageModel)
                            .where(
                                RunBudgetUsageModel.run_id == self._run_id,
                                RunBudgetUsageModel.active_remote_tasks + count
                                <= config.max_concurrent_remote_tasks,
                                RunBudgetUsageModel.is_exhausted.is_(False),
                            )
                            .values(
                                active_remote_tasks=RunBudgetUsageModel.active_remote_tasks
                                + count,
                                updated_at=now,
                            )
                        )
                        if result.rowcount != 1:
                            tokens = None
                        else:
                            reserved_tokens: List[str] = []
                            for item_operation_id in expected:
                                reservation, _created = _create_reservation(
                                    session,
                                    self._run_id,
                                    BudgetResource.REMOTE_TASK,
                                    item_operation_id,
                                    1,
                                    now,
                                )
                                reserved_tokens.append(reservation.reservation_id)
                            tokens = reserved_tokens
            except BudgetAdmissionDenied as exc:
                denial = exc
                if exc.is_terminal:
                    run, config, usage = _resolve(session, self._run_id)
                    _exhaust(session, run, config, usage, exc.reason, now)
                else:
                    raise

        if denial is not None:
            raise denial
        return tokens

    def acquire_remote_task_slot(self, operation_id: str) -> str:
        tokens = self.try_acquire_remote_slots(1, operation_id)
        if not tokens:
            raise BudgetAdmissionDenied(
                reason=BudgetStopReason.CONCURRENCY_LIMIT_REACHED,
                resource=BudgetResource.REMOTE_TASK,
                message="The per-run remote concurrency limit is currently saturated.",
            )
        return tokens[0]

    def release_remote_slots(self, tokens: Sequence[str]) -> None:
        now = self._clock()
        with self._locked_scope() as session:
            for token in tokens:
                record = session.get(BudgetReservationModel, token)
                if record is None or record.resource != BudgetResource.REMOTE_TASK.value:
                    continue
                _record_outcome(
                    session,
                    token,
                    BudgetResource.REMOTE_TASK,
                    OUTCOME_RELEASED,
                    now,
                )

    def release_remote_task_slot(self, token: str) -> None:
        self.release_remote_slots([token])

    # -- inspection -----------------------------------------------------

    def status(self) -> BudgetStatus:
        with self._locked_scope() as session:
            return get_budget_status(session, self._run_id, now=self._clock())

    def record_compute_cost(self, **kwargs: Any) -> None:
        with self._locked_scope() as session:
            record_compute_cost(session, self._run_id, now=self._clock(), **kwargs)

    def mark_exhausted(
        self,
        reason: BudgetStopReason,
        resource: Optional[BudgetResource] = None,
    ) -> None:
        """Persist a terminal reason found by the caller's local accounting.

        A caller-local counter must never be reported as a persisted budget
        exhaustion unless the persisted counter actually reached its limit.
        Time and cost reasons are independent gates and are always recorded.
        """
        with self._locked_scope() as session:
            run, config, usage = _resolve(session, self._run_id)
            if reason is BudgetStopReason.QUERY_BUDGET_EXHAUSTED:
                if usage.queries_reserved < config.max_oracle_queries:
                    return
            elif reason is BudgetStopReason.EVALUATION_LIMIT_REACHED:
                if usage.evaluations_reserved < config.max_candidate_evaluations:
                    return
            elif reason is BudgetStopReason.HYPOTHESIS_LIMIT_REACHED:
                if (
                    config.max_generated_hypotheses is None
                    or usage.hypotheses_generated < config.max_generated_hypotheses
                ):
                    return
            _exhaust(
                session,
                run,
                config,
                usage,
                reason,
                self._clock(),
                details={"resource": resource.value} if resource is not None else None,
            )


__all__ = [
    "RESERVATION_RESERVED",
    "RESERVATION_COMMITTED",
    "RESERVATION_RELEASED",
    "RESERVATION_AMBIGUOUS",
    "OUTCOME_COMPLETED",
    "OUTCOME_FAILED",
    "OUTCOME_RELEASED",
    "OUTCOME_AMBIGUOUS",
    "VALID_OUTCOMES",
    "TERMINAL_RUN_STATUSES",
    "DEFAULT_STALE_RESERVATION_SECONDS",
    "resolve_budget_config",
    "initialize_run_budget",
    "persist_exhaustion",
    "reserve_oracle_query",
    "record_oracle_query_outcome",
    "reserve_candidate_evaluations",
    "complete_candidate_evaluation",
    "release_candidate_evaluation",
    "reserve_hypotheses",
    "complete_hypothesis_generation",
    "record_compute_cost",
    "mark_cost_unavailable",
    "get_budget_status",
    "cancel_outstanding_run_tasks",
    "recover_stale_reservations",
    "detect_expired_runs",
    "RunBudgetManager",
]
