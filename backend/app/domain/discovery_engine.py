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
from typing import Any, Dict, List, Optional, Tuple

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
    StructuredError,
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
    WARNING_RANKING_STALE,
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
from .ranking_contracts import EvaluationTier, PlausibilityStatus, RankingResult
from .ranking_engine import HypothesisRanker

DEFAULT_METRIC_NAMES: Tuple[str, ...] = ("mae", "rmse")

RUN_LIMITATIONS: Tuple[str, ...] = (
    "All committed observations are used as training data; the engine does not "
    "reserve a held-out partition, so held-out metrics are reported as unavailable.",
    "The default planner selects the first eligible experiment in generator order. "
    "Active disagreement/information-gain selection is deferred to Issues #10-#11.",
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
        run_id: Optional[str] = None,
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
        self._consecutive_failures = 0
        self._iterations_completed = 0

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
        return tuple(self._observations.values())

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
    # Public lifecycle
    # ------------------------------------------------------------------

    def run(self, initial_observations: Sequence[Observation] = ()) -> DiscoveryRunResult:
        """Execute one bounded discovery run and return its result snapshot.

        ``initial_observations`` must be permitted observations already
        available to the discovery engine. They never consume query budget.
        A configuration object that cannot even be constructed raises
        ``DiscoveryRunError``; any failure after construction (including
        invalid initial observations or candidate generation failure) is
        reported inside the returned result as FAILED, before any oracle call.
        """
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

        try:
            self._initialize_candidates()
            self._ingest_initial_observations(initial_observations)
        except DiscoveryRunError as exc:
            self._errors.append(exc.error)
            return self._finalize(
                DiscoveryLifecycleStatus.FAILED,
                DiscoveryTerminationReason.UNRECOVERABLE_ERROR,
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

        self._observations[observation.observation_id] = observation
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
            self._observations[observation.observation_id] = observation

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
            self._observations[observation.observation_id] = observation

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

            if execution is None:
                return self._finalize(
                    DiscoveryLifecycleStatus.TERMINATED_NO_EXPERIMENTS,
                    DiscoveryTerminationReason.EXPERIMENT_POOL_EXHAUSTED,
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
                warnings=tuple(iteration_warnings),
            )
            self._iteration_history.append(record)

            if not refresh_ok:
                return self._finalize(
                    DiscoveryLifecycleStatus.FAILED,
                    DiscoveryTerminationReason.COMPONENT_FAILURE,
                )

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
            remaining = [
                candidate
                for candidate in batch.candidates
                if candidate.candidate_id not in tried
            ]
            if not remaining:
                break
            selected = self._select_experiment(remaining)
            if selected is None:
                break
            if selected.candidate_id in tried:
                break
            tried.append(selected.candidate_id)

            in_batch = any(
                candidate.candidate_id == selected.candidate_id for candidate in batch.candidates
            )
            if not in_batch or selected.status != CandidateExperimentStatus.ELIGIBLE:
                self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION,
                    error=make_structured_error(
                        ERROR_INVALID_PLANNER_SELECTION,
                        "Planner selected a candidate that is not eligible in the generated batch.",
                        experiment_id=selected.candidate_id,
                    ),
                )
                continue

            if not self._config.allow_duplicate_queries and self._is_duplicate_input(
                selected.input_data
            ):
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
                self._warnings.append(validation_error)
                self._record_experiment(
                    iteration,
                    selected,
                    ExperimentExecutionOutcome.REJECTED_PRE_INVOCATION,
                    error=validation_error,
                )
                continue

            if self._attempted_queries >= self._config.max_query_budget:
                raise _BudgetExhausted()
            return self._invoke_oracle(selected, iteration)

        return None

    def _generate_experiment_batch(self):
        exclude_inputs = [
            dict(observation.input_values) for observation in self._observations.values()
        ]
        generation_config = ExperimentGenerationConfig(
            pool_size=self._config.experiment_pool_size,
            strategy=self._config.experiment_strategy,
            bounds=self._config.domain_bounds,
            seed=self._config.experiment_seed,
            exclude_inputs=exclude_inputs,
            tolerance=self._config.query_exclusion_tolerance,
            allow_undersized=True,
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
    ) -> Optional[CandidateExperiment]:
        batch = CandidateExperimentBatch(
            candidates=list(remaining),
            requested_count=len(remaining),
            generated_count=len(remaining),
        )
        try:
            selected = self._planner.select_experiment(
                list(self._candidates),
                dict(self._evaluations),
                list(self._observations.values()),
                batch,
                self._ranking,
            )
        except Exception as exc:  # noqa: BLE001 - planner boundary
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_PLANNER_FAILURE,
                    f"Experiment planner failed ({type(exc).__name__}).",
                    exception_type=type(exc).__name__,
                )
            ) from None
        if selected is None:
            return None
        if not isinstance(selected, CandidateExperiment):
            raise _ComponentFailure(
                make_structured_error(
                    ERROR_INVALID_PLANNER_SELECTION,
                    "Experiment planner must return a CandidateExperiment or None, got "
                    f"{type(selected).__name__}.",
                    received_type=type(selected).__name__,
                )
            )
        return selected

    def _invoke_oracle(
        self, selected: CandidateExperiment, iteration: int
    ) -> ExperimentExecutionRecord:
        self._attempted_queries += 1
        try:
            observation = self._oracle.query(selected.input_data)
        except Exception as exc:  # noqa: BLE001 - oracle failures are recoverable data
            self._failed_queries += 1
            self._consecutive_failures += 1
            error = make_structured_error(
                ERROR_ORACLE_QUERY_FAILURE,
                f"Oracle query raised ({type(exc).__name__}).",
                exception_type=type(exc).__name__,
            )
            self._warnings.append(error)
            return self._record_experiment(
                iteration, selected, ExperimentExecutionOutcome.INVOKED_FAILED, error=error
            )

        validation_error = self._validate_oracle_response(observation, selected.input_data)
        if validation_error is not None:
            self._failed_queries += 1
            self._consecutive_failures += 1
            self._warnings.append(validation_error)
            return self._record_experiment(
                iteration,
                selected,
                ExperimentExecutionOutcome.INVOKED_FAILED,
                error=validation_error,
            )

        self._successful_queries += 1
        self._consecutive_failures = 0
        self._observations[observation.observation_id] = observation
        return self._record_experiment(
            iteration,
            selected,
            ExperimentExecutionOutcome.INVOKED_SUCCESS,
            observation=observation,
        )

    def _record_experiment(
        self,
        iteration: int,
        selected: CandidateExperiment,
        outcome: ExperimentExecutionOutcome,
        error: Optional[StructuredError] = None,
        observation: Optional[Observation] = None,
    ) -> ExperimentExecutionRecord:
        record = ExperimentExecutionRecord(
            sequence=len(self._experiment_history) + 1,
            iteration=iteration,
            experiment_id=selected.candidate_id,
            input_id=selected.input_data.input_id,
            input_values=dict(selected.input_data.values),
            outcome=outcome,
            observation_id=observation.observation_id if observation is not None else None,
            error=error,
            oracle_sequence_number=(
                observation.sequence_number if observation is not None else None
            ),
            strategy=(
                selected.strategy.value if selected.strategy is not None else None
            ),
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
            return make_structured_error(
                ERROR_ORACLE_QUERY_FAILURE,
                f"Oracle returned status {observation.status.value!r}; the response is not "
                "committed as evidence.",
                status=observation.status.value,
                cause=observation.error.to_dict() if observation.error is not None else None,
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
        return QueryAccounting(
            max_query_budget=self._config.max_query_budget,
            attempted_queries=self._attempted_queries,
            successful_queries=self._successful_queries,
            failed_queries=self._failed_queries,
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
            self._evaluations[candidate.hypothesis_id]
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
            observations=tuple(self._observations.values()),
            experiment_history=tuple(self._experiment_history),
            iteration_history=tuple(self._iteration_history),
            ingestion_history=tuple(self._ingestion_history),
            errors=tuple(self._errors),
            warnings=tuple(self._warnings),
            ranking_is_fresh=self._ranking_is_fresh,
            limitations=self._limitations(),
            started_at=self._started_at,
            finished_at=self._finished_at,
        )


__all__ = ["DiscoveryEngine", "DEFAULT_METRIC_NAMES", "RUN_LIMITATIONS"]
