"""Contracts for the end-to-end black-box discovery loop (Issue #8).

Defines the orchestration-level data types only: run configuration, lifecycle
status, termination reasons, query accounting, experiment/iteration history,
the planner boundary, and the immutable run result.

Responsibilities split
----------------------
The discovery engine coordinates existing scientific components:

- Issue #5 ``CandidateGenerator`` proposes candidates.
- Issue #6 ``EvaluationPipeline`` fits and evaluates them.
- Issue #7 ``HypothesisRanker`` ranks them and derives evidence summaries.
- Issue #9 ``CandidateExperimentGenerator`` proposes valid experiment inputs.
- Issue #4 ``OracleInterface`` executes queries.

Nothing here reimplements those algorithms, and nothing here may carry
evaluator-only ground truth. The oracle is reachable only through its public
``validate_input``/``query`` methods.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional, Protocol, Tuple

from .contracts import (
    EvaluationResult,
    HypothesisRepresentation,
    Observation,
    StructuredError,
    freeze_payload,
    thaw_payload,
    utc_now_iso,
    verify_no_ground_truth_leakage,
)
from .experiment_generator import (
    CandidateExperiment,
    CandidateExperimentBatch,
    CandidateExperimentStatus,
    ExperimentGenerationStrategy,
    NumericalDomainBounds,
)
from .fitting import FittingConfig
from .candidate_generator import HypothesisGenerationConfig
from .ranking_contracts import RankingPolicyConfig, RankingResult

# ==============================================================================
# 1. Error Codes
# ==============================================================================

ERROR_INVALID_DISCOVERY_CONFIG = "INVALID_DISCOVERY_CONFIG"
ERROR_INVALID_INITIAL_OBSERVATION = "INVALID_INITIAL_OBSERVATION"
ERROR_INVALID_OBSERVATION = "INVALID_OBSERVATION"
ERROR_DUPLICATE_OBSERVATION_ID = "DUPLICATE_OBSERVATION_ID"
ERROR_DUPLICATE_OBSERVATION_INPUT = "DUPLICATE_OBSERVATION_INPUT"
ERROR_RUN_NOT_ACTIVE = "RUN_NOT_ACTIVE"
ERROR_RUN_NOT_FINISHED = "RUN_NOT_FINISHED"
ERROR_RUN_TERMINAL_FAILURE = "RUN_TERMINAL_FAILURE"
ERROR_CANDIDATE_GENERATION_FAILURE = "CANDIDATE_GENERATION_FAILURE"
ERROR_EVALUATION_FAILURE = "EVALUATION_FAILURE"
ERROR_RANKING_FAILURE = "RANKING_FAILURE"
ERROR_EXPERIMENT_GENERATION_FAILURE = "EXPERIMENT_GENERATION_FAILURE"
ERROR_PLANNER_FAILURE = "PLANNER_FAILURE"
ERROR_INVALID_PLANNER_SELECTION = "INVALID_PLANNER_SELECTION"
ERROR_DUPLICATE_QUERY = "DUPLICATE_QUERY"
ERROR_ORACLE_QUERY_FAILURE = "ORACLE_QUERY_FAILURE"
ERROR_INVALID_ORACLE_RESPONSE = "INVALID_ORACLE_RESPONSE"
ERROR_UNEXPECTED_DISCOVERY_ERROR = "UNEXPECTED_DISCOVERY_ERROR"

WARNING_EXPERIMENT_POOL_UNDERSIZED = "EXPERIMENT_POOL_UNDERSIZED"
WARNING_DUPLICATE_QUERY_SKIPPED = "DUPLICATE_QUERY_SKIPPED"
WARNING_RANKING_STALE = "RANKING_STALE"

DISCOVERY_ENGINE_VERSION = "1.0.0"


class DiscoveryRunError(Exception):
    """Raised when a discovery run cannot be initialized from its inputs."""

    def __init__(self, error: StructuredError):
        super().__init__(f"[{error.code}] {error.message}")
        self.error = error


# ==============================================================================
# 2. Lifecycle Status and Termination
# ==============================================================================

class DiscoveryLifecycleStatus(str, Enum):
    """Terminal or current state of one discovery run.

    Every terminal state carries its own explicit reason, so no generic
    "completed" state exists: a run that stops is either a configured
    termination or a failure.
    """

    INITIALIZING = "initializing"
    RUNNING = "running"
    TERMINATED_STOPPING_CRITERION = "terminated_stopping_criterion"
    TERMINATED_BUDGET_EXHAUSTED = "terminated_budget_exhausted"
    TERMINATED_MAX_ITERATIONS = "terminated_max_iterations"
    TERMINATED_NO_EXPERIMENTS = "terminated_no_experiments"
    FAILED = "failed"


class DiscoveryTerminationReason(str, Enum):
    """Machine-readable explanation of why a run stopped."""

    STOPPING_CRITERION_PLAUSIBILITY_REACHED = "stopping_criterion_plausibility_reached"
    STOPPING_CRITERION_METRIC_THRESHOLD_REACHED = "stopping_criterion_metric_threshold_reached"
    BUDGET_EXHAUSTED = "budget_exhausted"
    MAX_ITERATIONS_REACHED = "max_iterations_reached"
    EXPERIMENT_POOL_EXHAUSTED = "experiment_pool_exhausted"
    CONSECUTIVE_ORACLE_FAILURES = "consecutive_oracle_failures"
    COMPONENT_FAILURE = "component_failure"
    UNRECOVERABLE_ERROR = "unrecoverable_error"


# ==============================================================================
# 3. Configuration
# ==============================================================================

@dataclass(frozen=True)
class StoppingCriteriaConfig:
    """Configured, explicit discovery stopping criteria.

    Both criteria are optional and never inferred. In particular, a candidate
    ranking first is NOT success, and an unconfigured performance threshold can
    never terminate a run as a success.

    Fields:
      target_plausible_candidate_count: Stop once at least this many candidates
        hold ``PlausibilityStatus.PLAUSIBLE`` under the ranking policy (which
        itself requires an explicit performance acceptance threshold).
      target_primary_metric_threshold: Stop once the best *scientifically
        accepted* candidate's primary metric (held-out preferred, training
        fallback) satisfies this bound. Acceptance means
        ``PlausibilityStatus.PLAUSIBLE`` in a rankable comparability tier,
        which itself requires the ranking policy's explicit
        ``performance_acceptance_threshold``. This threshold can tighten
        acceptance but can never replace it: without a configured policy
        acceptance threshold the criterion is never satisfied.
      consecutive_failure_limit: Consecutive failed oracle queries tolerated
        before the run terminates as a failure. Must be >= 1.
    """

    target_plausible_candidate_count: Optional[int] = None
    target_primary_metric_threshold: Optional[float] = None
    consecutive_failure_limit: int = 3

    def __post_init__(self) -> None:
        if self.target_plausible_candidate_count is not None:
            value = self.target_plausible_candidate_count
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    "target_plausible_candidate_count must be an integer >= 1 or None."
                )
        if self.target_primary_metric_threshold is not None:
            value = self.target_primary_metric_threshold
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("target_primary_metric_threshold must be a finite number or None.")
            if not math.isfinite(float(value)):
                raise ValueError("target_primary_metric_threshold must be finite.")
        if isinstance(self.consecutive_failure_limit, bool) or not isinstance(
            self.consecutive_failure_limit, int
        ):
            raise ValueError("consecutive_failure_limit must be an integer >= 1.")
        if self.consecutive_failure_limit < 1:
            raise ValueError("consecutive_failure_limit must be an integer >= 1.")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "target_plausible_candidate_count": self.target_plausible_candidate_count,
            "target_primary_metric_threshold": self.target_primary_metric_threshold,
            "consecutive_failure_limit": self.consecutive_failure_limit,
        }


@dataclass(frozen=True)
class DiscoveryRunConfig:
    """Validated, reproducible configuration for one discovery run.

    Fields:
      max_query_budget: Hard upper bound on oracle invocations. Every attempted
        invocation (successful or not) consumes one unit. Zero is valid and
        means "evaluate initial observations only".
      max_iterations: Maximum outer orchestration cycles. Each executed cycle
        performs at most one oracle query. Must be >= 1.
      stopping_criteria: Explicit scientific stopping configuration.
      domain_bounds: Bounds of the numeric input variable searched by the
        experiment generator.
      hypothesis_config: Candidate generation settings (Issue #5), including
        the candidate-generation seed.
      fitting_config: Fitting settings (Issue #6).
      ranking_policy: Ranking and evidence policy (Issue #7). PLAUSIBLE
        requires an explicit ``performance_acceptance_threshold`` here.
      experiment_pool_size: Number of candidate experiments requested per
        iteration (Issue #9 bound: 1..500).
      experiment_strategy: Experiment generation strategy.
      allow_duplicate_queries: When False (default), an experiment whose input
        matches an already-committed observation is never sent to the oracle.
      allow_duplicate_external_observations: When False (default), an
        externally supplied observation whose input matches an already
        committed observation is rejected. This is deliberately independent of
        ``allow_duplicate_queries``: re-querying the oracle and accepting a
        repeated external measurement are different scientific decisions.
      query_exclusion_tolerance: Pure absolute tolerance used for duplicate
        detection (``rel_tol=0.0``).
      experiment_seed: Seed forwarded to stochastic experiment strategies. The
        grid strategy is deterministic and ignores it.
      dataset_id: Optional stable dataset identifier used for evaluation and
        ranking comparability. Defaults to the run ID when omitted.
      max_experiment_candidates_per_iteration: Maximum candidates from one
        generated batch that may be tried (validation/deduplication rejects
        them) before the run terminates as "no valid experiment".
    """

    max_query_budget: int = 10
    max_iterations: int = 10
    stopping_criteria: StoppingCriteriaConfig = field(default_factory=StoppingCriteriaConfig)
    domain_bounds: NumericalDomainBounds = field(default_factory=NumericalDomainBounds)
    hypothesis_config: HypothesisGenerationConfig = field(default_factory=HypothesisGenerationConfig)
    fitting_config: FittingConfig = field(default_factory=FittingConfig)
    ranking_policy: RankingPolicyConfig = field(default_factory=RankingPolicyConfig)
    experiment_pool_size: int = 10
    experiment_strategy: ExperimentGenerationStrategy = ExperimentGenerationStrategy.GRID
    allow_duplicate_queries: bool = False
    allow_duplicate_external_observations: bool = False
    query_exclusion_tolerance: float = 1e-5
    experiment_seed: Optional[int] = None
    dataset_id: Optional[str] = None
    max_experiment_candidates_per_iteration: int = 25

    def __post_init__(self) -> None:
        self._validate()

    def _validate(self) -> None:
        if isinstance(self.max_query_budget, bool) or not isinstance(self.max_query_budget, int):
            raise ValueError("max_query_budget must be an integer >= 0.")
        if self.max_query_budget < 0:
            raise ValueError("max_query_budget must be an integer >= 0.")
        if isinstance(self.max_iterations, bool) or not isinstance(self.max_iterations, int):
            raise ValueError("max_iterations must be an integer >= 1.")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be an integer >= 1.")
        if not isinstance(self.stopping_criteria, StoppingCriteriaConfig):
            raise ValueError("stopping_criteria must be a StoppingCriteriaConfig.")
        if not isinstance(self.domain_bounds, NumericalDomainBounds):
            raise ValueError("domain_bounds must be a NumericalDomainBounds.")
        if not isinstance(self.hypothesis_config, HypothesisGenerationConfig):
            raise ValueError("hypothesis_config must be a HypothesisGenerationConfig.")
        generation_error = self.hypothesis_config.validate()
        if generation_error is not None:
            raise ValueError(
                f"Invalid hypothesis_config: [{generation_error.code}] {generation_error.message}"
            )
        if not isinstance(self.fitting_config, FittingConfig):
            raise ValueError("fitting_config must be a FittingConfig.")
        fitting_error = self.fitting_config.validate()
        if fitting_error is not None:
            raise ValueError(
                f"Invalid fitting_config: [{fitting_error.code}] {fitting_error.message}"
            )
        if not isinstance(self.ranking_policy, RankingPolicyConfig):
            raise ValueError("ranking_policy must be a RankingPolicyConfig.")
        if (
            isinstance(self.experiment_pool_size, bool)
            or not isinstance(self.experiment_pool_size, int)
            or not 1 <= self.experiment_pool_size <= 500
        ):
            raise ValueError("experiment_pool_size must be an integer between 1 and 500.")
        if not isinstance(self.experiment_strategy, ExperimentGenerationStrategy):
            raise ValueError("experiment_strategy must be an ExperimentGenerationStrategy.")
        if not isinstance(self.allow_duplicate_queries, bool):
            raise ValueError("allow_duplicate_queries must be a bool.")
        if not isinstance(self.allow_duplicate_external_observations, bool):
            raise ValueError("allow_duplicate_external_observations must be a bool.")
        if (
            isinstance(self.query_exclusion_tolerance, bool)
            or not isinstance(self.query_exclusion_tolerance, (int, float))
            or not math.isfinite(float(self.query_exclusion_tolerance))
            or float(self.query_exclusion_tolerance) < 0.0
        ):
            raise ValueError("query_exclusion_tolerance must be a finite number >= 0.")
        if self.experiment_seed is not None and (
            isinstance(self.experiment_seed, bool) or not isinstance(self.experiment_seed, int)
        ):
            raise ValueError("experiment_seed must be an integer or None.")
        if self.dataset_id is not None and (
            not isinstance(self.dataset_id, str) or not self.dataset_id
        ):
            raise ValueError("dataset_id must be a non-empty string or None.")
        if (
            isinstance(self.max_experiment_candidates_per_iteration, bool)
            or not isinstance(self.max_experiment_candidates_per_iteration, int)
            or self.max_experiment_candidates_per_iteration < 1
        ):
            raise ValueError(
                "max_experiment_candidates_per_iteration must be an integer >= 1."
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the sanitized, reproducible configuration snapshot."""
        return {
            "max_query_budget": self.max_query_budget,
            "max_iterations": self.max_iterations,
            "stopping_criteria": self.stopping_criteria.to_dict(),
            "domain_bounds": {
                "variable_name": self.domain_bounds.variable_name,
                "min_val": self.domain_bounds.min_val,
                "max_val": self.domain_bounds.max_val,
                "domain_name": self.domain_bounds.domain_name,
            },
            "hypothesis_config": self.hypothesis_config.to_dict(),
            "fitting_config": self.fitting_config.to_dict(),
            "ranking_policy": {
                "primary_metric": self.ranking_policy.primary_metric,
                "secondary_metric": self.ranking_policy.secondary_metric,
                "metric_direction": self.ranking_policy.metric_direction,
                "complexity_window": self.ranking_policy.complexity_window,
                "tolerance_absolute": self.ranking_policy.tolerance_absolute,
                "tolerance_relative": self.ranking_policy.tolerance_relative,
                "min_samples_for_plausibility": self.ranking_policy.min_samples_for_plausibility,
                "max_tolerated_contradictions": self.ranking_policy.max_tolerated_contradictions,
                "performance_acceptance_threshold": (
                    self.ranking_policy.performance_acceptance_threshold
                ),
                "allow_unverified_comparability": self.ranking_policy.allow_unverified_comparability,
            },
            "experiment_pool_size": self.experiment_pool_size,
            "experiment_strategy": self.experiment_strategy.value,
            "allow_duplicate_queries": self.allow_duplicate_queries,
            "allow_duplicate_external_observations": (
                self.allow_duplicate_external_observations
            ),
            "query_exclusion_tolerance": self.query_exclusion_tolerance,
            "experiment_seed": self.experiment_seed,
            "dataset_id": self.dataset_id,
            "max_experiment_candidates_per_iteration": (
                self.max_experiment_candidates_per_iteration
            ),
        }


# ==============================================================================
# 4. Query Accounting
# ==============================================================================

@dataclass(frozen=True)
class QueryAccounting:
    """Oracle invocation accounting for one run.

    ``attempted_queries`` counts every ``oracle.query`` call, including calls
    that raised or returned an invalid/failed observation. Pre-invocation
    rejections and duplicate skips are not invocations and do not consume
    budget.
    """

    max_query_budget: int
    attempted_queries: int
    successful_queries: int
    failed_queries: int

    def __post_init__(self) -> None:
        if self.attempted_queries > self.max_query_budget:
            raise ValueError("attempted_queries must not exceed max_query_budget.")
        if self.successful_queries + self.failed_queries != self.attempted_queries:
            raise ValueError(
                "successful_queries + failed_queries must equal attempted_queries."
            )
        if min(self.attempted_queries, self.successful_queries, self.failed_queries) < 0:
            raise ValueError("query counters must be non-negative.")

    @property
    def remaining_queries(self) -> int:
        return max(0, self.max_query_budget - self.attempted_queries)

    def to_dict(self) -> Dict[str, int]:
        return {
            "max_query_budget": self.max_query_budget,
            "attempted_queries": self.attempted_queries,
            "successful_queries": self.successful_queries,
            "failed_queries": self.failed_queries,
            "remaining_queries": self.remaining_queries,
        }


# ==============================================================================
# 5. History Records
# ==============================================================================

class ExperimentExecutionOutcome(str, Enum):
    """Outcome of one proposed experiment attempt."""

    VALIDATED = "validated"
    REJECTED_PRE_INVOCATION = "rejected_pre_invocation"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    INVOKED_SUCCESS = "invoked_success"
    INVOKED_FAILED = "invoked_failed"
    FAILED_BEFORE_EXECUTION = "failed_before_execution"


@dataclass(frozen=True)
class ExperimentExecutionRecord:
    """Structured history entry for one proposed experiment.

    Fields:
      sequence: 1-based position in the experiment history.
      iteration: 1-based outer iteration that proposed the experiment.
      experiment_id: Stable candidate-experiment identifier from Issue #9.
      input_id: ExperimentInput identifier submitted (or proposed).
      input_values: Input values proposed. Never contains oracle internals.
      outcome: Terminal outcome of this proposal.
      observation_id: Committed observation ID, when the query succeeded.
      error: Structured failure/skip explanation, when applicable.
      oracle_sequence_number: The observation's oracle sequence number, when
        the oracle reported one.
      strategy: Experiment generation strategy label.
      recorded_at: UTC ISO-8601 timestamp.
    """

    sequence: int
    iteration: int
    experiment_id: str
    input_id: str
    input_values: Mapping[str, Any]
    outcome: ExperimentExecutionOutcome
    observation_id: Optional[str] = None
    error: Optional[StructuredError] = None
    oracle_sequence_number: Optional[int] = None
    strategy: Optional[str] = None
    recorded_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("ExperimentExecutionRecord sequence must be >= 1.")
        if self.iteration < 1:
            raise ValueError("ExperimentExecutionRecord iteration must be >= 1.")
        if not isinstance(self.input_values, Mapping):
            raise ValueError("input_values must be a mapping.")
        if self.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS and not self.observation_id:
            raise ValueError("INVOKED_SUCCESS requires an observation_id.")
        if (
            self.outcome
            in (
                ExperimentExecutionOutcome.INVOKED_FAILED,
                ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION,
                ExperimentExecutionOutcome.SKIPPED_DUPLICATE,
                ExperimentExecutionOutcome.FAILED_BEFORE_EXECUTION,
            )
            and self.error is None
        ):
            raise ValueError(f"Outcome {self.outcome.value} requires a structured error.")
        object.__setattr__(self, "input_values", freeze_payload(self.input_values))

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "sequence": self.sequence,
            "iteration": self.iteration,
            "experiment_id": self.experiment_id,
            "input_id": self.input_id,
            "input_values": thaw_payload(self.input_values),
            "outcome": self.outcome.value,
            "recorded_at": self.recorded_at,
        }
        if self.observation_id is not None:
            data["observation_id"] = self.observation_id
        if self.oracle_sequence_number is not None:
            data["oracle_sequence_number"] = self.oracle_sequence_number
        if self.strategy is not None:
            data["strategy"] = self.strategy
        if self.error is not None:
            data["error"] = self.error.to_dict()
        return data


@dataclass(frozen=True)
class DiscoveryIterationRecord:
    """Structured history entry for one completed outer iteration.

    Captures the query-accounting delta, the selected experiment/observation,
    the resulting top hypothesis, and whether ranking is fresh. Large payloads
    are referenced by ID rather than copied.
    """

    iteration: int
    selected_experiment_id: Optional[str]
    observation_id: Optional[str]
    outcome: Optional[ExperimentExecutionOutcome]
    attempted_queries_before: int
    attempted_queries_after: int
    successful_queries_after: int
    failed_queries_after: int
    top_hypothesis_id: Optional[str]
    top_primary_metric: Optional[float]
    ranking_is_fresh: bool
    termination_reason: Optional[DiscoveryTerminationReason] = None
    warnings: Tuple[StructuredError, ...] = ()
    recorded_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.iteration < 1:
            raise ValueError("DiscoveryIterationRecord iteration must be >= 1.")
        if self.attempted_queries_after < self.attempted_queries_before:
            raise ValueError("Query accounting must not decrease within an iteration.")

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "iteration": self.iteration,
            "selected_experiment_id": self.selected_experiment_id,
            "observation_id": self.observation_id,
            "outcome": self.outcome.value if self.outcome is not None else None,
            "attempted_queries_before": self.attempted_queries_before,
            "attempted_queries_after": self.attempted_queries_after,
            "successful_queries_after": self.successful_queries_after,
            "failed_queries_after": self.failed_queries_after,
            "top_hypothesis_id": self.top_hypothesis_id,
            "top_primary_metric": self.top_primary_metric,
            "ranking_is_fresh": self.ranking_is_fresh,
            "recorded_at": self.recorded_at,
        }
        if self.termination_reason is not None:
            data["termination_reason"] = self.termination_reason.value
        if self.warnings:
            data["warnings"] = [warning.to_dict() for warning in self.warnings]
        return data


@dataclass(frozen=True)
class DiscoveryIngestionRecord:
    """History entry for one externally supplied observation."""

    sequence: int
    observation_id: str
    input_values: Mapping[str, Any]
    ranking_is_fresh: bool
    evaluation_count: int
    recorded_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("DiscoveryIngestionRecord sequence must be >= 1.")
        if not isinstance(self.input_values, Mapping):
            raise ValueError("input_values must be a mapping.")
        object.__setattr__(self, "input_values", freeze_payload(self.input_values))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "observation_id": self.observation_id,
            "input_values": thaw_payload(self.input_values),
            "ranking_is_fresh": self.ranking_is_fresh,
            "evaluation_count": self.evaluation_count,
            "recorded_at": self.recorded_at,
        }


# ==============================================================================
# 6. Experiment Planner Boundary
# ==============================================================================

class ExperimentPlanner(Protocol):
    """Narrow planning interface consumed by the discovery engine.

    Issue #10/#11 own active selection (multi-hypothesis disagreement, dynamic
    information gain). The orchestrator only needs "choose one eligible
    experiment from this batch", so a planner can be swapped in without the
    engine knowing how selection works.

    A planner receives only permitted discovery data: candidate hypotheses,
    their evaluation results, committed observations, the generated batch, and
    the current ranking. It must never receive evaluator-only ground truth.
    """

    def select_experiment(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> Optional[CandidateExperiment]:
        """Return the next experiment, or None when nothing is selectable."""
        ...


class DeterministicOrderPlanner:
    """Default Issue #8 planner: first eligible candidate in batch order.

    Deterministic and information-free: it does not score hypotheses or peek at
    anything hidden. Active disagreement selection is deferred to Issue #10.
    """

    planner_id = "deterministic_order_planner"

    def select_experiment(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> Optional[CandidateExperiment]:
        for candidate in candidate_batch.candidates:
            if candidate.status == CandidateExperimentStatus.ELIGIBLE:
                return candidate
        return None


# ==============================================================================
# 7. Run Result Snapshot
# ==============================================================================

@dataclass(frozen=True)
class DiscoveryRunResult:
    """Immutable snapshot of a discovery run.

    The result is a plain data contract suitable for a later Flask serializer,
    persistence layer, or compute adapter. It contains no oracle objects,
    exception instances, open handles, or evaluator-only data.

    Fields:
      run_id: Stable run identifier (also the default evaluation dataset ID).
      status: Lifecycle state at snapshot time.
      termination_reason: Explicit machine-readable terminal reason. ``None``
        only for a snapshot taken while the run is still active.
      config: Sanitized reproducibility configuration.
      iterations_completed: Completed outer iterations.
      query_accounting: Attempted/successful/failed/remaining query counters.
      hypotheses: Candidate hypotheses, stable IDs preserved.
      evaluations: Latest EvaluationResult per hypothesis, candidate order.
      ranking: Latest valid ranking snapshot, or None when ranking never ran.
      observations: Committed observations in ingestion order.
      experiment_history: One record per proposed experiment attempt.
      iteration_history: One record per completed iteration.
      ingestion_history: One record per external observation ingestion.
      errors: Structured terminal/component errors.
      warnings: Structured non-fatal warnings.
      ranking_is_fresh: False when a new observation was committed but the
        evaluation/ranking refresh failed; the ranking snapshot is then stale.
      limitations: Human-readable limitations of this run.
      started_at / finished_at: UTC ISO-8601 timestamps.
    """

    run_id: str
    status: DiscoveryLifecycleStatus
    termination_reason: Optional[DiscoveryTerminationReason]
    config: DiscoveryRunConfig
    iterations_completed: int
    query_accounting: QueryAccounting
    hypotheses: Tuple[HypothesisRepresentation, ...] = ()
    evaluations: Tuple[EvaluationResult, ...] = ()
    ranking: Optional[RankingResult] = None
    observations: Tuple[Observation, ...] = ()
    experiment_history: Tuple[ExperimentExecutionRecord, ...] = ()
    iteration_history: Tuple[DiscoveryIterationRecord, ...] = ()
    ingestion_history: Tuple[DiscoveryIngestionRecord, ...] = ()
    errors: Tuple[StructuredError, ...] = ()
    warnings: Tuple[StructuredError, ...] = ()
    ranking_is_fresh: bool = False
    limitations: Tuple[str, ...] = ()
    started_at: str = field(default_factory=utc_now_iso)
    finished_at: str = field(default_factory=utc_now_iso)

    @property
    def ranked_hypotheses(self) -> Tuple[Any, ...]:
        """Ordered RankedCandidate tuple from the latest ranking snapshot."""
        return self.ranking.ranked_candidates if self.ranking is not None else ()

    @property
    def remaining_queries(self) -> int:
        return self.query_accounting.remaining_queries

    def evaluation_by_hypothesis(self) -> Dict[str, EvaluationResult]:
        """Return a plain mapping of latest evaluation results by hypothesis ID."""
        return {result.hypothesis_id: result for result in self.evaluations}

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a discovery-safe, JSON-compatible dictionary.

        Raises ValueError if any ground-truth/secret key reached the payload.
        """
        data: Dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status.value,
            "termination_reason": (
                self.termination_reason.value if self.termination_reason is not None else None
            ),
            "config": self.config.to_dict(),
            "iterations_completed": self.iterations_completed,
            "ranking_is_fresh": self.ranking_is_fresh,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "query_accounting": self.query_accounting.to_dict(),
            "hypotheses": [hypothesis.to_dict() for hypothesis in self.hypotheses],
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
            "ranking": self.ranking.to_dict() if self.ranking is not None else None,
            "observations": [observation.to_dict() for observation in self.observations],
            "experiment_history": [record.to_dict() for record in self.experiment_history],
            "iteration_history": [record.to_dict() for record in self.iteration_history],
            "ingestion_history": [record.to_dict() for record in self.ingestion_history],
            "errors": [error.to_dict() for error in self.errors],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "limitations": list(self.limitations),
        }
        verify_no_ground_truth_leakage(data)
        return data


def make_structured_error(code: str, message: str, **details: Any) -> StructuredError:
    """Create a StructuredError with optional details, omitted when empty."""
    return StructuredError(
        code=code,
        message=message,
        details=dict(details) if details else None,
    )


__all__ = [
    "ERROR_INVALID_DISCOVERY_CONFIG",
    "ERROR_INVALID_INITIAL_OBSERVATION",
    "ERROR_INVALID_OBSERVATION",
    "ERROR_DUPLICATE_OBSERVATION_ID",
    "ERROR_DUPLICATE_OBSERVATION_INPUT",
    "ERROR_RUN_NOT_ACTIVE",
    "ERROR_RUN_NOT_FINISHED",
    "ERROR_RUN_TERMINAL_FAILURE",
    "ERROR_CANDIDATE_GENERATION_FAILURE",
    "ERROR_EVALUATION_FAILURE",
    "ERROR_RANKING_FAILURE",
    "ERROR_EXPERIMENT_GENERATION_FAILURE",
    "ERROR_PLANNER_FAILURE",
    "ERROR_INVALID_PLANNER_SELECTION",
    "ERROR_DUPLICATE_QUERY",
    "ERROR_ORACLE_QUERY_FAILURE",
    "ERROR_INVALID_ORACLE_RESPONSE",
    "ERROR_UNEXPECTED_DISCOVERY_ERROR",
    "WARNING_EXPERIMENT_POOL_UNDERSIZED",
    "WARNING_DUPLICATE_QUERY_SKIPPED",
    "WARNING_RANKING_STALE",
    "DISCOVERY_ENGINE_VERSION",
    "DiscoveryRunError",
    "DiscoveryLifecycleStatus",
    "DiscoveryTerminationReason",
    "StoppingCriteriaConfig",
    "DiscoveryRunConfig",
    "QueryAccounting",
    "ExperimentExecutionOutcome",
    "ExperimentExecutionRecord",
    "DiscoveryIterationRecord",
    "DiscoveryIngestionRecord",
    "ExperimentPlanner",
    "DeterministicOrderPlanner",
    "DiscoveryRunResult",
    "make_structured_error",
]
