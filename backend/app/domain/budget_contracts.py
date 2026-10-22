"""Per-run resource budget contracts (Issue #14).

This module owns the *policy* side of budgeting: what a run may consume, how
zero/omitted values are interpreted, which stop reasons exist, and which cost
claims are honestly supportable. It deliberately contains no database code, so
the discovery engine and the persistence-backed budget service can share the
same types without a layering violation.

Accounting model
----------------
A configured limit and the usage observed against it are separate values. A
configured limit is immutable for the life of the run; usage counters are
monotonic (except that a never-dispatched reservation may be released).

Zero semantics (explicit, never "unlimited")
--------------------------------------------
``max_oracle_queries=0``             no oracle query may be admitted.
``max_candidate_evaluations=0``      no candidate evaluation may be admitted.
``max_concurrent_remote_tasks=0``    no remote task may be active.
``max_runtime_seconds=0``            no execution time is available; no work.
``max_generated_hypotheses=0``       no new hypothesis may be generated.
``max_compute_spend_usd=0``          no positive-cost work may be admitted.

An optional limit that is ``None`` means *disabled* (no application limit),
which is not the same as zero, unlimited, or unknown. Integer admission limits
are never ``None``: omitted values resolve to the documented defaults below so
that every run has an explicit, validated budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, runtime_checkable

# ==============================================================================
# 1. Documented defaults and hard bounds
# ==============================================================================

DEFAULT_MAX_ORACLE_QUERIES = 10
DEFAULT_MAX_CANDIDATE_EVALUATIONS = 100
DEFAULT_MAX_CONCURRENT_REMOTE_TASKS = 4
DEFAULT_MAX_RUNTIME_SECONDS = 3600
DEFAULT_MAX_GENERATED_HYPOTHESES: Optional[int] = None  # disabled
DEFAULT_MAX_COMPUTE_SPEND_USD: Optional[float] = None  # disabled

# Implementation bounds (not provider guarantees). Values outside these are
# rejected rather than silently clamped.
MAX_ORACLE_QUERIES_LIMIT = 100_000
MAX_CANDIDATE_EVALUATIONS_LIMIT = 1_000_000
MAX_CONCURRENT_REMOTE_TASKS_LIMIT = 32
MAX_RUNTIME_SECONDS_LIMIT = 604_800  # 7 days
MAX_GENERATED_HYPOTHESES_LIMIT = 10_000
MAX_COMPUTE_SPEND_USD_LIMIT = 10_000.0

BUDGET_CONFIG_SCHEMA_VERSION = "1.0"

CONFIG_FIELDS = (
    "max_oracle_queries",
    "max_candidate_evaluations",
    "max_concurrent_remote_tasks",
    "max_runtime_seconds",
    "max_generated_hypotheses",
    "max_compute_spend_usd",
)

# Legacy v1 API names accepted at the HTTP boundary only. They map onto the
# canonical fields above; the canonical names are what is persisted and served.
LEGACY_FIELD_ALIASES = {
    "max_queries": "max_oracle_queries",
    "max_evaluations": "max_candidate_evaluations",
    "max_runtime_seconds": "max_runtime_seconds",
    "spend_ceiling_usd": "max_compute_spend_usd",
    "max_concurrent_remote_tasks": "max_concurrent_remote_tasks",
    "max_generated_hypotheses": "max_generated_hypotheses",
}

ZERO_SEMANTICS: Dict[str, str] = {
    "max_oracle_queries": "Zero permits no oracle query; negative values are rejected.",
    "max_candidate_evaluations": "Zero permits no candidate evaluation; negative values are rejected.",
    "max_concurrent_remote_tasks": "Zero permits no active remote task; negative values are rejected.",
    "max_runtime_seconds": "Zero means no execution time is available, so no work may start.",
    "max_generated_hypotheses": "Zero permits no new hypothesis; null disables this limit.",
    "max_compute_spend_usd": (
        "Zero permits no work with a positive estimated cost; null disables the application "
        "estimate gate. This is never a provider-enforced spending cap."
    ),
}


# ==============================================================================
# 2. Resources, stop reasons, cost states
# ==============================================================================

class BudgetResource(str, Enum):
    """Resource categories admitted by the budget manager."""

    ORACLE_QUERY = "oracle_query"
    CANDIDATE_EVALUATION = "candidate_evaluation"
    REMOTE_TASK = "remote_task"
    HYPOTHESIS = "hypothesis"


class BudgetStopReason(str, Enum):
    """Structured reasons a run stopped consuming resources.

    These are machine-readable and stored on the run as ``stop_reason``.
    ``CONCURRENCY_LIMIT_REACHED`` is *backpressure*, not a terminal exhaustion
    reason: an occupied slot delays scheduling and must not end a run.
    """

    QUERY_BUDGET_EXHAUSTED = "QUERY_BUDGET_EXHAUSTED"
    COMPUTE_BUDGET_EXHAUSTED = "COMPUTE_BUDGET_EXHAUSTED"
    TIME_LIMIT_EXCEEDED = "TIME_LIMIT_EXCEEDED"
    EVALUATION_LIMIT_REACHED = "EVALUATION_LIMIT_REACHED"
    HYPOTHESIS_LIMIT_REACHED = "HYPOTHESIS_LIMIT_REACHED"
    CONCURRENCY_LIMIT_REACHED = "CONCURRENCY_LIMIT_REACHED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        """Whether the reason ends the run instead of applying backpressure."""
        return self is not BudgetStopReason.CONCURRENCY_LIMIT_REACHED

    @property
    def run_status(self) -> str:
        """Existing run lifecycle status for this reason."""
        if self is BudgetStopReason.CANCELLED:
            return "cancelled"
        if self is BudgetStopReason.FAILED:
            return "failed"
        if self is BudgetStopReason.CONCURRENCY_LIMIT_REACHED:
            return "running"
        return "budget_exhausted"

    @classmethod
    def from_value(cls, value: Any) -> Optional["BudgetStopReason"]:
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value))
        except ValueError:
            # Legacy human-readable stop reasons are preserved verbatim and are
            # not reinterpreted as budget reasons.
            return None


class CostDataState(str, Enum):
    """Availability and settlement state of monetary data.

    ``UNAVAILABLE``/``PENDING_SETTLEMENT`` are never serialized as ``0``.
    """

    NOT_STARTED = "not_started"
    ESTIMATED_ONLY = "estimated_only"
    PENDING_SETTLEMENT = "pending_settlement"
    SETTLED = "settled"
    UNAVAILABLE = "unavailable"


class CostEnforcement(str, Enum):
    """How strongly a compute-spend limit is enforced."""

    #: No provider charge is expected (local/mock execution).
    NOT_APPLICABLE = "not_applicable"
    #: Application-side estimate only; stops future scheduling, not running work.
    APPLICATION_ESTIMATE = "application_estimate"
    #: No reliable cost data exists, so the control cannot be offered.
    UNSUPPORTED = "unsupported"
    #: A verified provider-side hard cap exists. Not implemented for Nodus 1.0.27.
    PROVIDER_HARD_CAP = "provider_hard_cap"


class CostEstimateBasis(str, Enum):
    """Where an application-side estimate comes from."""

    NOT_CONFIGURED = "not_configured"
    ADAPTER_REPORTED = "adapter_reported"
    CONSERVATIVE_UNKNOWN = "conservative_unknown"


# ==============================================================================
# 3. Errors
# ==============================================================================

class BudgetValidationError(ValueError):
    """Raised when a budget payload/config cannot be admitted.

    ``details`` is a JSON-serializable list of per-field problems, suitable for
    the API's documented error envelope.
    """

    def __init__(self, message: str, details: Optional[Sequence[Mapping[str, Any]]] = None):
        super().__init__(message)
        self.message = message
        self.details = list(details or [])

    def to_dict(self) -> Dict[str, Any]:
        return {"code": "INVALID_BUDGET", "message": self.message, "fields": self.details}


class BudgetAdmissionDenied(Exception):
    """Raised when a reservation cannot be granted.

    A terminal denial ends the run (query/evaluation/hypothesis/runtime/spend
    exhaustion). The exception never carries credentials or oracle internals.
    """

    def __init__(
        self,
        reason: BudgetStopReason,
        resource: BudgetResource,
        message: str,
        *,
        limit: Optional[int] = None,
        used: Optional[int] = None,
    ):
        self.reason = reason
        self.resource = resource
        self.message = message
        self.limit = limit
        self.used = used
        super().__init__(f"[{reason.value}] {message}")

    @property
    def is_terminal(self) -> bool:
        return self.reason.is_terminal

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.reason.value,
            "reason": self.reason.value,
            "resource": self.resource.value,
            "message": self.message,
            "limit": self.limit,
            "used": self.used,
            "terminal": self.is_terminal,
        }


class BudgetBackpressure(Exception):
    """Raised when a concurrency slot is temporarily unavailable."""

    def __init__(self, message: str, *, active: int, limit: int):
        self.message = message
        self.active = active
        self.limit = limit
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": BudgetStopReason.CONCURRENCY_LIMIT_REACHED.value,
            "message": self.message,
            "active": self.active,
            "limit": self.limit,
            "terminal": False,
        }


class BudgetNotFoundError(LookupError):
    """Raised when a run has no budget record and cannot be resolved."""


# ==============================================================================
# 4. Budget configuration
# ==============================================================================

def _require_int(
    field: str, value: Any, *, minimum: int, maximum: int, errors: list
) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(
            {
                "field": field,
                "code": "INVALID_TYPE",
                "message": f"'{field}' must be an integer.",
                "received_type": type(value).__name__,
            }
        )
        return None
    if value < minimum:
        errors.append(
            {
                "field": field,
                "code": "OUT_OF_RANGE",
                "message": f"'{field}' must be >= {minimum}.",
                "received": value,
            }
        )
        return None
    if value > maximum:
        errors.append(
            {
                "field": field,
                "code": "OUT_OF_RANGE",
                "message": f"'{field}' must be <= {maximum}.",
                "received": value,
            }
        )
        return None
    return value


def _require_optional_int(
    field: str, value: Any, *, minimum: int, maximum: int, errors: list
) -> Optional[int]:
    if value is None:
        return None
    return _require_int(field, value, minimum=minimum, maximum=maximum, errors=errors)


def _require_optional_money(
    field: str, value: Any, *, maximum: float, errors: list
) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(
            {
                "field": field,
                "code": "INVALID_TYPE",
                "message": f"'{field}' must be a finite number or null.",
                "received_type": type(value).__name__,
            }
        )
        return None
    as_float = float(value)
    if not math.isfinite(as_float):
        errors.append(
            {
                "field": field,
                "code": "NOT_FINITE",
                "message": f"'{field}' must be a finite number or null.",
                "received": str(value),
            }
        )
        return None
    if as_float < 0.0:
        errors.append(
            {
                "field": field,
                "code": "OUT_OF_RANGE",
                "message": f"'{field}' must be >= 0.",
                "received": as_float,
            }
        )
        return None
    if as_float > maximum:
        errors.append(
            {
                "field": field,
                "code": "OUT_OF_RANGE",
                "message": f"'{field}' must be <= {maximum}.",
                "received": as_float,
            }
        )
        return None
    return as_float


@dataclass(frozen=True)
class RunBudgetConfig:
    """Validated, immutable resource budget for one discovery run.

    Integer admission limits always have a concrete value. Optional limits use
    ``None`` for *disabled*; zero always means "no work of this kind".
    """

    max_oracle_queries: int = DEFAULT_MAX_ORACLE_QUERIES
    max_candidate_evaluations: int = DEFAULT_MAX_CANDIDATE_EVALUATIONS
    max_concurrent_remote_tasks: int = DEFAULT_MAX_CONCURRENT_REMOTE_TASKS
    max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS
    max_generated_hypotheses: Optional[int] = DEFAULT_MAX_GENERATED_HYPOTHESES
    max_compute_spend_usd: Optional[float] = DEFAULT_MAX_COMPUTE_SPEND_USD

    def __post_init__(self) -> None:
        errors: list[Dict[str, Any]] = []
        for field, minimum, maximum in (
            ("max_oracle_queries", 0, MAX_ORACLE_QUERIES_LIMIT),
            ("max_candidate_evaluations", 0, MAX_CANDIDATE_EVALUATIONS_LIMIT),
            ("max_concurrent_remote_tasks", 0, MAX_CONCURRENT_REMOTE_TASKS_LIMIT),
            ("max_runtime_seconds", 0, MAX_RUNTIME_SECONDS_LIMIT),
        ):
            _require_int(
                field,
                getattr(self, field),
                minimum=minimum,
                maximum=maximum,
                errors=errors,
            )
        _require_optional_int(
            "max_generated_hypotheses",
            self.max_generated_hypotheses,
            minimum=0,
            maximum=MAX_GENERATED_HYPOTHESES_LIMIT,
            errors=errors,
        )
        _require_optional_money(
            "max_compute_spend_usd",
            self.max_compute_spend_usd,
            maximum=MAX_COMPUTE_SPEND_USD_LIMIT,
            errors=errors,
        )
        if errors:
            raise BudgetValidationError("Invalid run budget configuration.", errors)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_mapping(
        cls,
        payload: Optional[Mapping[str, Any]],
        *,
        allow_legacy_aliases: bool = False,
    ) -> "RunBudgetConfig":
        """Validate a budget mapping.

        Unknown fields are rejected so a typo cannot silently disable a limit.
        Legacy flat API names are accepted only when explicitly requested.
        """
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise BudgetValidationError(
                "Invalid run budget configuration.",
                [
                    {
                        "field": "budget",
                        "code": "INVALID_TYPE",
                        "message": "Budget must be an object.",
                        "received_type": type(payload).__name__,
                    }
                ],
            )

        allowed = set(CONFIG_FIELDS)
        aliases = LEGACY_FIELD_ALIASES if allow_legacy_aliases else {}
        resolved: Dict[str, Any] = {}
        errors: list[Dict[str, Any]] = []

        for raw_key, value in payload.items():
            if not isinstance(raw_key, str):
                errors.append(
                    {
                        "field": "budget",
                        "code": "INVALID_FIELD",
                        "message": "Budget field names must be strings.",
                    }
                )
                continue
            if raw_key in allowed:
                key = raw_key
            elif raw_key in aliases:
                key = aliases[raw_key]
            else:
                errors.append(
                    {
                        "field": raw_key,
                        "code": "UNKNOWN_FIELD",
                        "message": f"'{raw_key}' is not a supported budget field.",
                        "supported_fields": list(CONFIG_FIELDS),
                    }
                )
                continue
            if key in resolved:
                errors.append(
                    {
                        "field": key,
                        "code": "CONFLICTING_SETTING",
                        "message": (
                            f"'{key}' was provided more than once (for example through both "
                            "a canonical and a legacy name)."
                        ),
                    }
                )
                continue
            resolved[key] = value

        defaults = cls()
        values: Dict[str, Any] = {}
        for field in CONFIG_FIELDS:
            values[field] = resolved.get(field, getattr(defaults, field))

        if errors:
            raise BudgetValidationError("Invalid run budget configuration.", errors)
        return cls(**values)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Resolution and reporting
    # ------------------------------------------------------------------

    def limit_for(self, resource: BudgetResource) -> Optional[int]:
        """Return the configured admission limit for *resource*.

        ``None`` means the limit is disabled for this resource.
        """
        return {
            BudgetResource.ORACLE_QUERY: self.max_oracle_queries,
            BudgetResource.CANDIDATE_EVALUATION: self.max_candidate_evaluations,
            BudgetResource.REMOTE_TASK: self.max_concurrent_remote_tasks,
            BudgetResource.HYPOTHESIS: self.max_generated_hypotheses,
        }[resource]

    def cost_enforcement(self, execution_mode: str) -> CostEnforcement:
        """Describe what the compute-spend limit can actually guarantee."""
        if self.max_compute_spend_usd is None:
            return CostEnforcement.NOT_APPLICABLE
        if execution_mode in {"local", "mock"}:
            # Local execution incurs no provider charge; the configured value is
            # still surfaced but cannot gate a real provider bill.
            return CostEnforcement.NOT_APPLICABLE
        return CostEnforcement.APPLICATION_ESTIMATE

    @property
    def permits_any_work(self) -> bool:
        """Whether the budget permits any resource-consuming work at all."""
        if self.max_runtime_seconds == 0:
            return False
        return any(
            (
                self.max_oracle_queries > 0,
                self.max_candidate_evaluations > 0,
                self.max_concurrent_remote_tasks > 0,
                bool(self.max_generated_hypotheses),
            )
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": BUDGET_CONFIG_SCHEMA_VERSION,
            "max_oracle_queries": self.max_oracle_queries,
            "max_candidate_evaluations": self.max_candidate_evaluations,
            "max_concurrent_remote_tasks": self.max_concurrent_remote_tasks,
            "max_runtime_seconds": self.max_runtime_seconds,
            "max_generated_hypotheses": self.max_generated_hypotheses,
            "max_compute_spend_usd": self.max_compute_spend_usd,
        }

    @property
    def is_default(self) -> bool:
        return self == RunBudgetConfig()


def is_budget_payload(payload: Any) -> bool:
    """Whether *payload* is usable as a canonical or legacy budget mapping."""
    return payload is None or isinstance(payload, Mapping)


# ==============================================================================
# 5. Summaries
# ==============================================================================

@dataclass(frozen=True)
class BudgetLimitStatus:
    """Admission status for one integer resource."""

    resource: BudgetResource
    limit: Optional[int]
    used: int
    remaining: Optional[int]
    exhausted: bool

    @property
    def is_disabled(self) -> bool:
        return self.limit is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "exhausted": self.exhausted,
            "enforcement": "application",
            "disabled": self.is_disabled,
        }


@dataclass(frozen=True)
class BudgetStatus:
    """Consistent, API-safe budget summary for one run."""

    run_id: str
    config: RunBudgetConfig
    execution_mode: str
    status: str
    oracle_queries: BudgetLimitStatus
    candidate_evaluations: BudgetLimitStatus
    generated_hypotheses: BudgetLimitStatus
    query_counts: Mapping[str, int]
    evaluation_counts: Mapping[str, int]
    active_remote_tasks: int
    max_concurrent_remote_tasks: int
    run_started_at: Optional[str]
    deadline_at: Optional[str]
    elapsed_seconds: Optional[float]
    remaining_seconds: Optional[float]
    estimated_cost_usd: Optional[float]
    accruing_cost_usd: Optional[float]
    settled_cost_usd: Optional[float]
    cost_data_state: CostDataState
    cost_enforcement: CostEnforcement
    provider_hard_cap_supported: bool
    is_exhausted: bool
    stop_reason: Optional[BudgetStopReason]
    raw_stop_reason: Optional[str]
    zero_semantics: Mapping[str, str]
    limitations: Sequence[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "schema_version": BUDGET_CONFIG_SCHEMA_VERSION,
            "execution_mode": self.execution_mode,
            "status": self.status,
            "limits": self.config.to_dict(),
            "usage": {
                "oracle_queries": self.oracle_queries.to_dict(),
                "candidate_evaluations": self.candidate_evaluations.to_dict(),
                "generated_hypotheses": self.generated_hypotheses.to_dict(),
                "query_outcomes": {
                    "attempted": self.oracle_queries.used,
                    **dict(self.query_counts),
                },
                "evaluation_outcomes": {
                    "attempted": self.candidate_evaluations.used,
                    **dict(self.evaluation_counts),
                },
                "active_remote_tasks": self.active_remote_tasks,
                "max_concurrent_remote_tasks": self.max_concurrent_remote_tasks,
            },
            "runtime": {
                "started_at": self.run_started_at,
                "deadline_at": self.deadline_at,
                "elapsed_seconds": self.elapsed_seconds,
                "remaining_seconds": self.remaining_seconds,
            },
            "cost": {
                "estimated_cost_usd": self.estimated_cost_usd,
                "accruing_cost_usd": self.accruing_cost_usd,
                "settled_cost_usd": self.settled_cost_usd,
                "data_state": self.cost_data_state.value,
                "enforcement": self.cost_enforcement.value,
                "provider_hard_cap_supported": self.provider_hard_cap_supported,
            },
            "is_exhausted": self.is_exhausted,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "raw_stop_reason": self.raw_stop_reason,
            "zero_semantics": dict(self.zero_semantics),
            "limitations": list(self.limitations),
        }


# ==============================================================================
# 6. Ports implemented by the persistence-backed budget service
# ==============================================================================

@runtime_checkable
class RunBudgetPort(Protocol):
    """Budget admission boundary consumed by discovery execution code."""

    def ensure_work_allowed(self, operation_id: str) -> None:
        """Raise ``BudgetAdmissionDenied`` when no further work may be admitted."""

    def reserve_oracle_query(self, operation_id: str) -> str:
        """Atomically reserve one oracle invocation; return a reservation token."""

    def record_oracle_query_outcome(self, reservation_id: str, outcome: str) -> None:
        """Record ``completed``, ``failed``, or ``released`` for a reservation."""

    def reserve_candidate_evaluations(self, count: int, operation_id: str) -> str:
        """Atomically reserve *count* candidate evaluations."""

    def complete_candidate_evaluation(self, reservation_id: str, succeeded: bool) -> None:
        """Record a terminal evaluation outcome."""

    def reserve_hypotheses(self, count: int, operation_id: str) -> str:
        """Atomically reserve *count* new hypothesis slots."""

    def mark_exhausted(
        self,
        reason: "BudgetStopReason",
        resource: Optional["BudgetResource"] = None,
    ) -> None:
        """Persist a terminal exhaustion reason discovered by local accounting."""


@runtime_checkable
class ParallelBudgetPort(Protocol):
    """Budget admission boundary consumed by parallel evaluation."""

    def reserve_candidate_evaluations(self, count: int, operation_id: str) -> str: ...

    def complete_candidate_evaluation(self, reservation_id: str, succeeded: bool) -> None: ...

    def release_candidate_evaluations(self, reservation_id: str) -> None: ...

    def try_acquire_remote_slots(self, count: int, operation_id: str) -> Optional[Sequence[str]]:
        """Acquire all *count* slots or none; return tokens, or None for backpressure."""

    def release_remote_slots(self, tokens: Sequence[str]) -> None: ...


__all__ = [
    "DEFAULT_MAX_ORACLE_QUERIES",
    "DEFAULT_MAX_CANDIDATE_EVALUATIONS",
    "DEFAULT_MAX_CONCURRENT_REMOTE_TASKS",
    "DEFAULT_MAX_RUNTIME_SECONDS",
    "DEFAULT_MAX_GENERATED_HYPOTHESES",
    "DEFAULT_MAX_COMPUTE_SPEND_USD",
    "MAX_ORACLE_QUERIES_LIMIT",
    "MAX_CANDIDATE_EVALUATIONS_LIMIT",
    "MAX_CONCURRENT_REMOTE_TASKS_LIMIT",
    "MAX_RUNTIME_SECONDS_LIMIT",
    "MAX_GENERATED_HYPOTHESES_LIMIT",
    "MAX_COMPUTE_SPEND_USD_LIMIT",
    "BUDGET_CONFIG_SCHEMA_VERSION",
    "CONFIG_FIELDS",
    "LEGACY_FIELD_ALIASES",
    "ZERO_SEMANTICS",
    "BudgetResource",
    "BudgetStopReason",
    "CostDataState",
    "CostEnforcement",
    "CostEstimateBasis",
    "BudgetValidationError",
    "BudgetAdmissionDenied",
    "BudgetBackpressure",
    "BudgetNotFoundError",
    "RunBudgetConfig",
    "BudgetLimitStatus",
    "BudgetStatus",
    "RunBudgetPort",
    "ParallelBudgetPort",
    "is_budget_payload",
]
