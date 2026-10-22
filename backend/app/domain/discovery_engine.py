"""End-to-end black-box discovery loop orchestration (Issue #8).

The engine is the *coordinator* only. It owns the bounded lifecycle, state
transitions, query accounting, and provenance. Each scientific responsibility
stays in its own component:

- candidate proposal ......... Issue #5 ``CandidateGenerator``
- fitting and evaluation ..... Issue #6 ``EvaluationPipeline``
- ranking and evidence ....... Issue #7 ``HypothesisRanker``
- experiment generation ...... Issue #9 ``CandidateExperimentGenerator``
- experiment selection ....... ``ExperimentPlanner`` (default deterministic)
- query execution ............ Issue #4 ``OracleInterface``

Ground-truth isolation
----------------------
The engine receives an ``OracleInterface`` and may only call its public
``validate_input`` and ``query`` methods. It never imports ``GroundTruthRule``,
never inspects oracle internals, and never places hidden values in results,
history, errors, or warnings. Only successful ``Observation`` payloads become
evidence.

State consistency
-----------------
A successful oracle response is validated and committed immediately. If the
subsequent evaluation/ranking refresh raises, the observation stays committed,
the previous ranking snapshot is preserved, ``ranking_is_fresh`` becomes False,
and the run terminates as FAILED through ``COMPONENT_FAILURE`` instead of
claiming a ranking it did not compute. A failed experiment never mutates
previously committed observations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from threading import Lock
from typing import Any, Callable, Dict, List, Optional, Tuple

from .candidate_generator import CandidateGenerationError, CandidateGenerator
from .contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    ExperimentInput,
    HypothesisRepresentation,
    Observation,
    ObservationStatus,
    OracleInterface,
    OracleQueryError,
    StructuredError,
    freeze_payload,
    generate_id,
    utc_now_iso,
)
from .discovery_contracts import (
    DISCOVERY_ENGINE_VERSION,
    ERROR_CANDIDATE_GENERATION_FAILURE,
    ERROR_DUPLICATE_OBSERVATION_ID,
    ERROR_DUPLICATE_OBSERVATION_INPUT,
    ERROR_DUPLICATE_QUERY,
    ERROR_EVALUATION_FAILURE,
    ERROR_EXPERIMENT_GENERATION_FAILURE,
    ERROR_INVALID_OBSERVATION,
    ERROR_INVALID_DISCOVERY_CONFIG,
    ERROR_INVALID_INITIAL_OBSERVATION,
    ERROR_INVALID_ORACLE_RESPONSE,
    ERROR_INVALID_PLANNER_SELECTION,
    ERROR_ORACLE_QUERY_FAILURE,
    ERROR_PLANNER_FAILURE,
    ERROR_RANKING_FAILURE,
    ERROR_RUN_NOT_ACTIVE,
    ERROR_RUN_NOT_FINISHED,
    ERROR_RUN_TERMINAL_FAILURE,
    ERROR_UNEXPECTED_DISCOVERY_ERROR,
    WARNING_DUPLICATE_QUERY_SKIPPED,
    WARNING_EXPERIMENT_POOL_UNDERSIZED,
    WARNING_EXPERIMENT_SELECTION_UNAVAILABLE,
    WARNING_RANKING_STALE,
    WARNING_SCIENTIST_FALLBACK,
    WARNING_SCIENTIST_PROPOSAL_SKIPPED,
    WARNING_SCIENTIST_ROUND_FAILED,
    DeterministicOrderPlanner,
    DiscoveryIngestionRecord,
    DiscoveryIterationRecord,
    DiscoveryLifecycleStatus,
    DiscoveryRunConfig,
    DiscoveryRunError,
    DiscoveryRunResult,
    DiscoveryTerminationReason,
    ExperimentExecutionOutcome,
    ExperimentExecutionRecord,
    ExperimentPlanner,
    QueryAccounting,
    make_structured_error,
)
from .evaluation import SUPPORTED_METRICS, EvaluationPipeline
from .experiment_generator import (
    ERROR_EMPTY_CANDIDATE_POOL,
    CandidateExperiment,
    CandidateExperimentBatch,
    CandidateExperimentGenerationError,
    CandidateExperimentGenerator,
    CandidateExperimentStatus,
    ExperimentGenerationConfig,
)
from .hypothesis_models import ModelFamilyRegistry, default_registry
from .llm_scientist import LLMScientist
from .llm_scientist_contracts import ScientistRoundRecord
from .ranking_contracts import EvaluationTier, PlausibilityStatus, RankingResult
from .ranking_engine import HypothesisRanker
from .selection_contracts import ExperimentSelectionResult

DEFAULT_METRIC_NAMES: Tuple[str, ...] = ("mae", "rmse")

RUN_LIMITATIONS: Tuple[str, ...] = (
    "All committed observations are used as training data; the engine does not "
    "reserve a held-out partition, so held-out metrics are reported as unavailable.",
    "The default planner selects the first eligible experiment in generator order. "
    "Active disagreement selection (Issue #10) is opt-in through an "
    "ExperimentPlanner; information-gain selection is deferred to Issue #11.",
    "Ranking is a presentation of measured evidence, not a claim that the hidden "
    "rule has been discovered.",
)


class _ComponentFailure(Exception):
    """Internal wrapper for a fatal downstream component failure."""

    def __init__(self, error: StructuredError):
        super().__init__(f"[{error.code}] {error.message}")
        self.error = error


class _BudgetExhausted(Exception):
    """Internal signal that no oracle invocation is permitted."""


class _CancelledException(Exception):
    """Internal signal that execution was cancelled."""


class DiscoveryEngine:
    """Coordinates one bounded discovery run over existing components."""

    engine_version = DISCOVERY_ENGINE_VERSION

    def __init__(
        self,
        oracle: OracleInterface,
        config: Optional[DiscoveryRunConfig] = None,
        *,
        candidate_generator: Optional[CandidateGenerator] = None,
        evaluation_pipeline: Optional[EvaluationPipeline] = None,
        ranker: Optional[HypothesisRanker] = None,
        experiment_generator: Optional[CandidateExperimentGenerator] = None,
        planner: Optional[ExperimentPlanner] = None,
        registry: Optional[ModelFamilyRegistry] = None,
        scientist: Optional[LLMScientist] = None,
        run_id: Optional[str] = None,
        cancellation_token: Optional[Callable[[], bool]] = None,
    ):
        if not callable(getattr(oracle, "validate_input", None)) or not callable(
            getattr(oracle, "query", None)
        ):
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "A discovery oracle must expose validate_input() and query().",
                    received_type=type(oracle).__name__,
                )
            )

        self._config = config if config is not None else DiscoveryRunConfig()
        if not isinstance(self._config, DiscoveryRunConfig):
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "config must be a DiscoveryRunConfig.",
                    received_type=type(self._config).__name__,
                )
            )

        policy = self._config.ranking_policy
        unsupported = [
            name
            for name in (policy.primary_metric, policy.secondary_metric)
            if name not in SUPPORTED_METRICS
        ]
        if unsupported:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    f"Ranking policy references unsupported metric(s): {sorted(set(unsupported))}.",
                    supported_metrics=sorted(SUPPORTED_METRICS),
                )
            )

        self._oracle = oracle
        self._registry = registry if registry is not None else default_registry()
        self._candidate_generator = candidate_generator or CandidateGenerator(
            self._config.hypothesis_config, registry=self._registry
        )
        self._evaluation_pipeline = evaluation_pipeline or EvaluationPipeline(
            registry=self._registry, fitting_config=self._config.fitting_config
        )
        self._ranker = ranker or HypothesisRanker(
            policy=self._config.ranking_policy, registry=self._registry
        )
        self._experiment_generator = experiment_generator or CandidateExperimentGenerator()
        self._planner: ExperimentPlanner = planner or DeterministicOrderPlanner()

        # Optional LLM Scientist (Issue #41). It is a proposal source only: the
        # engine still owns fitting, evaluation, ranking, query accounting,
        # experiment selection, and stopping. ``None`` means deterministic-only.
        if scientist is not None and not callable(getattr(scientist, "propose", None)):
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "scientist must expose a propose() method.",
                    received_type=type(scientist).__name__,
                )
            )
        self._scientist = scientist
        self._scientist_history: List[ScientistRoundRecord] = []
        self._pending_suggested_inputs: List[float] = []

        self._run_id = run_id or generate_id("run")
        self._dataset_id = self._config.dataset_id or self._run_id

        # Candidate state (stable IDs preserved for the whole run).
        self._candidates: List[HypothesisRepresentation] = []
        self._candidate_by_id: Dict[str, HypothesisRepresentation] = {}
        self._output_variable: Optional[str] = None

        # Committed evidence.
        self._observations: Dict[str, Observation] = {}
        self._evaluations: Dict[str, EvaluationResult] = {}
        self._ranking: Optional[RankingResult] = None
        self._ranking_is_fresh = False

        # Accounting.
        self._attempted_queries = 0
        self._successful_queries = 0
        self._failed_queries = 0
        self._rejected_queries = 0
        self._duplicate_queries = 0
        self._retry_count = 0
        self._in_flight_queries = 0
        # Query reservations are atomic so concurrent callers cannot reserve
        # more invocations than the configured run budget.
        self._query_lock = Lock()
        self._lifecycle_lock = Lock()
        self._consecutive_failures = 0
        self._iterations_completed = 0
        self._no_informative_experiment = False

        # History.
        self._experiment_history: List[ExperimentExecutionRecord] = []
        self._iteration_history: List[DiscoveryIterationRecord] = []
        self._ingestion_history: List[DiscoveryIngestionRecord] = []
        self._errors: List[StructuredError] = []
        self._warnings: List[StructuredError] = []

        # Lifecycle.
        self._status = DiscoveryLifecycleStatus.INITIALIZING
        self._termination_reason: Optional[DiscoveryTerminationReason] = None
        self._started = False
        self._finished = False
        self._started_at = utc_now_iso()
        self._finished_at = self._started_at

        # Cancellation.
        self._cancellation_token: Optional[Callable[[], bool]] = cancellation_token
        self._cancellation_requested: bool = False

    # ------------------------------------------------------------------
    # Read-only inspection
    # ------------------------------------------------------------------

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def config(self) -> DiscoveryRunConfig:
        return self._config

    @property
    def status(self) -> DiscoveryLifecycleStatus:
        return self._status

    @property
    def observations(self) -> Tuple[Observation, ...]:
        """Committed observations as read-only snapshots.

        Payloads are frozen, so nested mutation raises instead of altering
        committed evidence.
        """
        return tuple(self._frozen_observation(o) for o in self._observations.values())

    @property
    def candidates(self) -> Tuple[HypothesisRepresentation, ...]:
        return tuple(self._candidates)

    @property
    def ranking(self) -> Optional[RankingResult]:
        return self._ranking

    @property
    def query_accounting(self) -> QueryAccounting:
        return self._accounting()

    # ------------------------------------------------------------------
    # Cancellation controls
    # ------------------------------------------------------------------

    def set_cancellation_token(self, token: Optional[Callable[[], bool]]) -> None:
        """Register a cancellation token callback that returns True if cancelled."""
        self._cancellation_token = token

    def request_cancellation(self) -> None:
        """Signal the discovery engine to stop scheduling new work and shut down."""
        self._cancellation_requested = True

    def cancel(self) -> None:
        """Alias for request_cancellation."""
        self.request_cancellation()

    def is_cancelled(self) -> bool:
        """Check whether cancellation was signaled via explicit request or token."""
        if self._cancellation_requested:
            return True
        if self._cancellation_token is not None:
            try:
                return bool(self._cancellation_token())
            except Exception:
                return False
        return False

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(
        self,
        initial_observations: Sequence[Observation] = (),
        cancellation_token: Optional[Callable[[], bool]] = None,
    ) -> DiscoveryRunResult:
        """Execute one bounded discovery run and return its result snapshot.

        ``initial_observations`` must be permitted observations already
        available to the discovery engine. They never consume query budget.
        A configuration object that cannot even be constructed raises
        ``DiscoveryRunError``; any failure after construction (including
        invalid initial observations or candidate generation failure) is
        reported inside the returned result as FAILED, before any oracle call.
        """
        if cancellation_token is not None:
            self._cancellation_token = cancellation_token

        with self._lifecycle_lock:
            if self._started:
                raise DiscoveryRunError(
                    make_structured_error(
                        ERROR_INVALID_DISCOVERY_CONFIG,
                        "This DiscoveryEngine instance has already been started; create a new "
                        "engine for a new run.",
                    )
                )
            self._started = True
            self._started_at = utc_now_iso()

        if self.is_cancelled():
            return self._finalize(
                DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                DiscoveryTerminationReason.CANCELLED,
            )

        try:
            self._initialize_candidates()
            self._ingest_initial_observations(initial_observations)
        except DiscoveryRunError as exc:
            self._errors.append(exc.error)
            return self._finalize(
                DiscoveryLifecycleStatus.FAILED,
                DiscoveryTerminationReason.UNRECOVERABLE_ERROR,
            )

        if self.is_cancelled():
            return self._finalize(
                DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                DiscoveryTerminationReason.CANCELLED,
            )

        self._status = DiscoveryLifecycleStatus.RUNNING

        # Initial evaluation/ranking pass. A failure here is terminal but still
        # returns the valid initial observations and hypotheses.
        if self._observations:
            if not self._refresh_evaluation_and_ranking():
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )
        else:
            if not self._rank_without_evaluations():
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )

        # Optional first LLM proposal round: it sees the initial committed
        # evidence and, if it contributes new candidates, those candidates are
        # evaluated and ranked by the existing pipeline before iteration 1.
        if self._run_scientist_round(iteration=None):
            if self._observations:
                if not self._refresh_evaluation_and_ranking():
                    return self._finalize(
                        DiscoveryLifecycleStatus.FAILED,
                        DiscoveryTerminationReason.COMPONENT_FAILURE,
                    )
            elif not self._rank_without_evaluations():
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )

        if self.is_cancelled():
            return self._finalize(
                DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                DiscoveryTerminationReason.CANCELLED,
            )

        return self._run_iterations()

    def add_observation(self, observation: Observation) -> DiscoveryRunResult:
        """Ingest one observation while the run is still active.

        Lifecycle contract: a run is mutable only until it terminates. Once a
        result has been finalized, this method raises ``DiscoveryRunError`` with
        ``RUN_NOT_ACTIVE`` and does not touch state. Use ``resume()`` for a
        deliberate post-terminal update of a non-failed run.

        While active, the update preserves existing observation and hypothesis
        IDs, rejects invalid or duplicate identities explicitly, and refreshes
        evaluation/ranking without consuming oracle budget. If the refresh
        fails, the observation and the previous ranking snapshot are preserved
        and the run ends FAILED.
        """
        if not self._started:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "add_observation() requires an initialized run; call run() first.",
                )
            )
        if self._finished:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_RUN_NOT_ACTIVE,
                    "add_observation() is not permitted after the run has terminated; "
                    "use resume() for a deliberate post-terminal update.",
                    status=self._status.value,
                    termination_reason=(
                        self._termination_reason.value
                        if self._termination_reason is not None
                        else None
                    ),
                )
            )
        error = self._validate_external_observation(observation, ERROR_INVALID_OBSERVATION)
        if error is not None:
            raise DiscoveryRunError(error)

        self._observations[observation.observation_id] = self._frozen_observation(observation)
        refresh_ok = self._refresh_evaluation_and_ranking()
        self._append_ingestion_record(observation, refresh_ok)
        if not refresh_ok:
            return self._finalize(
                DiscoveryLifecycleStatus.FAILED,
                DiscoveryTerminationReason.COMPONENT_FAILURE,
            )
        return self._build_result(self._status, self._termination_reason)

    def resume(self, observations: Sequence[Observation] = ()) -> DiscoveryRunResult:
        """Explicitly reopen a terminated, non-failed run with new observations.

        This is the supported incremental-update lifecycle after a run has
        ended. It validates every observation before mutating state, preserves
        existing observation and hypothesis IDs, ingests the observations
        without consuming oracle budget, refreshes evaluation/ranking, and then
        continues the same bounded loop. Budget and iteration counters are not
        reset, so resume can never exceed the configured limits.

        A FAILED run is not resumable because its state may be inconsistent;
        such calls raise ``DiscoveryRunError`` with ``RUN_TERMINAL_FAILURE`` and
        leave all state untouched. Calls before the run finishes raise
        ``RUN_NOT_FINISHED``.
        """
        if not self._started:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "resume() requires an initialized run; call run() first.",
                )
            )
        if not self._finished:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_RUN_NOT_FINISHED,
                    "resume() can only reopen a terminated run.",
                    status=self._status.value,
                )
            )
        if self._status == DiscoveryLifecycleStatus.FAILED:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_RUN_TERMINAL_FAILURE,
                    "A failed discovery run is not resumable.",
                    termination_reason=(
                        self._termination_reason.value
                        if self._termination_reason is not None
                        else None
                    ),
                )
            )
        validated = self._validate_external_observation_batch(observations)

        self._status = DiscoveryLifecycleStatus.RUNNING
        self._termination_reason = None
        self._finished = False
        for observation in validated:
            self._observations[observation.observation_id] = self._frozen_observation(observation)

        refresh_ok = self._refresh_evaluation_and_ranking()
        for observation in validated:
            self._append_ingestion_record(observation, refresh_ok)
        if not refresh_ok:
            return self._finalize(
                DiscoveryLifecycleStatus.FAILED,
                DiscoveryTerminationReason.COMPONENT_FAILURE,
            )
        return self._run_iterations()

    # ------------------------------------------------------------------
    # Initialization
    # ------------------------------------------------------------------

    def _ingest_initial_observations(self, initial_observations: Sequence[Observation]) -> None:
        if initial_observations is None:
            initial_observations = ()
        if isinstance(initial_observations, (str, bytes)) or isinstance(
            initial_observations, Mapping
        ):
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_INITIAL_OBSERVATION,
                    "initial_observations must be a sequence of Observation instances.",
                    received_type=type(initial_observations).__name__,
                )
            )
        for observation in initial_observations:
            error = self._validate_external_observation(observation, ERROR_INVALID_INITIAL_OBSERVATION)
            if error is not None:
                raise DiscoveryRunError(error)
            if observation.observation_id in self._observations:
                raise DiscoveryRunError(
                    make_structured_error(
                        ERROR_DUPLICATE_OBSERVATION_ID,
                        f"Duplicate initial observation ID {observation.observation_id!r}.",
                        observation_id=observation.observation_id,
                    )
                )
            self._observations[observation.observation_id] = self._frozen_observation(observation)

    def _initialize_candidates(self) -> None:
        try:
            candidates = self._candidate_generator.generate()
        except CandidateGenerationError as exc:
            raise DiscoveryRunError(exc.error) from None
        except Exception as exc:  # noqa: BLE001 - converted to a structured terminal error
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_CANDIDATE_GENERATION_FAILURE,
                    f"Candidate generation failed ({type(exc).__name__}).",
                    exception_type=type(exc).__name__,
                )
            ) from None
        if not candidates:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_CANDIDATE_GENERATION_FAILURE,
                    "Candidate generation returned no candidates.",
                )
            )
        self._candidates = list(candidates)
        self._candidate_by_id = {candidate.hypothesis_id: candidate for candidate in candidates}

        family = self._registry.get(self._candidates[0].model_family)
        if family is None:
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    f"Candidate model family {self._candidates[0].model_family!r} is not registered.",
                )
            )
        variables, variable_error = family.numeric_task_variables(self._candidates[0].parameters)
        if variable_error is not None or variables is None:
            raise DiscoveryRunError(
                variable_error
                if variable_error is not None
                else make_structured_error(
                    ERROR_INVALID_DISCOVERY_CONFIG,
                    "The model family does not declare numeric task variables.",
                )
            )
        self._output_variable = variables[1]

    # ------------------------------------------------------------------
    # Iteration loop
    # ------------------------------------------------------------------

    def _run_iterations(self) -> DiscoveryRunResult:
        while True:
            if self.is_cancelled():
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                    DiscoveryTerminationReason.CANCELLED,
                )
            criterion = self._satisfied_stopping_criterion()
            if criterion is not None:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_STOPPING_CRITERION, criterion
                )
            if self._attempted_queries >= self._config.max_query_budget:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED,
                    DiscoveryTerminationReason.BUDGET_EXHAUSTED,
                )
            if self._iterations_completed >= self._config.max_iterations:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_MAX_ITERATIONS,
                    DiscoveryTerminationReason.MAX_ITERATIONS_REACHED,
                )

            iteration = self._iterations_completed + 1
            attempted_before = self._attempted_queries

            try:
                execution = self._execute_one_experiment(iteration)
            except _CancelledException:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                    DiscoveryTerminationReason.CANCELLED,
                )
            except _BudgetExhausted:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED,
                    DiscoveryTerminationReason.BUDGET_EXHAUSTED,
                )
            except _ComponentFailure as failure:
                self._errors.append(failure.error)
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )
            except Exception as exc:  # noqa: BLE001 - never leak a raw crash
                if self.is_cancelled():
                    return self._finalize(
                        DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                        DiscoveryTerminationReason.CANCELLED,
                    )
                self._errors.append(
                    make_structured_error(
                        ERROR_UNEXPECTED_DISCOVERY_ERROR,
                        f"Unexpected discovery error ({type(exc).__name__}); the run stopped "
                        "and returned its recorded partial results.",
                        exception_type=type(exc).__name__,
                    )
                )
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.UNRECOVERABLE_ERROR,
                )

            if self.is_cancelled():
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_CANCELLED,
                    DiscoveryTerminationReason.CANCELLED,
                )

            if execution is None:
                reason = (
                    DiscoveryTerminationReason.NO_INFORMATIVE_EXPERIMENT_AVAILABLE
                    if self._no_informative_experiment
                    else DiscoveryTerminationReason.EXPERIMENT_POOL_EXHAUSTED
                )
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS,
                    reason,
                )

            iteration_warnings: List[StructuredError] = []
            refresh_ok = True
            if execution.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS:
                refresh_ok = self._refresh_evaluation_and_ranking()
                if not refresh_ok:
                    iteration_warnings.append(
                        make_structured_error(
                            WARNING_RANKING_STALE,
                            "The new observation was committed, but evaluation/ranking failed; "
                            "the previous ranking snapshot is preserved and is now stale.",
                            observation_id=execution.observation_id,
                        )
                    )

            self._iterations_completed = iteration
            top_id, top_metric = self._top_candidate_summary()
            record = DiscoveryIterationRecord(
                iteration=iteration,
                selected_experiment_id=execution.experiment_id,
                observation_id=execution.observation_id,
                outcome=execution.outcome,
                attempted_queries_before=attempted_before,
                attempted_queries_after=self._attempted_queries,
                successful_queries_after=self._successful_queries,
                failed_queries_after=self._failed_queries,
                top_hypothesis_id=top_id,
                top_primary_metric=top_metric,
                ranking_is_fresh=self._ranking_is_fresh and refresh_ok,
                warnings=tuple(self._frozen_error(item) for item in iteration_warnings),
            )
            self._iteration_history.append(record)

            if not refresh_ok:
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )

            # Evidence feedback cycle (Issue #41): only a successful query adds
            # committed evidence, so only then may a later proposal round see it.
            if (
                execution.outcome == ExperimentExecutionOutcome.INVOKED_SUCCESS
                and self._run_scientist_round(iteration=iteration)
            ):
                if not self._refresh_evaluation_and_ranking():
                    return self._finalize(
                        DiscoveryLifecycleStatus.FAILED,
                        DiscoveryTerminationReason.COMPONENT_FAILURE,
                    )

            # Stopping precedence order:
            # 1. Configured stopping criterion satisfied
            criterion = self._satisfied_stopping_criterion()
            if criterion is not None:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_STOPPING_CRITERION, criterion
                )

            # 2. Query budget exhausted (takes precedence over consecutive failures)
            if self._attempted_queries >= self._config.max_query_budget:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_BUDGET_EXHAUSTED,
                    DiscoveryTerminationReason.BUDGET_EXHAUSTED,
                )

            # 3. Repeated consecutive oracle failures when budget remains
            if (
                execution.outcome == ExperimentExecutionOutcome.INVOKED_FAILED
                and self._consecutive_failures >= self._config.stopping_criteria.consecutive_failure_limit
            ):
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.CONSECUTIVE_ORACLE_FAILURES,
                )

    def _execute_one_experiment(
        self, iteration: int
    ) -> Optional[ExperimentExecutionRecord]:
        """Generate, select, validate, and execute at most one experiment.

        Returns the execution record for the invocation attempt, or ``None``
        when no valid, non-duplicate, pre-validation-accepted experiment
        remains. Raises ``_ComponentFailure`` for contract violations and
        ``_BudgetExhausted`` if the budget disappeared mid-iteration.
        """
        if self.is_cancelled():
            raise _CancelledException()

        batch = self._generate_experiment_batch()
        if batch is None:
            return None
        if batch.is_undersized and batch.diagnostic_message:
            self._warnings.append(
                make_structured_error(
                    WARNING_EXPERIMENT_POOL_UNDERSIZED,
                    batch.diagnostic_message,
                    generated_count=batch.generated_count,
                    requested_count=batch.requested_count,
                )
            )

        tried: List[str] = []
        limit = min(
            len(batch.candidates),
            self._config.max_experiment_candidates_per_iteration,
        )
        for _ in range(limit):
            if self.is_cancelled():
                raise _CancelledException()
            remaining = [
                candidate
                for candidate in batch.candidates
                if candidate.candidate_id not in tried
            ]
            if not remaining:
                break
            selected, selection_diagnostics = self._select_experiment(remaining)
            if selected is None:
                if (
                    selection_diagnostics is not None
                    and selection_diagnostics.get("status") == "no_selection"
                ):
                    self._no_informative_experiment = True
                    self._warnings.append(
                        make_structured_error(
                            WARNING_EXPERIMENT_SELECTION_UNAVAILABLE,
                            "The experiment planner reported no selectable candidate; "
                            "the run terminates without an oracle call.",
                            reason_code=selection_diagnostics.get("reason_code"),
                            selection_strategy=selection_diagnostics.get("strategy"),
                            rationale=selection_diagnostics.get("rationale"),
                        )
                    )
                break
            if selected.candidate_id in tried:
                break
            tried.append(selected.candidate_id)

            in_batch = any(
                candidate.candidate_id == selected.candidate_id for candidate in batch.candidates
            )
            if not in_batch or selected.status != CandidateExperimentStatus.ELIGIBLE:
                self._rejected_queries += 1
                self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION,
                    error=make_structured_error(
                        ERROR_INVALID_PLANNER_SELECTION,
                        "Planner selected a candidate that is not eligible in the generated batch.",
                        experiment_id=selected.candidate_id,
                    ),
                    selection_diagnostics=selection_diagnostics,
                )
                continue

            if not self._config.allow_duplicate_queries and self._is_duplicate_input(
                selected.input_data
            ):
                self._duplicate_queries += 1
                self._warnings.append(
                    make_structured_error(
                        WARNING_DUPLICATE_QUERY_SKIPPED,
                        "Experiment input matches an already committed observation; it was "
                        "not submitted to the oracle.",
                        input_values=dict(selected.input_data.values),
                    )
                )
                self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.SKIPPED_DUPLICATE,
                    error=make_structured_error(
                        ERROR_DUPLICATE_QUERY,
                        "Duplicate experiment input skipped.",
                        input_values=dict(selected.input_data.values),
                    ),
                    selection_diagnostics=selection_diagnostics,
                )
                continue

            try:
                validation_error = self._oracle.validate_input(selected.input_data)
            except Exception as exc:  # noqa: BLE001 - oracle boundary
                raise _ComponentFailure(
                    make_structured_error(
                        ERROR_ORACLE_QUERY_FAILURE,
                        f"Oracle input validation raised ({type(exc).__name__}).",
                        exception_type=type(exc).__name__,
                    )
                ) from None
            if validation_error is not None:
                self._rejected_queries += 1
                cause_code = getattr(validation_error, "code", None) or "INVALID_INPUT"
                sanitized_error = make_structured_error(
                    cause_code if isinstance(cause_code, str) else "INVALID_INPUT",
                    f"Oracle rejected candidate input ({cause_code}).",
                    cause_code=cause_code,
                )
                self._warnings.append(sanitized_error)
                self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION,
                    error=sanitized_error,
                    selection_diagnostics=selection_diagnostics,
                )
                continue

            if self.is_cancelled():
                raise _CancelledException()
            if self._attempted_queries >= self._config.max_query_budget:
                raise _BudgetExhausted()
            return self._invoke_oracle(selected, iteration, selection_diagnostics)

        return None

    def _max_supported_polynomial_degree(self) -> int:
        """Highest degree the run's candidate configuration supports."""
        return max(0, int(self._config.hypothesis_config.max_degree))

    def _run_scientist_round(self, iteration: Optional[int]) -> bool:
        """Run one optional LLM proposal round; return True when candidates were added.

        The scientist receives only permitted state through the ``propose``
        arguments: committed observations, candidate specifications, evaluation
        metrics, the ranking snapshot, and the configured domain. It never
        receives the oracle, evaluator objects, ground truth, credentials, or
        query counters as mutable state.

        Every returned proposal is re-validated here before it becomes a
        candidate, so a provider cannot add an unsupported family, duplicate
        candidate, or malformed specification even if the scientist contract were
        bypassed. Advisory experiment suggestions are queued for the existing
        experiment generator; they cannot call the oracle directly.
        """
        scientist = self._scientist
        if scientist is None or self.is_cancelled():
            return False
        if not bool(getattr(scientist, "can_propose", True)):
            return False
        # A proposal round never consumes oracle budget. It only runs when true
        # committed evidence exists to reason about, and the scientist's own
        # ``max_rounds`` bounds the total number of provider calls. With a zero
        # query budget the run is "evaluate initial observations only", so no
        # provider call is made at all.
        if not self._observations:
            return False
        if iteration is None and self._config.max_query_budget == 0:
            return False

        try:
            outcome = scientist.propose(
                run_id=self._run_id,
                observations=self.observations,
                candidates=tuple(self._candidates),
                evaluations=dict(self._evaluations),
                ranking=self._ranking,
                domain_bounds=self._config.domain_bounds,
                output_variable=self._output_variable or "y",
                max_polynomial_degree=self._max_supported_polynomial_degree(),
                iteration=iteration,
            )
        except Exception as exc:  # noqa: BLE001 - scientist boundary is untrusted
            self._warnings.append(
                make_structured_error(
                    WARNING_SCIENTIST_ROUND_FAILED,
                    "Optional LLM Scientist round failed; deterministic discovery "
                    "continues with the existing candidates.",
                    exception_type=type(exc).__name__,
                    iteration=iteration,
                )
            )
            return False

        record = getattr(outcome, "record", None)
        hypotheses = getattr(outcome, "hypotheses", ())
        if not isinstance(record, ScientistRoundRecord) or not isinstance(hypotheses, tuple):
            self._warnings.append(
                make_structured_error(
                    WARNING_SCIENTIST_ROUND_FAILED,
                    "Optional LLM Scientist returned an invalid outcome; its output was "
                    "ignored and deterministic discovery continues.",
                    iteration=iteration,
                )
            )
            return False

        self._scientist_history.append(record)
        if record.fallback_applied:
            self._warnings.append(
                make_structured_error(
                    WARNING_SCIENTIST_FALLBACK,
                    record.fallback_reason
                    or "LLM proposal round produced no usable hypothesis; deterministic "
                    "discovery continued.",
                    round_id=record.round_id,
                    outcome_status=(
                        record.outcome_status.value
                        if record.outcome_status is not None
                        else None
                    ),
                    rejected_count=record.rejected_count,
                    iteration=iteration,
                )
            )

        added = 0
        existing_ids = set(self._candidate_by_id)
        existing_keys = {
            key
            for key in (
                self._canonical_candidate_key(candidate) for candidate in self._candidates
            )
            if key is not None
        }
        for hypothesis in hypotheses:
            if not isinstance(hypothesis, HypothesisRepresentation):
                continue
            if not hypothesis.hypothesis_id or hypothesis.hypothesis_id in existing_ids:
                self._warnings.append(
                    make_structured_error(
                        WARNING_SCIENTIST_PROPOSAL_SKIPPED,
                        "Scientist proposal was skipped because its hypothesis ID is "
                        "empty or already used.",
                        round_id=record.round_id,
                    )
                )
                continue
            family = self._registry.get(hypothesis.model_family)
            if family is None:
                self._warnings.append(
                    make_structured_error(
                        WARNING_SCIENTIST_PROPOSAL_SKIPPED,
                        "Scientist proposal used an unregistered model family and was "
                        "skipped.",
                        round_id=record.round_id,
                        model_family=hypothesis.model_family,
                    )
                )
                continue
            model, model_error = family.parse_specification(hypothesis.parameters)
            if model_error is not None or model is None:
                self._warnings.append(
                    make_structured_error(
                        WARNING_SCIENTIST_PROPOSAL_SKIPPED,
                        "Scientist proposal had an invalid model specification and was "
                        "skipped.",
                        round_id=record.round_id,
                        family_error_code=(model_error.code if model_error else None),
                    )
                )
                continue
            canonical_key = model.canonical_key()
            if canonical_key in existing_keys:
                self._warnings.append(
                    make_structured_error(
                        WARNING_SCIENTIST_PROPOSAL_SKIPPED,
                        "Scientist proposal duplicated an existing candidate and was "
                        "skipped.",
                        round_id=record.round_id,
                        hypothesis_id=hypothesis.hypothesis_id,
                    )
                )
                continue
            existing_keys.add(canonical_key)
            existing_ids.add(hypothesis.hypothesis_id)
            self._candidates.append(hypothesis)
            self._candidate_by_id[hypothesis.hypothesis_id] = hypothesis
            added += 1

        for value in getattr(outcome, "suggested_inputs", ()):
            if self._config.domain_bounds.validate_value(value) is not None:
                continue
            numeric = float(value)
            if numeric not in self._pending_suggested_inputs:
                self._pending_suggested_inputs.append(numeric)

        return added > 0

    def _canonical_candidate_key(
        self, candidate: HypothesisRepresentation
    ) -> Optional[str]:
        """Canonical duplication key for a candidate, or None when unparseable."""
        family = self._registry.get(candidate.model_family)
        if family is None:
            return None
        model, error = family.parse_specification(candidate.parameters)
        if error is not None or model is None:
            return None
        return model.canonical_key()

    def _generate_experiment_batch(self):
        exclude_inputs = [
            dict(observation.input_values) for observation in self._observations.values()
        ]
        # Advisory LLM suggestions are consumed once, through the existing
        # generator's validation/deduplication, and then cleared. The planner,
        # oracle validation, duplicate guard, and query budget still decide
        # whether anything is actually executed.
        suggested_inputs = list(self._pending_suggested_inputs)
        self._pending_suggested_inputs.clear()
        generation_config = ExperimentGenerationConfig(
            pool_size=self._config.experiment_pool_size,
            strategy=self._config.experiment_strategy,
            bounds=self._config.domain_bounds,
            seed=self._config.experiment_seed,
            exclude_inputs=exclude_inputs,
            tolerance=self._config.query_exclusion_tolerance,
            allow_undersized=True,
            suggested_inputs=suggested_inputs,
        )
        try:
            return self._experiment_generator.generate_batch(generation_config)
        except CandidateExperimentGenerationError as exc:
            if exc.error.code == ERROR_EMPTY_CANDIDATE_POOL:
                self._warnings.append(exc.error)
                return None
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_EXPERIMENT_GENERATION_FAILURE,
                    exc.error.message,
                    cause=exc.error.to_dict(),
                )
            ) from None
        except Exception as exc:  # noqa: BLE001 - converted to structured failure
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_EXPERIMENT_GENERATION_FAILURE,
                    f"Experiment generation failed ({type(exc).__name__}).",
                    exception_type=type(exc).__name__,
                )
            ) from None

    def _select_experiment(
        self,
        remaining: Sequence[CandidateExperiment],
    ) -> Tuple[Optional[CandidateExperiment], Optional[Dict[str, Any]]]:
        """Ask the planner for one experiment, preserving the Issue #8 contract.

        Planners that also expose ``select_experiment_with_diagnostics`` return
        a structured Issue #10 result; its serialized diagnostics are attached
        to the recorded experiment attempt. Plain planners return only the
        candidate, exactly as before.
        """
        batch = CandidateExperimentBatch(
            candidates=list(remaining),
            requested_count=len(remaining),
            generated_count=len(remaining),
        )
        structured = getattr(self._planner, "select_experiment_with_diagnostics", None)
        selection_diagnostics: Optional[Dict[str, Any]] = None
        try:
            if callable(structured):
                selection = structured(
                    list(self._candidates),
                    dict(self._evaluations),
                    list(self._observations.values()),
                    batch,
                    self._ranking,
                )
                if not isinstance(selection, ExperimentSelectionResult):
                    raise _ComponentFailure(
                        make_structured_error(
                            ERROR_INVALID_PLANNER_SELECTION,
                            "Structured planner selection must be an "
                            "ExperimentSelectionResult, got "
                            f"{type(selection).__name__}.",
                            received_type=type(selection).__name__,
                        )
                    )
                selection_diagnostics = selection.to_dict()
                selected = selection.selected_candidate
            else:
                selected = self._planner.select_experiment(
                    list(self._candidates),
                    dict(self._evaluations),
                    list(self._observations.values()),
                    batch,
                    self._ranking,
                )
        except _ComponentFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - planner boundary
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_PLANNER_FAILURE,
                    f"Experiment planner failed ({type(exc).__name__}).",
                    exception_type=type(exc).__name__,
                )
            ) from None
        if selected is None:
            return None, selection_diagnostics
        if not isinstance(selected, CandidateExperiment):
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_INVALID_PLANNER_SELECTION,
                    "Experiment planner must return a CandidateExperiment or None, got "
                    f"{type(selected).__name__}.",
                    received_type=type(selected).__name__,
                )
            )
        return selected, selection_diagnostics

    def _invoke_oracle(
        self,
        selected: CandidateExperiment,
        iteration: int,
        selection_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> ExperimentExecutionRecord:
        """Invoke the oracle under an atomic query-budget reservation.

        Only transient transport failures are retryable. Every retry is a new
        oracle invocation and consumes budget; invalid responses and other
        failures are not retried. Existing observations are committed only
        after a successful, validated response.
        """
        last_error: Optional[StructuredError] = None
        for attempt_index in range(self._config.max_query_retries + 1):
            if self.is_cancelled():
                raise _CancelledException()
            with self._query_lock:
                if self.is_cancelled():
                    raise _CancelledException()
                if self._attempted_queries >= self._config.max_query_budget:
                    if last_error is None:
                        raise _BudgetExhausted()
                    break
                self._attempted_queries += 1
                self._in_flight_queries = 1

            try:
                observation = self._oracle.query(selected.input_data)
            except Exception as exc:  # noqa: BLE001 - oracle boundary
                with self._query_lock:
                    self._failed_queries += 1
                    self._in_flight_queries = 0
                    self._consecutive_failures += 1
                if isinstance(exc, OracleQueryError):
                    cause = exc.error
                    # Do not forward arbitrary oracle error details: the
                    # adapter is an untrusted boundary and may include data
                    # that must not enter discovery diagnostics.
                    error = make_structured_error(
                        ERROR_ORACLE_QUERY_FAILURE,
                        f"Oracle query failed ({cause.code}).",
                        cause_code=cause.code,
                    )
                else:
                    error = make_structured_error(
                        ERROR_ORACLE_QUERY_FAILURE,
                        f"Oracle query raised ({type(exc).__name__}).",
                        exception_type=type(exc).__name__,
                    )
                last_error = error
                self._warnings.append(error)
                if (
                    self._is_retryable_oracle_exception(exc)
                    and attempt_index < self._config.max_query_retries
                    # Fast-path guard only — not the authoritative reservation.
                    # The loop is sequential (one oracle call active at a time per
                    # engine instance), so this read outside the lock is safe.
                    # The authoritative atomic budget check happens under
                    # ``_query_lock`` at the top of the next loop iteration.
                    and self._attempted_queries < self._config.max_query_budget
                ):
                    with self._query_lock:
                        self._retry_count += 1
                    continue
                return self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.INVOKED_FAILED,
                    error=error,
                    selection_diagnostics=selection_diagnostics,
                )

            validation_error = self._validate_oracle_response(observation, selected.input_data)
            if validation_error is not None:
                with self._query_lock:
                    self._failed_queries += 1
                    self._in_flight_queries = 0
                    self._consecutive_failures += 1
                self._warnings.append(validation_error)
                return self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.INVOKED_FAILED,
                    error=validation_error,
                    selection_diagnostics=selection_diagnostics,
                )

            with self._query_lock:
                self._successful_queries += 1
                self._in_flight_queries = 0
                self._consecutive_failures = 0
            self._observations[observation.observation_id] = self._frozen_observation(observation)
            return self._record_experiment(
                iteration,
                selected,
                ExperimentExecutionOutcome.INVOKED_SUCCESS,
                observation=observation,
                selection_diagnostics=selection_diagnostics,
            )

        # A previous failed attempt consumed the remaining budget. Preserve
        # its structured failure rather than submitting another oracle call.
        assert last_error is not None
        return self._record_experiment(
            iteration,
            selected,
            ExperimentExecutionOutcome.INVOKED_FAILED,
            error=last_error,
            selection_diagnostics=selection_diagnostics,
        )

    @staticmethod
    def _is_retryable_oracle_exception(exc: Exception) -> bool:
        """Retry only explicit transient transport failures."""
        if isinstance(exc, (TimeoutError, ConnectionError)):
            return True
        if isinstance(exc, OracleQueryError):
            return exc.error.code in {
                "ORACLE_TIMEOUT",
                "ORACLE_RATE_LIMITED",
                "ORACLE_TEMPORARY_FAILURE",
                "ORACLE_UNAVAILABLE",
            }
        return False

    def _record_experiment(
        self,
        iteration: int,
        selected: CandidateExperiment,
        outcome: ExperimentExecutionOutcome,
        error: Optional[StructuredError] = None,
        observation: Optional[Observation] = None,
        selection_diagnostics: Optional[Dict[str, Any]] = None,
    ) -> ExperimentExecutionRecord:
        record = ExperimentExecutionRecord(
            sequence=len(self._experiment_history) + 1,
            iteration=iteration,
            experiment_id=selected.candidate_id,
            input_id=selected.input_data.input_id,
            input_values=dict(selected.input_data.values),
            outcome=outcome,
            observation_id=observation.observation_id if observation is not None else None,
            error=self._frozen_error(error) if error is not None else None,
            oracle_sequence_number=(
                observation.sequence_number if observation is not None else None
            ),
            strategy=(
                selected.strategy.value if selected.strategy is not None else None
            ),
            selection_diagnostics=selection_diagnostics,
        )
        self._experiment_history.append(record)
        return record

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_external_observation(
        self, observation: Any, code: str
    ) -> Optional[StructuredError]:
        if not isinstance(observation, Observation):
            return make_structured_error(
                code,
                "Observations must be Observation instances.",
                received_type=type(observation).__name__,
            )
        if not isinstance(observation.observation_id, str) or not observation.observation_id:
            return make_structured_error(code, "Observation ID must be a non-empty string.")
        if observation.observation_id in self._observations:
            return make_structured_error(
                ERROR_DUPLICATE_OBSERVATION_ID,
                f"Observation ID {observation.observation_id!r} was already ingested.",
                observation_id=observation.observation_id,
            )
        if observation.status != ObservationStatus.SUCCESS:
            return make_structured_error(
                code,
                "Only successful observations can become discovery evidence.",
                status=observation.status.value,
            )
        input_variable = self._config.domain_bounds.variable_name
        input_error = self._validate_numeric_payload(
            observation.input_values, input_variable, "input", code
        )
        if input_error is not None:
            return input_error
        if self._output_variable is None:
            return make_structured_error(
                code, "The evaluation output variable is not initialized for this run."
            )
        output_error = self._validate_numeric_payload(
            observation.output_values, self._output_variable, "output", code
        )
        if output_error is not None:
            return output_error
        if (
            not self._config.allow_duplicate_external_observations
            and self._is_duplicate_input_values(observation.input_values)
        ):
            return make_structured_error(
                ERROR_DUPLICATE_OBSERVATION_INPUT,
                "An observation with the same input values was already committed.",
                input_values=dict(observation.input_values),
            )
        return None

    def _frozen_error(self, error: StructuredError) -> StructuredError:
        """Return a snapshot whose details cannot be mutated in place."""
        return StructuredError(
            code=error.code,
            message=error.message,
            details=(
                freeze_payload(dict(error.details)) if error.details is not None else None
            ),
        )

    def _frozen_evaluation(self, evaluation: EvaluationResult) -> EvaluationResult:
        """Return an evaluation snapshot with read-only nested mappings."""
        return replace(
            evaluation,
            metrics=freeze_payload(dict(evaluation.metrics)),
            held_out_metrics=(
                freeze_payload(dict(evaluation.held_out_metrics))
                if evaluation.held_out_metrics is not None
                else None
            ),
            error=(
                self._frozen_error(evaluation.error)
                if evaluation.error is not None
                else None
            ),
            fitting_config=(
                freeze_payload(dict(evaluation.fitting_config))
                if evaluation.fitting_config is not None
                else None
            ),
            fitted_specification=(
                freeze_payload(dict(evaluation.fitted_specification))
                if evaluation.fitted_specification is not None
                else None
            ),
            observation_counts=(
                freeze_payload(dict(evaluation.observation_counts))
                if evaluation.observation_counts is not None
                else None
            ),
            exclusion_codes=(
                freeze_payload(dict(evaluation.exclusion_codes))
                if evaluation.exclusion_codes is not None
                else None
            ),
            metric_definitions=(
                freeze_payload(dict(evaluation.metric_definitions))
                if evaluation.metric_definitions is not None
                else None
            ),
            dataset_ids=(
                freeze_payload(dict(evaluation.dataset_ids))
                if evaluation.dataset_ids is not None
                else None
            ),
            warnings=(
                tuple(self._frozen_error(warning) for warning in evaluation.warnings)
                if evaluation.warnings is not None
                else None
            ),
        )

    def _frozen_observation(self, observation: Observation) -> Observation:
        """Return an engine-owned observation with read-only nested payloads."""
        return Observation(
            observation_id=observation.observation_id,
            input_id=observation.input_id,
            input_values=freeze_payload(dict(observation.input_values)),
            output_values=(
                freeze_payload(dict(observation.output_values))
                if observation.output_values is not None
                else None
            ),
            status=observation.status,
            sequence_number=observation.sequence_number,
            timestamp=observation.timestamp,
            error=self._frozen_error(observation.error) if observation.error is not None else None,
            noise_metadata=(
                freeze_payload(dict(observation.noise_metadata))
                if observation.noise_metadata is not None
                else None
            ),
            is_held_out=observation.is_held_out,
        )

    def _validate_external_observation_batch(
        self, observations: Optional[Sequence[Observation]]
    ) -> List[Observation]:
        """Validate a whole update batch before any state mutation."""
        if observations is None:
            return []
        if isinstance(observations, (str, bytes)) or isinstance(observations, Mapping):
            raise DiscoveryRunError(
                make_structured_error(
                    ERROR_INVALID_OBSERVATION,
                    "Observations must be supplied as a sequence of Observation instances.",
                    received_type=type(observations).__name__,
                )
            )
        validated: List[Observation] = []
        for observation in observations:
            error = self._validate_external_observation(observation, ERROR_INVALID_OBSERVATION)
            if error is not None:
                raise DiscoveryRunError(error)
            if any(
                existing.observation_id == observation.observation_id
                for existing in validated
            ):
                raise DiscoveryRunError(
                    make_structured_error(
                        ERROR_DUPLICATE_OBSERVATION_ID,
                        f"Observation ID {observation.observation_id!r} appears twice "
                        "in the update batch.",
                        observation_id=observation.observation_id,
                    )
                )
            if not self._config.allow_duplicate_external_observations:
                for existing in validated:
                    if self._inputs_match(observation.input_values, existing.input_values):
                        raise DiscoveryRunError(
                            make_structured_error(
                                ERROR_DUPLICATE_OBSERVATION_INPUT,
                                "Two observations in the update batch share the same input values.",
                                input_values=dict(observation.input_values),
                            )
                        )
            validated.append(observation)
        return validated

    def _inputs_match(
        self, first: Mapping[str, Any], second: Mapping[str, Any]
    ) -> bool:
        variable = self._config.domain_bounds.variable_name
        first_value = first.get(variable)
        second_value = second.get(variable)
        for value in (first_value, second_value):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return False
        try:
            return math.isclose(
                float(first_value),
                float(second_value),
                rel_tol=0.0,
                abs_tol=float(self._config.query_exclusion_tolerance),
            )
        except OverflowError:
            return False

    def _append_ingestion_record(
        self, observation: Observation, ranking_is_fresh: bool
    ) -> DiscoveryIngestionRecord:
        record = DiscoveryIngestionRecord(
            sequence=len(self._ingestion_history) + 1,
            observation_id=observation.observation_id,
            input_values=dict(observation.input_values),
            ranking_is_fresh=ranking_is_fresh,
            evaluation_count=len(self._evaluations),
        )
        self._ingestion_history.append(record)
        return record

    def _validate_numeric_payload(
        self,
        payload: Any,
        variable: str,
        label: str,
        code: str,
    ) -> Optional[StructuredError]:
        if not isinstance(payload, Mapping):
            return make_structured_error(
                code,
                f"Observation {label}_values must be a mapping.",
                received_type=type(payload).__name__,
            )
        if variable not in payload:
            return make_structured_error(
                code,
                f"Observation {label}_values is missing required variable {variable!r}.",
                required_variable=variable,
            )
        value = payload[variable]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return make_structured_error(
                code,
                f"Observation {label} value for {variable!r} must be a real number.",
                variable=variable,
                received_type=type(value).__name__,
            )
        try:
            numeric = float(value)
        except OverflowError:
            return make_structured_error(
                code,
                f"Observation {label} value for {variable!r} overflows float.",
                variable=variable,
            )
        if not math.isfinite(numeric):
            return make_structured_error(
                code,
                f"Observation {label} value for {variable!r} must be finite.",
                variable=variable,
            )
        return None

    def _validate_oracle_response(
        self, observation: Any, requested_input: ExperimentInput
    ) -> Optional[StructuredError]:
        if not isinstance(observation, Observation):
            return make_structured_error(
                ERROR_INVALID_ORACLE_RESPONSE,
                "Oracle must return an Observation instance.",
                received_type=type(observation).__name__,
            )
        if observation.status != ObservationStatus.SUCCESS:
            cause_code = (
                observation.error.code
                if (observation.error is not None and getattr(observation.error, "code", None))
                else None
            )
            return make_structured_error(
                ERROR_ORACLE_QUERY_FAILURE,
                f"Oracle returned status {observation.status.value!r}; the response is not "
                "committed as evidence.",
                status=observation.status.value,
                cause_code=cause_code,
            )
        if observation.observation_id in self._observations:
            return make_structured_error(
                ERROR_INVALID_ORACLE_RESPONSE,
                "Oracle returned an observation ID that is already committed.",
                observation_id=observation.observation_id,
            )
        if observation.input_id != requested_input.input_id:
            return make_structured_error(
                ERROR_INVALID_ORACLE_RESPONSE,
                "Oracle response input_id does not match the submitted experiment.",
                expected_input_id=requested_input.input_id,
                received_input_id=observation.input_id,
            )
        input_variable = self._config.domain_bounds.variable_name
        input_error = self._validate_numeric_payload(
            observation.input_values, input_variable, "input", ERROR_INVALID_ORACLE_RESPONSE
        )
        if input_error is not None:
            return input_error
        returned_value = float(observation.input_values[input_variable])
        requested_value = float(requested_input.values[input_variable])
        if not math.isclose(returned_value, requested_value, rel_tol=1e-9, abs_tol=1e-12):
            return make_structured_error(
                ERROR_INVALID_ORACLE_RESPONSE,
                "Oracle response input values do not match the submitted experiment.",
                requested_value=requested_value,
                returned_value=returned_value,
            )
        if self._output_variable is None:
            return make_structured_error(
                ERROR_INVALID_ORACLE_RESPONSE,
                "The evaluation output variable is not initialized for this run.",
            )
        output_error = self._validate_numeric_payload(
            observation.output_values,
            self._output_variable,
            "output",
            ERROR_INVALID_ORACLE_RESPONSE,
        )
        if output_error is not None:
            return output_error
        return None

    def _is_duplicate_input(self, experiment_input: ExperimentInput) -> bool:
        return self._is_duplicate_input_values(experiment_input.values)

    def _is_duplicate_input_values(self, values: Mapping[str, Any]) -> bool:
        variable = self._config.domain_bounds.variable_name
        if variable not in values:
            return False
        try:
            candidate_value = float(values[variable])
        except (TypeError, ValueError, OverflowError):
            return False
        tolerance = float(self._config.query_exclusion_tolerance)
        for observation in self._observations.values():
            existing = observation.input_values.get(variable)
            if isinstance(existing, bool) or not isinstance(existing, (int, float)):
                continue
            try:
                if math.isclose(
                    candidate_value,
                    float(existing),
                    rel_tol=0.0,
                    abs_tol=tolerance,
                ):
                    return True
            except OverflowError:
                continue
        return False

    # ------------------------------------------------------------------
    # Evaluation, ranking, and termination criteria
    # ------------------------------------------------------------------

    def _metric_names(self) -> List[str]:
        policy = self._config.ranking_policy
        names: List[str] = []
        for name in (policy.primary_metric, policy.secondary_metric):
            if name not in names:
                names.append(name)
        return names

    def _observation_ids(self) -> List[str]:
        return list(self._observations.keys())

    def _refresh_evaluation_and_ranking(self) -> bool:
        """Recompute evaluation and ranking; commit both only on full success."""
        if not self._candidates:
            return False
        observation_ids = self._observation_ids()
        metric_names = self._metric_names()
        requests = [
            EvaluationRequest(
                hypothesis=candidate,
                observation_ids=list(observation_ids),
                held_out_observation_ids=[],
                metric_names=list(metric_names),
            )
            for candidate in self._candidates
        ]
        try:
            results = self._evaluation_pipeline.evaluate_many(
                requests, dict(self._observations), dataset_id=self._dataset_id
            )
        except Exception as exc:  # noqa: BLE001 - component boundary
            self._ranking_is_fresh = False
            self._errors.append(
                make_structured_error(
                    ERROR_EVALUATION_FAILURE,
                    f"Evaluation failed ({type(exc).__name__}); the previous results and "
                    "ranking snapshot were preserved.",
                    exception_type=type(exc).__name__,
                )
            )
            return False

        if not isinstance(results, list) or len(results) != len(requests):
            self._ranking_is_fresh = False
            self._errors.append(
                make_structured_error(
                    ERROR_EVALUATION_FAILURE,
                    "Evaluation returned an unexpected result count; the previous results and "
                    "ranking snapshot were preserved.",
                    expected_count=len(requests),
                    received_count=len(results) if isinstance(results, list) else None,
                )
            )
            return False

        evaluations: Dict[str, EvaluationResult] = {}
        expected_ids = {candidate.hypothesis_id for candidate in self._candidates}
        for result in results:
            if not isinstance(result, EvaluationResult):
                self._ranking_is_fresh = False
                self._errors.append(
                    make_structured_error(
                        ERROR_EVALUATION_FAILURE,
                        "Evaluation returned a non-EvaluationResult item; the previous results "
                        "and ranking snapshot were preserved.",
                        received_type=type(result).__name__,
                    )
                )
                return False
            if result.hypothesis_id in evaluations:
                self._ranking_is_fresh = False
                self._errors.append(
                    make_structured_error(
                        ERROR_EVALUATION_FAILURE,
                        "Evaluation returned duplicate results for one hypothesis ID; the "
                        "previous results and ranking snapshot were preserved.",
                        hypothesis_id=result.hypothesis_id,
                    )
                )
                return False
            if result.hypothesis_id not in expected_ids:
                self._ranking_is_fresh = False
                self._errors.append(
                    make_structured_error(
                        ERROR_EVALUATION_FAILURE,
                        "Evaluation returned a result for an unknown hypothesis ID; the "
                        "previous results and ranking snapshot were preserved.",
                        hypothesis_id=result.hypothesis_id,
                    )
                )
                return False
            evaluations[result.hypothesis_id] = result

        missing_ids = sorted(expected_ids - set(evaluations))
        if missing_ids:
            self._ranking_is_fresh = False
            self._errors.append(
                make_structured_error(
                    ERROR_EVALUATION_FAILURE,
                    "Evaluation did not return a result for every requested hypothesis; the "
                    "previous results and ranking snapshot were preserved.",
                    missing_hypothesis_ids=missing_ids,
                )
            )
            return False

        observations = list(self._observations.values())
        observations_by_hypothesis = {
            candidate.hypothesis_id: observations for candidate in self._candidates
        }
        try:
            ranking = self._ranker.rank_candidates(
                self._candidates,
                evaluations,
                observations_by_hypothesis,
                dataset_version=len(self._observations),
            )
        except Exception as exc:  # noqa: BLE001 - component boundary
            self._ranking_is_fresh = False
            self._errors.append(
                make_structured_error(
                    ERROR_RANKING_FAILURE,
                    f"Ranking failed ({type(exc).__name__}); the previous results and ranking "
                    "snapshot were preserved.",
                    exception_type=type(exc).__name__,
                )
            )
            return False

        self._evaluations = evaluations
        self._ranking = ranking
        self._ranking_is_fresh = True
        return True

    def _rank_without_evaluations(self) -> bool:
        """Rank unevaluated candidates so a fresh run exposes its hypothesis set."""
        try:
            ranking = self._ranker.rank_candidates(
                self._candidates,
                {},
                {candidate.hypothesis_id: [] for candidate in self._candidates},
                dataset_version=0,
            )
        except Exception as exc:  # noqa: BLE001 - component boundary
            self._record_ranking_failure(exc, prefix="Initial ranking")
            return False
        self._evaluations = {}
        self._ranking = ranking
        self._ranking_is_fresh = True
        return True

    def _record_ranking_failure(self, exc: Exception, prefix: str) -> None:
        self._ranking_is_fresh = False
        self._errors.append(
            make_structured_error(
                ERROR_RANKING_FAILURE,
                f"{prefix} failed ({type(exc).__name__}).",
                exception_type=type(exc).__name__,
            )
        )

    def _satisfied_stopping_criterion(self) -> Optional[DiscoveryTerminationReason]:
        criteria = self._config.stopping_criteria
        if not self._ranking_is_fresh or self._ranking is None:
            # A stale or absent ranking must never be declared a discovery success.
            return None

        target_count = criteria.target_plausible_candidate_count
        if target_count is not None:
            plausible = sum(
                1
                for ranked in self._ranking.ranked_candidates
                if ranked.status == PlausibilityStatus.PLAUSIBLE
            )
            if plausible >= target_count:
                return DiscoveryTerminationReason.STOPPING_CRITERION_PLAUSIBILITY_REACHED

        threshold = criteria.target_primary_metric_threshold
        if threshold is not None:
            # A low training error alone is not scientific acceptance. The
            # metric criterion applies only to the best candidate that the
            # ranking policy already accepts as PLAUSIBLE in a rankable,
            # comparable tier. PLAUSIBLE requires an explicit policy
            # performance_acceptance_threshold, so an unconfigured acceptance
            # policy can never stop a run as a success.
            accepted = next(
                (
                    ranked
                    for ranked in self._ranking.ranked_candidates
                    if ranked.status == PlausibilityStatus.PLAUSIBLE
                    and ranked.evaluation_tier
                    in (EvaluationTier.VALID_HELD_OUT, EvaluationTier.VALID_TRAINING_ONLY)
                ),
                None,
            )
            if accepted is None:
                return None
            value = (
                accepted.held_out_score
                if accepted.held_out_score is not None
                else accepted.training_score
            )
            if value is None or not math.isfinite(value):
                return None
            policy = self._config.ranking_policy
            if policy.metric_direction == "minimize":
                met = value <= float(threshold)
            else:
                met = value >= float(threshold)
            if met:
                return DiscoveryTerminationReason.STOPPING_CRITERION_METRIC_THRESHOLD_REACHED
        return None

    def _top_candidate_summary(self) -> Tuple[Optional[str], Optional[float]]:
        if self._ranking is None or not self._ranking.ranked_candidates:
            return None, None
        top = self._ranking.ranked_candidates[0]
        value = top.held_out_score if top.held_out_score is not None else top.training_score
        return top.hypothesis_id, value

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _accounting(self) -> QueryAccounting:
        with self._query_lock:
            return QueryAccounting(
                max_query_budget=self._config.max_query_budget,
                attempted_queries=self._attempted_queries,
                successful_queries=self._successful_queries,
                failed_queries=self._failed_queries,
                rejected_queries=self._rejected_queries,
                duplicate_queries=self._duplicate_queries,
                retry_count=self._retry_count,
                in_flight_queries=self._in_flight_queries,
            )

    def _limitations(self) -> Tuple[str, ...]:
        limitations = list(RUN_LIMITATIONS)
        if not self._observations:
            limitations.append(
                "No observations were available, so every candidate is UNEVALUATED; no metric "
                "or ranking claim is made."
            )
        if not self._ranking_is_fresh:
            limitations.append(
                "The latest committed observations are not reflected in the ranking snapshot."
            )
        failed_evaluations = sum(
            1
            for result in self._evaluations.values()
            if result.status == EvaluationStatus.FAILED
        )
        if failed_evaluations:
            limitations.append(
                f"{failed_evaluations} candidate evaluation(s) failed and are reported as "
                "UNEVALUABLE; they carry no metrics and are never ranked as best."
            )
        if self._config.max_query_budget == 0:
            limitations.append(
                "The configured query budget was zero, so only initial observations were used."
            )
        if self._scientist is not None:
            limitations.append(
                "Optional LLM-assisted proposals are uncalibrated conjectures; their "
                "self-reported confidence is not statistical uncertainty, and only the "
                "existing fitting/evaluation/ranking components judge them."
            )
            if any(record.fallback_applied for record in self._scientist_history):
                limitations.append(
                    "At least one LLM proposal round produced no usable hypothesis or "
                    "failed; deterministic discovery continued and the fallback is "
                    "recorded in scientist_history."
                )
        return tuple(limitations)

    def _finalize(
        self,
        status: DiscoveryLifecycleStatus,
        reason: DiscoveryTerminationReason,
    ) -> DiscoveryRunResult:
        self._status = status
        self._termination_reason = reason
        self._finished = True
        self._finished_at = utc_now_iso()
        return self._build_result(status, reason)

    def _build_result(
        self,
        status: DiscoveryLifecycleStatus,
        reason: Optional[DiscoveryTerminationReason],
    ) -> DiscoveryRunResult:
        evaluations = tuple(
            self._frozen_evaluation(self._evaluations[candidate.hypothesis_id])
            for candidate in self._candidates
            if candidate.hypothesis_id in self._evaluations
        )
        return DiscoveryRunResult(
            run_id=self._run_id,
            status=status,
            termination_reason=reason,
            config=self._config,
            iterations_completed=self._iterations_completed,
            query_accounting=self._accounting(),
            hypotheses=tuple(self._candidates),
            evaluations=evaluations,
            ranking=self._ranking,
            observations=tuple(
                self._frozen_observation(observation)
                for observation in self._observations.values()
            ),
            experiment_history=tuple(self._experiment_history),
            iteration_history=tuple(self._iteration_history),
            ingestion_history=tuple(self._ingestion_history),
            errors=tuple(self._frozen_error(error) for error in self._errors),
            warnings=tuple(self._frozen_error(warning) for warning in self._warnings),
            ranking_is_fresh=self._ranking_is_fresh,
            scientist_history=tuple(self._scientist_history),
            limitations=self._limitations(),
            started_at=self._started_at,
            finished_at=self._finished_at,
        )


__all__ = ["DiscoveryEngine", "DEFAULT_METRIC_NAMES", "RUN_LIMITATIONS"]
