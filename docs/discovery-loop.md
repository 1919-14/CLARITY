# Discovery Loop (Issue #8)

The discovery loop connects CLARITY's scientific components into one repeatable
black-box run: it proposes candidate hypotheses, fits and evaluates them,
ranks them with evidence, proposes and selects experiments, queries the oracle
within a hard budget, ingests observations, and reranks.

**Code references**
- [backend/app/domain/discovery_engine.py](../backend/app/domain/discovery_engine.py) — `DiscoveryEngine` (orchestration only)
- [backend/app/domain/discovery_contracts.py](../backend/app/domain/discovery_contracts.py) — configuration, statuses, history records, planner protocol, immutable result
- [backend/tests/test_discovery_loop.py](../backend/tests/test_discovery_loop.py) — integration tests for complete runs, budgets, stopping, failures, incremental updates, isolation, and repeatability
- [backend/app/adapters/oracle/local_simulator.py](../backend/app/adapters/oracle/local_simulator.py) — deterministic local test oracle

## 1. Purpose and architecture

The engine is a coordinator. It owns lifecycle, state, query accounting, and
provenance. It does **not** reimplement any scientific algorithm:

| Responsibility | Component (issue) | Interface used |
| --- | --- | --- |
| Candidate proposal | `CandidateGenerator` (#5) | `generate() -> list[HypothesisRepresentation]` |
| Fitting + evaluation | `EvaluationPipeline` (#6) | `evaluate_many(requests, observations, dataset_id=...)` |
| Ranking + evidence | `HypothesisRanker` (#7) | `rank_candidates(candidates, evaluations, observations_by_hypothesis, dataset_version=...)` |
| Experiment proposal | `CandidateExperimentGenerator` (#9) | `generate_batch(ExperimentGenerationConfig)` |
| Experiment selection | planner boundary (this issue) | `ExperimentPlanner.select_experiment(...)` |
| Query execution | `OracleInterface` (#4) | `validate_input(...)`, `query(...)` |

```text
config + initial observations
      │
      ▼
DiscoveryEngine.run()
  ├─ CandidateGenerator.generate()                (once; stable IDs for the run)
  ├─ initial evaluation + ranking                 (skipped fitting when no observations)
  └─ loop (bounded)
       1. stopping criterion satisfied?      → TERMINATED_STOPPING_CRITERION
       2. query budget exhausted?            → TERMINATED_BUDGET_EXHAUSTED
       3. max_iterations reached?            → TERMINATED_MAX_ITERATIONS
       4. generate experiments               (all committed inputs excluded)
       5. planner selects one               (planner protocol)
       6. duplicate check → validate_input  (no budget consumed on rejection)
       7. oracle.query()                     (budget reserved first)
       8. validate response → commit         (invalid responses never committed)
       9. re-evaluate + rerank               (on successful observation)
      10. record iteration history
        ↳ no valid experiment                → TERMINATED_NO_EXPERIMENTS
        ↳ consecutive oracle failures        → FAILED
        ↳ evaluation/ranking exception       → FAILED (partial results preserved)
```

The engine is domain code: it has no Flask request objects, MySQL models,
Nodus SDK calls, or React concerns, so those integrations can be layered on
later without changing the loop.

## 2. Entry point

```python
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.discovery_contracts import (
    DiscoveryRunConfig, StoppingCriteriaConfig,
)
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.ranking_contracts import RankingPolicyConfig

config = DiscoveryRunConfig(
    max_query_budget=5,
    max_iterations=5,
    domain_bounds=NumericalDomainBounds(variable_name="x", min_val=-2.0, max_val=2.0),
    hypothesis_config=HypothesisGenerationConfig(
        min_degree=0, max_degree=3, candidate_count=6, seed=7
    ),
    ranking_policy=RankingPolicyConfig(
        min_samples_for_plausibility=3,
        performance_acceptance_threshold=1e9,
    ),
    experiment_pool_size=5,
)

result = DiscoveryEngine(PolynomialLocalOracle(), config).run()

print(result.status, result.termination_reason)
print(result.query_accounting.to_dict())
for ranked in result.ranked_hypotheses[:3]:
    print(ranked.rank, ranked.hypothesis_id, ranked.status, ranked.training_score)
```

`run(initial_observations=())` executes the whole bounded loop and returns a
`DiscoveryRunResult`. `DiscoveryEngine` instances are single-use per run.

Lifecycle: a run is mutable only until it terminates. `add_observation()` is the
active-run ingestion primitive and is rejected with `RUN_NOT_ACTIVE` after a
result has been finalized. Post-terminal updates use `resume(observations)` on a
non-failed run (see section 8); neither path calls the oracle or consumes budget.

## 3. Configuration and validation

`DiscoveryRunConfig` (frozen dataclass) fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `max_query_budget` | `10` | Hard cap on oracle **invocations**. `0` is valid. |
| `max_iterations` | `10` | Maximum outer cycles; at most one query per cycle. Must be >= 1. |
| `stopping_criteria` | `StoppingCriteriaConfig()` | Explicit scientific stop rules. |
| `domain_bounds` | `NumericalDomainBounds()` | Numeric input bounds for experiment generation. |
| `hypothesis_config` | `HypothesisGenerationConfig()` | Candidate generation settings (includes its own seed). |
| `fitting_config` | `FittingConfig()` | Fitting settings. |
| `ranking_policy` | `RankingPolicyConfig()` | Ranking/evidence policy. `PLAUSIBLE` requires an explicit `performance_acceptance_threshold`. |
| `experiment_pool_size` | `10` | Experiment candidates requested per iteration (1..500). |
| `experiment_strategy` | `grid` | `grid`, `uniform_random`, or `stratified_random`. |
| `allow_duplicate_queries` | `False` | Allow re-querying an already observed input. |
| `allow_duplicate_external_observations` | `False` | Accept an externally supplied observation whose input matches a committed one. Independent of `allow_duplicate_queries`. |
| `query_exclusion_tolerance` | `1e-5` | Pure absolute tolerance for duplicate detection. |
| `experiment_seed` | `None` | Seed forwarded to stochastic experiment strategies. |
| `dataset_id` | `None` | Evaluation dataset identity; defaults to the run ID. |
| `max_experiment_candidates_per_iteration` | `25` | Bound on proposals tried per iteration. |

Validation happens in `__post_init__`, before any oracle call. Negative
budgets, non-positive iteration limits, out-of-range pool sizes, non-finite
tolerances, wrong types, and inconsistent component configurations raise
`ValueError`. `DiscoveryEngine.__init__` additionally rejects an unsupported
ranking metric or an object without `validate_input`/`query`, raising
`DiscoveryRunError` (with a `StructuredError`).

`StoppingCriteriaConfig`:

- `target_plausible_candidate_count: Optional[int]` — stop once at least N
  ranked candidates are `PlausibilityStatus.PLAUSIBLE`.
- `target_primary_metric_threshold: Optional[float]` — stop once the best
  **scientifically accepted** candidate's primary metric (held-out preferred,
  training fallback) satisfies the bound under the ranking policy's metric
  direction. Acceptance means `PLAUSIBLE` in a rankable comparability tier,
  which itself requires the policy's explicit
  `performance_acceptance_threshold`. The target threshold can tighten
  acceptance but never replaces it.
- `consecutive_failure_limit: int = 3` — consecutive failed oracle queries
  tolerated before terminating as failed.

Neither criterion is inferred. With no configured criterion, a run never stops
because a candidate ranks first, and a policy without
`performance_acceptance_threshold` can never produce `PLAUSIBLE`. A low
held-out/training error alone therefore never terminates a run as a success.

## 4. Lifecycle and state transitions

`DiscoveryLifecycleStatus`:

| Status | Meaning |
| --- | --- |
| `INITIALIZING` | Before the first iteration. |
| `RUNNING` | Inside the iteration loop. |
| `TERMINATED_STOPPING_CRITERION` | A configured, genuinely satisfied criterion stopped the run. |
| `TERMINATED_BUDGET_EXHAUSTED` | The query budget is fully consumed (including a zero budget). |
| `TERMINATED_MAX_ITERATIONS` | The iteration bound is reached. |
| `TERMINATED_NO_EXPERIMENTS` | No valid, non-duplicate, pre-validated experiment remains. |
| `FAILED` | A component or oracle failure stopped the run. |

`DiscoveryTerminationReason` gives the machine-readable cause:
`stopping_criterion_plausibility_reached`,
`stopping_criterion_metric_threshold_reached`, `budget_exhausted`,
`max_iterations_reached`, `experiment_pool_exhausted`,
`consecutive_oracle_failures`, `component_failure`, `unrecoverable_error`.

**Deterministic precedence.** At the top of every iteration, in order:

1. A configured stopping criterion that the current fresh ranking satisfies.
2. Query budget exhausted.
3. Maximum iterations reached.
4. Generate/validate experiments; if none remain, `EXPERIMENT_POOL_EXHAUSTED`.

Component/oracle failures terminate immediately when they occur (they are not
checked at the top). This ordering guarantees a run that already has enough
initial evidence never spends an unnecessary query: budget `0` with a satisfied
criterion reports `TERMINATED_STOPPING_CRITERION`, not budget exhaustion.

## 5. Query accounting and budget semantics

`QueryAccounting` exposes four values, also in the result:

- `attempted_queries` — every `oracle.query` call.
- `successful_queries` — calls that returned a valid, committed observation.
- `failed_queries` — calls that raised or returned a failed/invalid response.
- `remaining_queries` — `max(0, max_query_budget - attempted_queries)`.

Invariants:

- Invocation is reserved **before** `oracle.query()`; no path can exceed
  `max_query_budget`.
- A raised, non-success, or contract-invalid response still consumes budget:
  the oracle was queried.
- Pre-invocation rejection (`oracle.validate_input` error), duplicate skips,
  and external ingestion (`add_observation`/`resume`) do not consume budget.
- The oracle's own observation `sequence_number` is recorded in experiment
  history, but the engine's `attempted_queries` is authoritative.
- With `max_query_budget == 0`, the oracle is never called; initial
  observations are still evaluated and ranked.

## 6. Experiment planning boundary

Issue #10/#11 own active selection. The engine depends only on:

```python
class ExperimentPlanner(Protocol):
    def select_experiment(
        self,
        candidates: Sequence[HypothesisRepresentation],
        evaluations: Mapping[str, EvaluationResult],
        observations: Sequence[Observation],
        candidate_batch: CandidateExperimentBatch,
        ranking: Optional[RankingResult],
    ) -> Optional[CandidateExperiment]: ...
```

`DeterministicOrderPlanner` is the default: the first eligible candidate in
generator order. It performs no disagreement or information-gain computation.
A planner returning `None` ends the run with `TERMINATED_NO_EXPERIMENTS`. A
planner returning a non-`CandidateExperiment` fails the run with
`INVALID_PLANNER_SELECTION`; returning a candidate outside the batch or not
eligible records a pre-invocation rejection and the engine tries the next
proposal. Per-iteration proposals are bounded by
`max_experiment_candidates_per_iteration`.

The planner receives only permitted discovery data — candidates, evaluations,
observations, the batch, and the ranking — never the oracle or evaluator data.
The default planner ignores even those inputs.

## 7. Validation, commit boundary, and failure handling

For each proposed experiment:

1. Candidate is generated by Issue #9 with **all committed inputs excluded**
   (absolute tolerance).
2. Duplicate detection re-checks the committed set; duplicates are skipped and
   recorded, never queried.
3. `oracle.validate_input` is called. A rejection is recorded as
   `REJECTED_PRE_INVOCATION` and no budget is spent.
4. Budget is reserved, then `oracle.query` is called.
5. The response is validated: it must be an `Observation`, `SUCCESS`, have a
   new `observation_id`, match the submitted `input_id` and input value, and
   carry finite numeric input/output for the model family's variables. Invalid
   responses become `INVOKED_FAILED` with a structured error and are **not**
   committed.
6. A valid observation is committed immediately, preserving its ID.
7. Evaluation and ranking are recomputed for all candidates in one atomic
   step: both are committed only when both succeed.

If step 7 raises after a committed observation:

- the observation stays committed;
- the previous `EvaluationResult` set and `RankingResult` snapshot are kept;
- `ranking_is_fresh` becomes `False`;
- a `WARNING_RANKING_STALE` is recorded in that iteration;
- the run terminates `FAILED` / `COMPONENT_FAILURE` rather than claiming a
  ranking it did not compute.

Other handled failures (each produces a structured code in `errors` or
`warnings`, never a raw stack trace):

| Situation | Code | Result |
| --- | --- | --- |
| Invalid initial observation | `INVALID_INITIAL_OBSERVATION` | `FAILED` / `UNRECOVERABLE_ERROR`, no query |
| Candidate generation error | `CANDIDATE_GENERATION_FAILURE` | `FAILED` / `UNRECOVERABLE_ERROR` |
| Experiment generation error | `EXPERIMENT_GENERATION_FAILURE` | `FAILED` / `COMPONENT_FAILURE` |
| Empty pool / all rejected / all duplicate | `EMPTY_CANDIDATE_POOL`, `DUPLICATE_QUERY`, `INVALID_PLANNER_SELECTION` | `TERMINATED_NO_EXPERIMENTS` |
| Oracle raised | `ORACLE_QUERY_FAILURE` | failed query; continues up to the consecutive limit |
| Invalid/non-success response | `INVALID_ORACLE_RESPONSE`, `ORACLE_QUERY_FAILURE` | failed query; not committed |
| Consecutive failures at limit | `CONSECUTIVE_ORACLE_FAILURES` | `FAILED` |
| Evaluation raised / wrong shape | `EVALUATION_FAILURE` | `FAILED` / `COMPONENT_FAILURE`, partial state kept |
| Ranking raised | `RANKING_FAILURE` | `FAILED` / `COMPONENT_FAILURE`, observations kept |
| Planner raised | `PLANNER_FAILURE` | `FAILED` / `COMPONENT_FAILURE` |
| Unexpected engine error | `UNEXPECTED_DISCOVERY_ERROR` | `FAILED` / `UNRECOVERABLE_ERROR` |

No automatic oracle retries exist: a retry would spend another query. Bounded
retry policy belongs to the planner/oracle adapter and its own accounting.

## 8. Incremental observation ingestion and terminal lifecycle

A terminated run is immutable by default. Two explicit paths exist:

**Active-run primitive — `add_observation(observation)`**

- requires a started run and rejects calls after finalization with
  `RUN_NOT_ACTIVE`, leaving state and the terminal result untouched;
- validates the observation, commits it, refreshes evaluation/ranking, and
  appends a `DiscoveryIngestionRecord`;
- never calls the oracle and never consumes budget.

**Post-terminal update — `resume(observations=())`**

- only for a terminated, **non-failed** run; a FAILED run raises
  `RUN_TERMINAL_FAILURE` and state is untouched;
- validates the whole batch (type, status, finite `x`/`y`, duplicate IDs,
  duplicate inputs within the batch and against committed observations) before
  any mutation;
- explicitly reopens the run (`RUNNING`, no termination reason), ingests the
  observations, refreshes evaluation/ranking, and then continues the same
  bounded loop;
- budget and iteration counters are **not** reset, so resume can never exceed
  the configured limits (a resume with no budget left refreshes evidence and
  immediately terminates as `BUDGET_EXHAUSTED`);
- preserves all existing observation and hypothesis IDs and appends one
  `DiscoveryIngestionRecord` per observation.

If recomputation fails during either path, the observation is kept, the
previous ranking snapshot is preserved, `ranking_is_fresh=False`, and the result
is `FAILED` / `COMPONENT_FAILURE`. Rejected updates raise `DiscoveryRunError`
and leave state unchanged.

Duplicate policies are intentionally separate:

- `allow_duplicate_queries` governs **oracle** re-querying only.
- `allow_duplicate_external_observations` governs acceptance of repeated
  externally supplied inputs only.

Both default to `False`, and neither flag implies the other.

## 9. Result and run-history schema

`DiscoveryRunResult` (frozen) fields:

| Field | Content |
| --- | --- |
| `run_id` | Stable run ID (also the default evaluation dataset ID). |
| `status`, `termination_reason` | Terminal state and cause; `termination_reason` is `null` only for a snapshot taken while the run is active. |
| `config` | Sanitized reproducible `DiscoveryRunConfig`. |
| `iterations_completed` | Completed outer iterations. |
| `query_accounting` | Attempted/successful/failed/remaining. |
| `hypotheses` | Candidate tuple with stable IDs. |
| `evaluations` | Latest `EvaluationResult` per candidate, candidate order. |
| `ranking` | Latest valid `RankingResult` (or `None`). |
| `observations` | Committed observations in ingestion order. |
| `experiment_history` | `ExperimentExecutionRecord` per proposal. |
| `iteration_history` | `DiscoveryIterationRecord` per completed iteration. |
| `ingestion_history` | `DiscoveryIngestionRecord` per external update. |
| `errors`, `warnings` | Structured errors and warnings. |
| `ranking_is_fresh` | Whether the ranking reflects every committed observation. |
| `limitations` | Human-readable limitations of this run. |
| `started_at`, `finished_at` | UTC ISO-8601 timestamps. |

`to_dict()` returns a JSON-compatible dictionary and runs
`verify_no_ground_truth_leakage` on the whole payload, so a forbidden key
(`ground_truth*`, `secret_*`, `true_*`, `evaluator_only*`, `oracle_internal*`)
raises `ValueError` instead of being serialized.

History records:

- `ExperimentExecutionRecord` — `sequence`, `iteration`, `experiment_id`,
  `input_id`, `input_values`, `outcome`
  (`validated|rejected_pre_invocation|skipped_duplicate|invoked_success|invoked_failed|failed_before_execution`),
  `observation_id`, `error`, `oracle_sequence_number`, `strategy`,
  `recorded_at`.
- `DiscoveryIterationRecord` — `iteration`, selected experiment/observation,
  outcome, query counters before/after, top hypothesis ID and primary metric,
  `ranking_is_fresh`, optional termination reason, warnings, timestamp.
- `DiscoveryIngestionRecord` — `sequence`, `observation_id`, `input_values`,
  `ranking_is_fresh`, `evaluation_count`, timestamp.

Events are ordered by insertion (never dictionary iteration), and no record
copies large payloads: references are IDs.

## 10. Ground-truth isolation

- The engine's only oracle dependency is the `OracleInterface` protocol
  (`validate_input`, `query`). It never imports `GroundTruthRule`, never calls
  the local oracle's `_evaluator_get_ground_truth`, and never inspects private
  attributes.
- Candidate generation, evaluation, ranking, experiment generation, and
  planning receive permitted data only. The planner call signature is asserted
  in tests to contain exactly candidates/evaluations/observations/batch/ranking.
- Results, history, errors, and warnings carry observation payloads and
  metrics only. A canary value stored in a fake oracle's private attributes
  never appears in `result.to_dict()`, errors, warnings, or planner inputs
  (`test_evaluator_only_canary_never_reaches_result_or_history`).
- `DiscoveryRunResult.to_dict()` re-verifies the denylist on the serialized
  payload.

These are dependency-wiring and data-flow guarantees inside one process, not a
security sandbox.

## 11. Running the example and tests

```bash
cd backend
python -m pytest tests/test_discovery_loop.py -q   # focused discovery-loop suite
python -m pytest -q                                # full backend regression
```

A complete local scenario using the deterministic `PolynomialLocalOracle`:

```bash
cd backend
python - <<'PY'
from app.adapters.oracle.local_simulator import PolynomialLocalOracle
from app.domain.candidate_generator import HypothesisGenerationConfig
from app.domain.discovery_contracts import DiscoveryRunConfig
from app.domain.discovery_engine import DiscoveryEngine
from app.domain.experiment_generator import NumericalDomainBounds
from app.domain.ranking_contracts import RankingPolicyConfig
import json

config = DiscoveryRunConfig(
    max_query_budget=4,
    max_iterations=4,
    domain_bounds=NumericalDomainBounds(min_val=-2.0, max_val=2.0),
    hypothesis_config=HypothesisGenerationConfig(
        min_degree=0, max_degree=3, candidate_count=6, seed=7
    ),
    ranking_policy=RankingPolicyConfig(
        min_samples_for_plausibility=3,
        performance_acceptance_threshold=1e9,
    ),
    experiment_pool_size=5,
)
result = DiscoveryEngine(PolynomialLocalOracle(), config).run()
print(result.status.value, result.termination_reason.value)
print(json.dumps(result.query_accounting.to_dict()))
for ranked in result.ranked_hypotheses:
    print(ranked.rank, ranked.hypothesis_id, ranked.status.value,
          ranked.training_score, ranked.evidence_summary.supporting_count)
PY
```

## 12. Current limitations and deferred integration

- **Training-only metrics.** The engine does not reserve a held-out partition;
  every committed observation is training data for fitting and evidence, so
  `held_out_metrics` is unavailable and candidates rank in the
  `VALID_TRAINING_ONLY` tier. The evaluation pipeline already supports explicit
  held-out partitions for benchmark use.
- **Deterministic default planner.** Active disagreement/information-gain
  selection (Issues #10–#11) is not implemented. Swap in an `ExperimentPlanner`
  via the constructor.
- **Single domain shape.** The loop is numeric 1-D: one input variable from
  `NumericalDomainBounds` and the model family's scalar output. Multi-output
  and multi-variable domains are out of scope.
- **Candidate set is fixed at run start.** New observations refit and rerank
  the same hypotheses; no refinement/new-family proposals happen mid-run.
- **No automatic retries.** A failed oracle call consumes budget and either
  continues (up to `consecutive_failure_limit`) or terminates.
- **Ranking ties.** The Issue #7 sort key ends with `hypothesis_id`, which is
  run-unique, so equally scored candidates can exchange display position across
  separate runs. Metric values and experiment inputs are reproducible.
- **No persistence or API.** MySQL (#18), Flask routes (#19), Nodus (#12), and
  React (#21/#22) integrate later against the result/history contracts.
- **`TERMINATED_COMPLETED` does not exist.** Every terminal path has an
  explicit reason by design; a run either stops for a configured reason or
  fails.
- **Failed runs are not resumable.** `resume()` rejects `FAILED` runs because a
  component failure may leave evaluation/ranking state intentionally stale;
  start a new run with the accumulated observations instead.
- **Active-run `add_observation` is a primitive.** Because `run()` is
  synchronous, normal incremental updates after a run use `resume()`; the
  active-run guard exists for a future step/pause lifecycle and is enforced and
  tested now.
