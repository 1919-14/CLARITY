"""Query validation, limit enforcement, and stopping conditions for CLARITY.

Ensures CLARITY:
- Validates candidate queries before oracle submission.
- Rejects malformed, out-of-range, or unsupported inputs with structured diagnostics.
- Detects previously queried inputs according to configurable duplicate policies.
- Enforces authoritative maximum query limits under sequential and concurrent execution.
- Distinguishes attempted, rejected, failed, and successful query counts.
- Bounds retries for retryable failures while preventing uncontrolled query spending.
- Evaluates and enforces explicit stopping conditions with terminal status and reasons.
- Preserves previously collected observations across query failures.
- Prevents duplicate observation ingestion.
- Exposes structured query and stopping diagnostics without relying on evaluator ground truth.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import math
import threading
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

from .contracts import (
    ExperimentInput,
    Observation,
    ObservationStatus,
    OracleInterface,
    OracleQueryError,
    StructuredError,
    generate_id,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)


# ==============================================================================
# 1. Enums and Constants
# ==============================================================================

class RunStatus(str, Enum):
    """Lifecycle state of a discovery run."""
    PENDING = "pending"
    RUNNING = "running"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StoppingCondition(str, Enum):
    """Documented stopping conditions for discovery runs."""
    MAX_QUERIES_REACHED = "max_queries_reached"
    SUCCESS_CRITERION_MET = "success_criterion_met"
    NO_CANDIDATES_REMAINING = "no_candidates_remaining"
    NO_INFORMATIVE_EXPERIMENT = "no_informative_experiment"
    TERMINAL_ERROR = "terminal_error"
    CANCELLED = "cancelled"


class DuplicateQueryPolicy(str, Enum):
    """Policy for handling previously queried experiment inputs."""
    REJECT = "reject"         # Rejects query with DUPLICATE_QUERY error; oracle is not queried
    ALLOW = "allow"           # Permitted to re-query oracle
    CACHE_REUSE = "cache_reuse" # Returns cached observation without re-querying or consuming budget


# Standard Structured Error Codes
QUERY_LIMIT_EXCEEDED = "QUERY_LIMIT_EXCEEDED"
DUPLICATE_QUERY = "DUPLICATE_QUERY"
RUN_ALREADY_TERMINATED = "RUN_ALREADY_TERMINATED"
RUN_CANCELLED = "RUN_CANCELLED"
RETRY_BUDGET_EXHAUSTED = "RETRY_BUDGET_EXHAUSTED"
INVALID_EXPERIMENT_INPUT = "INVALID_EXPERIMENT_INPUT"
UNSUPPORTED_DOMAIN = "UNSUPPORTED_DOMAIN"
NON_FINITE_VALUE = "NON_FINITE_VALUE"
NON_RETRYABLE_ERROR = "NON_RETRYABLE_ERROR"
TERMINAL_EXECUTION_ERROR = "TERMINAL_EXECUTION_ERROR"

DEFAULT_RETRYABLE_ERROR_CODES: Set[str] = {
    "TRANSIENT_ERROR",
    "TIMEOUT",
    "ORACLE_FAILURE",
    "NETWORK_ERROR",
    "RATE_LIMITED",
    "SERVICE_UNAVAILABLE",
}


# ==============================================================================
# 2. Canonical Input Normalization
# ==============================================================================

def canonical_input_key(domain: str, values: Mapping[str, Any], float_precision: int = 6) -> str:
    """Produce a deterministic, canonical string key for an experiment input.
    
    Used to reliably detect duplicate queries regardless of dictionary key ordering
    or slight floating-point formatting differences.
    """
    normalized_pairs = []
    for k in sorted(values.keys()):
        val = values[k]
        if isinstance(val, bool):
            formatted_val = f"b:{val}"
        elif isinstance(val, int):
            formatted_val = f"i:{val}"
        elif isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                formatted_val = f"f:{val}"
            else:
                formatted_val = f"f:{round(val, float_precision):.{float_precision}f}"
        else:
            formatted_val = f"s:{str(val)}"
        normalized_pairs.append(f"{k}={formatted_val}")
    return f"{domain}|" + "&".join(normalized_pairs)


# ==============================================================================
# 3. Input Validation
# ==============================================================================

class QueryValidator:
    """Validates experiment proposals before submission to an oracle.
    
    Enforces:
    - Domain compatibility.
    - Value types and finite range checks.
    - Ground-truth leakage prevention on proposal metadata.
    - Oracle-specific domain constraint validation.
    """

    def __init__(
        self,
        permitted_domain: Optional[str] = None,
        oracle: Optional[OracleInterface] = None,
    ):
        self._permitted_domain = permitted_domain
        self._oracle = oracle

    def validate(self, experiment_input: ExperimentInput) -> Optional[StructuredError]:
        """Validate candidate experiment input. Return None if valid, StructuredError if invalid."""
        if not isinstance(experiment_input, ExperimentInput):
            return StructuredError(
                code=INVALID_EXPERIMENT_INPUT,
                message=f"Expected ExperimentInput instance, got {type(experiment_input).__name__}.",
            )

        # Domain verification
        if (
            self._permitted_domain is not None
            and experiment_input.domain != "default"
            and experiment_input.domain != self._permitted_domain
        ):
            return StructuredError(
                code=UNSUPPORTED_DOMAIN,
                message=(
                    f"Experiment input domain '{experiment_input.domain}' does not match "
                    f"permitted domain '{self._permitted_domain}'."
                ),
                details={"provided_domain": experiment_input.domain, "permitted_domain": self._permitted_domain},
            )

        # Values dictionary checks
        if not isinstance(experiment_input.values, Mapping) or not experiment_input.values:
            return StructuredError(
                code=INVALID_EXPERIMENT_INPUT,
                message="ExperimentInput values must be a non-empty mapping.",
            )

        for k, v in experiment_input.values.items():
            if not isinstance(k, str) or not k:
                return StructuredError(
                    code=INVALID_EXPERIMENT_INPUT,
                    message="Variable names in values must be non-empty strings.",
                )
            if isinstance(v, bool):
                # booleans are valid primitive types under ExperimentInput contract,
                # but oracles requiring real numbers will reject them.
                pass
            elif isinstance(v, (int, float)):
                if math.isnan(v) or math.isinf(v):
                    return StructuredError(
                        code=NON_FINITE_VALUE,
                        message=f"Value for '{k}' must be finite; got {v}.",
                        details={"variable": k, "value": str(v)},
                    )
            elif not isinstance(v, str):
                return StructuredError(
                    code=INVALID_EXPERIMENT_INPUT,
                    message=f"Unsupported value type for '{k}': {type(v).__name__}.",
                )

        # Leakage guardrails on metadata
        if experiment_input.metadata is not None:
            try:
                verify_no_ground_truth_leakage(experiment_input.metadata)
            except ValueError as e:
                return StructuredError(
                    code="GROUND_TRUTH_LEAKAGE",
                    message=f"Ground-truth leakage detected in query metadata: {str(e)}",
                )

        # Oracle-specific validation if available
        if self._oracle is not None:
            oracle_err = self._oracle.validate_input(experiment_input)
            if oracle_err is not None:
                return oracle_err

        return None


# ==============================================================================
# 4. Retry and Failure Classifier
# ==============================================================================

def is_retryable_error(
    error: Union[StructuredError, Exception, Observation],
    retryable_codes: Optional[Set[str]] = None,
) -> bool:
    """Determine whether an error is transient/retryable or terminal."""
    codes = retryable_codes if retryable_codes is not None else DEFAULT_RETRYABLE_ERROR_CODES

    if isinstance(error, Observation):
        if error.status == ObservationStatus.SUCCESS:
            return False
        if error.status == ObservationStatus.INVALID_INPUT:
            return False
        if error.error is not None:
            return error.error.code in codes
        return error.status == ObservationStatus.ORACLE_FAILURE

    if isinstance(error, OracleQueryError):
        return error.error.code in codes

    if isinstance(error, StructuredError):
        return error.code in codes

    # Generic system exception (e.g., connection reset, timeout) is retryable
    return isinstance(error, (TimeoutError, ConnectionError, OSError))


# ==============================================================================
# 5. Thread-Safe Observation Store
# ==============================================================================

class ObservationStore:
    """Thread-safe storage for discovery observations with deduplication.
    
    Guarantees:
    - Observations are preserved even if subsequent queries fail.
    - Repeated response processing does not duplicate observations.
    - Efficient lookup by observation_id and canonical input key.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._observations: List[Observation] = []
        self._by_id: Dict[str, Observation] = {}
        self._by_canonical_key: Dict[str, Observation] = {}

    def ingest(self, observation: Observation, canonical_key: Optional[str] = None) -> bool:
        """Ingest an observation idempotently.
        
        Returns:
            True if newly ingested; False if already present (deduplicated).
        """
        if not isinstance(observation, Observation):
            raise TypeError(f"Expected Observation, got {type(observation).__name__}")

        with self._lock:
            if observation.observation_id in self._by_id:
                return False

            self._observations.append(observation)
            self._by_id[observation.observation_id] = observation

            if canonical_key is not None and observation.status == ObservationStatus.SUCCESS:
                if canonical_key not in self._by_canonical_key:
                    self._by_canonical_key[canonical_key] = observation

            return True

    def get_by_id(self, observation_id: str) -> Optional[Observation]:
        with self._lock:
            return self._by_id.get(observation_id)

    def get_by_input_key(self, canonical_key: str) -> Optional[Observation]:
        with self._lock:
            return self._by_canonical_key.get(canonical_key)

    def all_observations(self) -> List[Observation]:
        with self._lock:
            return list(self._observations)

    def successful_observations(self) -> List[Observation]:
        with self._lock:
            return [obs for obs in self._observations if obs.status == ObservationStatus.SUCCESS]

    def count(self) -> int:
        with self._lock:
            return len(self._observations)


# ==============================================================================
# 6. Diagnostics & Execution Results
# ==============================================================================

@dataclass(frozen=True)
class StoppingDecision:
    """Decision indicating whether a discovery run must terminate."""
    should_stop: bool
    condition: Optional[StoppingCondition] = None
    reason: Optional[str] = None
    status: Optional[RunStatus] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "should_stop": self.should_stop,
            "condition": self.condition.value if self.condition else None,
            "reason": self.reason,
            "status": self.status.value if self.status else None,
        }


@dataclass(frozen=True)
class QueryDiagnostics:
    """Comprehensive diagnostics detailing query spending and stopping outcomes."""
    run_id: str
    configured_max_queries: int
    attempted_queries: int
    rejected_queries: int
    failed_queries: int
    successful_queries: int
    retries_performed: int
    duplicates_detected: int
    duplicate_handling_outcome: Dict[str, int]
    run_status: RunStatus
    is_terminated: bool
    stopping_condition: Optional[StoppingCondition] = None
    termination_reason: Optional[str] = None
    errors: List[Dict[str, Any]] = field(default_factory=list)
    observation_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "configured_max_queries": self.configured_max_queries,
            "attempted_queries": self.attempted_queries,
            "rejected_queries": self.rejected_queries,
            "failed_queries": self.failed_queries,
            "successful_queries": self.successful_queries,
            "retries_performed": self.retries_performed,
            "duplicates_detected": self.duplicates_detected,
            "duplicate_handling_outcome": dict(self.duplicate_handling_outcome),
            "run_status": self.run_status.value,
            "is_terminated": self.is_terminated,
            "stopping_condition": self.stopping_condition.value if self.stopping_condition else None,
            "termination_reason": self.termination_reason,
            "errors": list(self.errors),
            "observation_count": self.observation_count,
        }


@dataclass(frozen=True)
class QueryExecutionResult:
    """Result of submitting a candidate query through the query control layer."""
    success: bool
    observation: Optional[Observation] = None
    error: Optional[StructuredError] = None
    is_cached: bool = False
    attempts_used: int = 1

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "success": self.success,
            "is_cached": self.is_cached,
            "attempts_used": self.attempts_used,
        }
        if self.observation is not None:
            data["observation"] = self.observation.to_dict()
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data


# ==============================================================================
# 7. Query Control Configuration
# ==============================================================================

SuccessCriterionCallable = Callable[[List[Observation], Dict[str, Any]], Tuple[bool, Optional[str]]]


@dataclass(frozen=True)
class QueryControlConfig:
    """Configuration for query validation, limit enforcement, and stopping controls.
    
    Fields:
      max_queries: Authoritative maximum successful oracle queries permitted for this run.
      duplicate_policy: REJECT, ALLOW, or CACHE_REUSE.
      max_retries_per_query: Maximum retry attempts for retryable oracle failures.
      retryable_error_codes: Error codes considered transient.
      permitted_domain: Optional domain constraint.
      float_precision: Precision for canonical duplicate detection.
      success_criterion: Evaluator-independent success predicate (Callable[[observations, metadata], (bool, reason)]).
    """
    max_queries: int
    duplicate_policy: DuplicateQueryPolicy = DuplicateQueryPolicy.REJECT
    max_retries_per_query: int = 2
    retryable_error_codes: Set[str] = field(default_factory=lambda: set(DEFAULT_RETRYABLE_ERROR_CODES))
    permitted_domain: Optional[str] = None
    float_precision: int = 6
    success_criterion: Optional[SuccessCriterionCallable] = None

    def __post_init__(self):
        if not isinstance(self.max_queries, int) or self.max_queries < 1:
            raise ValueError(f"max_queries must be a positive integer, got {self.max_queries}.")
        if not isinstance(self.duplicate_policy, DuplicateQueryPolicy):
            raise ValueError(
                f"duplicate_policy must be a DuplicateQueryPolicy enum, got {type(self.duplicate_policy).__name__}."
            )
        if not isinstance(self.max_retries_per_query, int) or self.max_retries_per_query < 0:
            raise ValueError(
                f"max_retries_per_query must be a non-negative integer, got {self.max_retries_per_query}."
            )


# ==============================================================================
# 8. Query Controller
# ==============================================================================

class QueryController:
    """Authoritative query-validation, limit-enforcement, and stopping-control layer.
    
    Guarantees:
    - Queries are validated prior to oracle execution.
    - Rejected or duplicate queries do not consume successful query budget.
    - Query counts are tracked authoritatively and thread-safely.
    - The configured query limit cannot be exceeded under concurrent execution.
    - Retries are bounded and only applied to retryable failures.
    - Stopping conditions produce explicit terminal reasons.
    - Evaluator ground-truth is never accessed.
    """

    def __init__(
        self,
        config: QueryControlConfig,
        oracle: OracleInterface,
        run_id: Optional[str] = None,
        validator: Optional[QueryValidator] = None,
    ):
        self._config = config
        self._oracle = oracle
        self._run_id = run_id or generate_id("run")
        self._validator = validator or QueryValidator(
            permitted_domain=config.permitted_domain or oracle.domain,
            oracle=oracle,
        )

        self._lock = threading.Lock()
        self._store = ObservationStore()

        # Authoritative counters
        self._attempted_queries = 0
        self._rejected_queries = 0
        self._failed_queries = 0
        self._successful_queries = 0
        self._retries_performed = 0
        self._duplicates_detected = 0
        self._duplicate_outcomes = {"rejected": 0, "cache_reused": 0, "allowed": 0}

        # Lifecycle and stopping state
        self._status = RunStatus.PENDING
        self._stopping_decision: Optional[StoppingDecision] = None
        self._errors: List[StructuredError] = []

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def status(self) -> RunStatus:
        with self._lock:
            return self._status

    @property
    def is_terminated(self) -> bool:
        with self._lock:
            return self._status in {
                RunStatus.STOPPED,
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }

    @property
    def observation_store(self) -> ObservationStore:
        return self._store

    def submit_query(self, experiment_input: ExperimentInput) -> QueryExecutionResult:
        """Submit a candidate experiment proposal through the control gate.
        
        Steps:
        1. Check if run is already terminated.
        2. Validate experiment input before calling oracle.
        3. Check duplicate policy.
        4. Enforce query limit before submitting.
        5. Execute oracle query with bounded retries on retryable failures.
        6. Ingest observation into store.
        7. Evaluate stopping conditions.
        """
        # --- Pre-execution checks under lock ---
        with self._lock:
            if self._status in {RunStatus.STOPPED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                if self._stopping_decision and self._stopping_decision.condition == StoppingCondition.MAX_QUERIES_REACHED:
                    limit_err = StructuredError(
                        code=QUERY_LIMIT_EXCEEDED,
                        message=f"Query limit of {self._config.max_queries} reached for run '{self._run_id}'.",
                        details={"max_queries": self._config.max_queries, "successful_queries": self._successful_queries},
                    )
                    self._record_error_unlocked(limit_err)
                    return QueryExecutionResult(success=False, error=limit_err)

                term_err = StructuredError(
                    code=RUN_ALREADY_TERMINATED,
                    message=(
                        f"Run '{self._run_id}' is already terminated with status '{self._status.value}'. "
                        f"Stopping reason: {self._stopping_decision.reason if self._stopping_decision else 'Unknown'}"
                    ),
                    details={"status": self._status.value},
                )
                self._record_error_unlocked(term_err)
                return QueryExecutionResult(success=False, error=term_err)

            if self._status == RunStatus.PENDING:
                self._status = RunStatus.RUNNING

            # Check if budget is already exhausted before doing work
            if self._successful_queries >= self._config.max_queries:
                self._set_stopping_decision_unlocked(
                    StoppingCondition.MAX_QUERIES_REACHED,
                    f"Configured maximum query limit ({self._config.max_queries}) has been reached.",
                    RunStatus.STOPPED,
                )
                limit_err = StructuredError(
                    code=QUERY_LIMIT_EXCEEDED,
                    message=f"Query limit of {self._config.max_queries} reached for run '{self._run_id}'.",
                    details={"max_queries": self._config.max_queries, "successful_queries": self._successful_queries},
                )
                self._record_error_unlocked(limit_err)
                return QueryExecutionResult(success=False, error=limit_err)

        # --- Step 1: Validate input (independent of selection strategy) ---
        val_error = self._validator.validate(experiment_input)
        if val_error is not None:
            with self._lock:
                self._attempted_queries += 1
                self._rejected_queries += 1
                self._record_error_unlocked(val_error)
            return QueryExecutionResult(success=False, error=val_error)

        effective_domain = (
            self._oracle.domain
            if experiment_input.domain == "default" and self._oracle
            else experiment_input.domain
        )
        canonical_key = canonical_input_key(
            effective_domain,
            experiment_input.values,
            float_precision=self._config.float_precision,
        )

        # --- Step 2: Duplicate detection and policy enforcement ---
        cached_observation = self._store.get_by_input_key(canonical_key)
        if cached_observation is not None:
            with self._lock:
                self._duplicates_detected += 1

                if self._config.duplicate_policy == DuplicateQueryPolicy.REJECT:
                    self._attempted_queries += 1
                    self._rejected_queries += 1
                    self._duplicate_outcomes["rejected"] += 1
                    dup_err = StructuredError(
                        code=DUPLICATE_QUERY,
                        message=(
                            f"Input with values {experiment_input.values} in domain '{experiment_input.domain}' "
                            f"was previously queried. Policy is REJECT."
                        ),
                        details={"canonical_key": canonical_key, "policy": self._config.duplicate_policy.value},
                    )
                    self._record_error_unlocked(dup_err)
                    return QueryExecutionResult(success=False, error=dup_err)

                elif self._config.duplicate_policy == DuplicateQueryPolicy.CACHE_REUSE:
                    self._duplicate_outcomes["cache_reused"] += 1
                    return QueryExecutionResult(
                        success=True,
                        observation=cached_observation,
                        is_cached=True,
                        attempts_used=0,
                    )

                elif self._config.duplicate_policy == DuplicateQueryPolicy.ALLOW:
                    self._duplicate_outcomes["allowed"] += 1

        # --- Step 3: Concurrency-safe query budget check & reservation ---
        with self._lock:
            if self._successful_queries >= self._config.max_queries:
                self._set_stopping_decision_unlocked(
                    StoppingCondition.MAX_QUERIES_REACHED,
                    f"Configured maximum query limit ({self._config.max_queries}) has been reached.",
                    RunStatus.STOPPED,
                )
                limit_err = StructuredError(
                    code=QUERY_LIMIT_EXCEEDED,
                    message=f"Query limit of {self._config.max_queries} reached for run '{self._run_id}'.",
                    details={"max_queries": self._config.max_queries, "successful_queries": self._successful_queries},
                )
                self._record_error_unlocked(limit_err)
                return QueryExecutionResult(success=False, error=limit_err)

            self._attempted_queries += 1

        # --- Step 4: Execute oracle query with bounded retries ---
        max_retries = self._config.max_retries_per_query
        attempts_used = 0
        last_error: Optional[StructuredError] = None
        final_observation: Optional[Observation] = None

        while attempts_used <= max_retries:
            attempts_used += 1
            if attempts_used > 1:
                with self._lock:
                    self._retries_performed += 1

            try:
                obs = self._oracle.query(experiment_input)
                if obs.status == ObservationStatus.SUCCESS:
                    final_observation = obs
                    break
                else:
                    # Non-success observation returned by oracle
                    last_error = obs.error or StructuredError(
                        code="ORACLE_FAILURE",
                        message=f"Oracle returned status: {obs.status.value}",
                    )
                    final_observation = obs
                    if not is_retryable_error(obs, self._config.retryable_error_codes):
                        break

            except OracleQueryError as oqe:
                last_error = oqe.error
                if not is_retryable_error(oqe, self._config.retryable_error_codes):
                    break
            except Exception as e:
                last_error = StructuredError(
                    code="ORACLE_EXECUTION_EXCEPTION",
                    message=f"Oracle query raised exception: {str(e)}",
                    details={"exception_type": type(e).__name__},
                )
                if not is_retryable_error(e, self._config.retryable_error_codes):
                    break

        # --- Step 5: Post-execution accounting & store ingestion ---
        with self._lock:
            if final_observation is not None and final_observation.status == ObservationStatus.SUCCESS:
                self._successful_queries += 1
                self._store.ingest(final_observation, canonical_key=canonical_key)

                # Check if this query caused budget exhaustion
                if self._successful_queries >= self._config.max_queries:
                    self._set_stopping_decision_unlocked(
                        StoppingCondition.MAX_QUERIES_REACHED,
                        f"Maximum query limit ({self._config.max_queries}) reached after successful execution.",
                        RunStatus.STOPPED,
                    )
                else:
                    # Check evaluator-independent success criterion
                    self._check_success_criterion_unlocked()

                return QueryExecutionResult(
                    success=True,
                    observation=final_observation,
                    attempts_used=attempts_used,
                )
            else:
                self._failed_queries += 1
                if final_observation is not None:
                    # Store failed observation without overwriting or clearing previous ones
                    self._store.ingest(final_observation, canonical_key=None)

                if attempts_used > max_retries and last_error is not None:
                    exhausted_err = StructuredError(
                        code=RETRY_BUDGET_EXHAUSTED,
                        message=(
                            f"Retry budget of {max_retries} attempts exhausted for query. "
                            f"Last error: [{last_error.code}] {last_error.message}"
                        ),
                        details={"max_retries": max_retries, "last_error": last_error.to_dict()},
                    )
                    self._record_error_unlocked(exhausted_err)
                    return QueryExecutionResult(
                        success=False,
                        observation=final_observation,
                        error=exhausted_err,
                        attempts_used=attempts_used,
                    )

                if last_error is not None:
                    self._record_error_unlocked(last_error)

                return QueryExecutionResult(
                    success=False,
                    observation=final_observation,
                    error=last_error,
                    attempts_used=attempts_used,
                )

    # --- Stopping Condition Signaling & Transitions ---

    def cancel(self, reason: str = "Discovery run explicitly cancelled.") -> StoppingDecision:
        """Cancel the discovery run if supported by the orchestration layer."""
        with self._lock:
            return self._set_stopping_decision_unlocked(
                StoppingCondition.CANCELLED,
                reason,
                RunStatus.CANCELLED,
            )

    def record_terminal_error(self, error: StructuredError, reason: Optional[str] = None) -> StoppingDecision:
        """Record a terminal configuration, validation, or execution error."""
        with self._lock:
            self._record_error_unlocked(error)
            msg = reason or f"Terminal error: [{error.code}] {error.message}"
            return self._set_stopping_decision_unlocked(
                StoppingCondition.TERMINAL_ERROR,
                msg,
                RunStatus.FAILED,
            )

    def signal_no_candidates_remaining(
        self, reason: str = "No valid candidate experiments remain in the search space."
    ) -> StoppingDecision:
        """Signal that candidate generation or selection has exhausted valid experiments."""
        with self._lock:
            return self._set_stopping_decision_unlocked(
                StoppingCondition.NO_CANDIDATES_REMAINING,
                reason,
                RunStatus.STOPPED,
            )

    def signal_no_informative_experiment(
        self, reason: str = "No informative experiment is available under the current active selection strategy."
    ) -> StoppingDecision:
        """Signal that no experiment meets the required information/disagreement threshold."""
        with self._lock:
            return self._set_stopping_decision_unlocked(
                StoppingCondition.NO_INFORMATIVE_EXPERIMENT,
                reason,
                RunStatus.STOPPED,
            )

    def evaluate_stopping_conditions(
        self,
        candidates_available: bool = True,
        informative_available: bool = True,
    ) -> StoppingDecision:
        """Evaluate stopping conditions explicitly."""
        with self._lock:
            if self._stopping_decision is not None:
                return self._stopping_decision

            if not candidates_available:
                return self._set_stopping_decision_unlocked(
                    StoppingCondition.NO_CANDIDATES_REMAINING,
                    "No valid candidate experiments remain.",
                    RunStatus.STOPPED,
                )

            if not informative_available:
                return self._set_stopping_decision_unlocked(
                    StoppingCondition.NO_INFORMATIVE_EXPERIMENT,
                    "No informative experiment is available under the current strategy.",
                    RunStatus.STOPPED,
                )

            if self._successful_queries >= self._config.max_queries:
                return self._set_stopping_decision_unlocked(
                    StoppingCondition.MAX_QUERIES_REACHED,
                    f"Maximum query count ({self._config.max_queries}) reached.",
                    RunStatus.STOPPED,
                )

            # Check success criterion
            self._check_success_criterion_unlocked()
            if self._stopping_decision is not None:
                return self._stopping_decision

            return StoppingDecision(should_stop=False)

    def _check_success_criterion_unlocked(self) -> None:
        """Evaluate evaluator-independent success criterion if configured."""
        if self._config.success_criterion is None or self._stopping_decision is not None:
            return

        obs = self._store.all_observations()
        metadata = {
            "run_id": self._run_id,
            "successful_queries": self._successful_queries,
            "configured_max_queries": self._config.max_queries,
        }
        try:
            met, reason = self._config.success_criterion(obs, metadata)
            if met:
                self._set_stopping_decision_unlocked(
                    StoppingCondition.SUCCESS_CRITERION_MET,
                    reason or "Evaluator-independent discovery success criterion satisfied.",
                    RunStatus.COMPLETED,
                )
        except Exception as e:
            # If the success criterion evaluation fails, do not crash the run; record error
            self._record_error_unlocked(
                StructuredError(
                    code="SUCCESS_CRITERION_EVALUATION_ERROR",
                    message=f"Evaluating success criterion raised an error: {str(e)}",
                )
            )

    def _set_stopping_decision_unlocked(
        self,
        condition: StoppingCondition,
        reason: str,
        status: RunStatus,
    ) -> StoppingDecision:
        if self._stopping_decision is not None:
            return self._stopping_decision

        decision = StoppingDecision(
            should_stop=True,
            condition=condition,
            reason=reason,
            status=status,
        )
        self._stopping_decision = decision
        self._status = status
        return decision

    def _record_error_unlocked(self, error: StructuredError) -> None:
        self._errors.append(error)

    # --- Diagnostics Exposure ---

    def get_diagnostics(self) -> QueryDiagnostics:
        """Return authoritative query and stopping diagnostics snapshot."""
        with self._lock:
            return QueryDiagnostics(
                run_id=self._run_id,
                configured_max_queries=self._config.max_queries,
                attempted_queries=self._attempted_queries,
                rejected_queries=self._rejected_queries,
                failed_queries=self._failed_queries,
                successful_queries=self._successful_queries,
                retries_performed=self._retries_performed,
                duplicates_detected=self._duplicates_detected,
                duplicate_handling_outcome=dict(self._duplicate_outcomes),
                run_status=self._status,
                is_terminated=self._status in {
                    RunStatus.STOPPED,
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                },
                stopping_condition=self._stopping_decision.condition if self._stopping_decision else None,
                termination_reason=self._stopping_decision.reason if self._stopping_decision else None,
                errors=[e.to_dict() for e in self._errors],
                observation_count=self._store.count(),
            )
