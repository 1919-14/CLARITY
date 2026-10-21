# Active Experiment Selection (Issue #10)

**Code references:**
- [backend/app/domain/selection_contracts.py](../backend/app/domain/selection_contracts.py) — `SelectionStrategy`, `DisagreementMetric`, `ExperimentSelectionConfig`, `CandidateSelectionDiagnostics`, `ExperimentSelectionResult`
- [backend/app/domain/experiment_selection.py](../backend/app/domain/experiment_selection.py) — `ActiveExperimentSelector`, `ExperimentSelectionPlanner`, `RandomBaselinePlanner`, `compute_disagreement`
- [backend/tests/test_experiment_selection.py](../backend/tests/test_experiment_selection.py) — focused suite
- [docs/contracts.md](contracts.md) §8 — serialized contract summary

The selector chooses the next black-box query from a validated candidate batch by
comparing the predictions of the hypotheses still under consideration. It never
generates candidates (#9), fits or evaluates models (#6), ranks hypotheses (#7),
queries the oracle (#4), or reads evaluator-only data. It reuses the existing
`CandidateExperiment`, `HypothesisRepresentation`, `EvaluationResult`,
`Observation`, `RankingResult`, and model-family registry contracts.

## 1. Interface

```python
selector = ActiveExperimentSelector(
    ExperimentSelectionConfig(...),
    registry=default_registry(),   # optional; existing family registry
)
result = selector.select(
    candidates,        # Sequence[HypothesisRepresentation]
    evaluations,       # Mapping[str, EvaluationResult]
    observations,      # Sequence[Observation]  (committed inputs)
    candidate_batch,   # CandidateExperimentBatch from Issue #9
    ranking,           # RankingResult | None
)
```

`select()` returns an `ExperimentSelectionResult`; it does not raise for expected
scientific outcomes (empty pool, no usable predictions, zero disagreement). Bad
argument types raise `ValueError`.

### Planner integration (Issue #8)

`ExperimentSelectionPlanner` is a deliberate adapter:

```python
def select_experiment(...) -> Optional[CandidateExperiment]:
    ...   # unchanged Issue #8 contract

def select_experiment_with_diagnostics(...) -> ExperimentSelectionResult:
    ...
```

The planner protocol still requires only `select_experiment`, so existing
planners keep working. `DiscoveryEngine` checks for the optional structured
method and, when present, records the serialized decision in
`ExperimentExecutionRecord.selection_diagnostics`. A structured planner that
returns the wrong type fails with `INVALID_PLANNER_SELECTION`; a planner that
raises fails with `PLANNER_FAILURE`, exactly as before. Planner arguments are
unchanged (candidates, evaluations, observations, candidate batch, ranking).

`RandomBaselinePlanner(seed=...)` is the reproducible random baseline.

## 2. Configuration

`ExperimentSelectionConfig` is frozen and validated.

| Field | Default | Meaning |
| --- | --- | --- |
| `strategy` | `active_disagreement` | `active_disagreement` or `random_baseline`. |
| `disagreement_metric` | `population_variance` | `population_variance`, `sample_variance`, or `max_spread`. |
| `min_eligible_hypotheses` | `2` | Minimum hypotheses with usable predictions before a candidate is scored. Must be >= 2. |
| `exclude_queried_inputs` | `True` | Exclude candidates matching committed inputs under pure absolute tolerance. |
| `exclusion_tolerance` | `1e-5` | Absolute tolerance (`rel_tol=0.0`), matching the engine/#9 semantics. |
| `fallback_strategy` | `none` | `none` or explicit `sequential`. Random fallback is not supported. |
| `random_seed` | `None` | Required for `random_baseline`; ignored otherwise. |
| `uncertainty_weight` | `0.0` | Must be `0.0` until a validated uncertainty source exists. |
| `allow_candidate_specifications` | `False` | Explicit bootstrap opt-in (see §3). |

Invalid configuration raises `ValueError` before any selection: non-finite or
negative tolerance, `min_eligible_hypotheses < 2`, a random baseline without an
integer seed, a random baseline with a fallback, or a non-zero uncertainty
weight.

## 3. Hypothesis eligibility and prediction source

Selection uses the **whole** eligible set, never a top-K slice, so a ranking
position is never treated as scientific truth. A hypothesis is excluded from
scoring with a recorded reason when:

- it does not appear in the supplied ranking (`not_in_ranking`);
- the ranking marks it `CONTRADICTED` (`contradicted_by_evidence`) or
  `UNEVALUABLE` (`unevaluable`);
- in fitted mode, it has no `COMPLETED` evaluation (`no_completed_evaluation`),
  a `FAILED` evaluation (`evaluation_failed`), or no successful fitted
  specification (`no_fitted_specification`).

`UNCERTAIN` and `INCOMPARABLE` candidates are *not* excluded solely because of
their ranking tier: prediction validity depends on the specification, not the
metric comparability tier.

### Never mixing fitted and unfitted predictions

1. If at least `min_eligible_hypotheses` hypotheses have a successful fitted
   specification, **fitted specifications only** are used
   (`prediction_source="fitted_specification"`).
2. Otherwise, if `allow_candidate_specifications=True`, the decision switches
   uniformly to every hypothesis' own validated candidate specification
   (`prediction_source="candidate_specification"`). Fitted values from step 1
   are not mixed in.
3. Otherwise the selector stays in strict fitted mode and reports
   `INSUFFICIENT_HYPOTHESES`. The default is strict: an active planner needs
   enough initial observations to fit at least two hypotheses before it can
   select an experiment. Seed the run with `initial_observations` (or call
   `resume`) to reach that state; do not enable candidate specifications just to
   bypass evidence.

An `EvaluationResult` is only used for its fitted specification; the selector
never refits, evaluates, or modifies it. `HypothesisRepresentation.parameters`
is used only in explicit candidate mode.

## 4. Disagreement metrics

For a candidate input `x`, let `p_1..p_n` be the usable finite scalar
predictions of the `n` eligible hypotheses, `p_bar = (1/n) sum p_i`.

| Metric | Formula | Notes |
| --- | --- | --- |
| `population_variance` (default) | `(1/n) sum (p_i - p_bar)^2` | Divides by `n`. |
| `sample_variance` | `(1/(n-1)) sum (p_i - p_bar)^2` | Divides by `n - 1`; `n >= 2` enforced. |
| `max_spread` | `max(p_i) - min(p_i)` | Peak-to-peak spread. |

The sample-versus-population convention is explicit in the metric name and in
`compute_disagreement`'s docs; there is no hidden default other than population
variance. Values are scaled by their largest magnitude before squaring, so large
finite predictions do not overflow intermediate sums. If the final score is
still not finite, the candidate is reported as
`DISAGREEMENT_OVERFLOW` with `disagreement_score=None`; it is never treated as
zero or infinity in the ranking.

Scores are raw and **not normalized across the candidate pool**. Normalizing a
pool of equal scores would manufacture spread where none exists; a pool whose
maximum score is `0.0` returns `ZERO_DISAGREEMENT_POOL` instead. Raw variance is
scale-sensitive by construction: a candidate that separates predictions in a
larger output range has a larger score. Only the ordering within one metric and
one selection is meaningful, and a score is not a probability, information gain,
posterior uncertainty, or expected success rate.

Supported output type: one finite scalar numerical output from the registered
model family. Multi-output, categorical, vector, or probabilistic outputs are
reported as `unsupported_output_shape` (or the family's structured error code)
and never scored. Missing, malformed, non-finite, and overflowing predictions are
recorded per hypothesis in `excluded_predictions`; they are never replaced with
zero.

## 5. Candidates, duplicates, and outcomes

Candidates not marked `ELIGIBLE` by the Issue #9 contract are excluded with
`candidate_not_eligible`; a rejected candidate is never selected. When
`exclude_queried_inputs=True`, each candidate input is compared to committed
observation inputs with the same pure absolute tolerance used by the engine
(`rel_tol=0.0`, `abs_tol=exclusion_tolerance`). The comparison uses input values,
not generated input IDs, and is applied identically to both strategies.

| Status | Reason code | Selected? |
| --- | --- | --- |
| `selected` | `active_disagreement_max` / `random_baseline_draw` | yes |
| `fallback_selected` | `sequential_fallback` | yes, explicitly labelled |
| `no_selection` | `empty_candidate_pool`, `no_eligible_candidates`, `all_candidates_previously_queried`, `insufficient_eligible_hypotheses`, `no_usable_predictions`, `disagreement_overflow`, `zero_disagreement_pool` | no |

`no_selection` explicitly distinguishes an empty pool, a pool with no valid
candidates, an exhausted duplicate pool, too few hypotheses, and a valid but
uninformative pool. `fallback_selected` occurs only when
`fallback_strategy=sequential` is configured; it selects the first valid
candidate in canonical input order and its rationale states that it is not
disagreement-driven. A random draw is never used as a silent fallback.

## 6. Deterministic ordering and tie-breaking

Documented policy (`TIE_BREAK_POLICY`):

1. highest disagreement score;
2. lexicographically smallest canonical input key (numeric values ascending);
3. smallest `candidate_id`.

Scores tie only on exact float equality; no arbitrary rounding reorders
scientific scores. Both strategies sort the valid pool by the same canonical key,
so reversing the generated batch cannot change the winner or the serialized
diagnostics. `tied_candidate_ids` lists every candidate at the maximum score, and
`tie_break_applied` shows whether the tie-break decided the outcome.

## 7. Random baseline

`strategy=random_baseline` performs one uniform draw with a **local**
`random.Random(seed)` instance from the exact valid pool (same status filtering,
duplicate exclusions, and canonical ordering as active selection). It never
computes or uses disagreement scores (diagnostics are
`not_scored_random_baseline`), so it cannot be mistaken for an intelligent
strategy. A seed is mandatory; an empty valid pool returns the structured
no-selection result. The same seed and pool reproduce the same draw.

## 8. Uncertainty

`ExperimentSelectionResult.uncertainty` is:

```json
{
  "available": false,
  "source": null,
  "reason": "The fit and evaluation contracts expose no validated prediction-covariance, residual-variance, or interval estimate, so uncertainty is reported as unavailable. Prediction spread between hypotheses is not calibrated statistical uncertainty."
}
```

`EvaluationResult` carries metrics, sample counts, complexity, and the fitted
specification, but no residual variance, covariance matrix, or interval. The
selector therefore does **not** derive polynomial prediction variance, invent
covariance, or reinterpret disagreement as uncertainty.
`uncertainty_weight` is reserved but must be `0.0`; a future validated source can
add a documented combination at this small boundary without changing the rest of
the selector.

## 9. Example decision (actual output)

Two hypotheses `y = x` and `y = x^2`; candidates `x = 0` (they agree) and
`x = 2` (they differ). Abridged real output of
`ActiveExperimentSelector(ExperimentSelectionConfig(allow_candidate_specifications=True)).select(...)`:

```json
{
  "selection_version": "1.0.0",
  "status": "selected",
  "strategy": "active_disagreement",
  "reason_code": "active_disagreement_max",
  "rationale": "Selected candidate cand-exp-x2 at {'x': 2.0} with the maximum population_variance disagreement score (1.0) across 2 eligible hypothesis(es).",
  "candidate_pool_size": 2,
  "valid_candidate_count": 2,
  "eligible_hypothesis_count": 2,
  "eligible_hypothesis_ids": ["hyp-linear", "hyp-quadratic"],
  "excluded_hypotheses": {},
  "excluded_candidates": {},
  "candidates": [
    {
      "candidate_id": "cand-exp-x0",
      "input_values": {"x": 0.0},
      "scoring_status": "scored",
      "disagreement_score": 0.0,
      "usable_predictions": {"hyp-linear": 0.0, "hyp-quadratic": 0.0},
      "excluded_predictions": {},
      "usable_prediction_count": 2
    },
    {
      "candidate_id": "cand-exp-x2",
      "input_values": {"x": 2.0},
      "scoring_status": "scored",
      "disagreement_score": 1.0,
      "usable_predictions": {"hyp-linear": 2.0, "hyp-quadratic": 4.0},
      "excluded_predictions": {},
      "usable_prediction_count": 2
    }
  ],
  "selected_candidate_id": "cand-exp-x2",
  "selected_input_values": {"x": 2.0},
  "tie_break_policy": "highest disagreement score, then lexicographically smallest canonical input key (numeric values ascending), then smallest candidate_id",
  "tie_break_applied": false,
  "tied_candidate_ids": ["cand-exp-x2"],
  "prediction_source": "candidate_specification",
  "random_seed": null,
  "uncertainty": {"available": false, "source": null}
}
```

Input IDs are omitted for brevity; the full payload includes them. The complete
serialization is deterministic for identical inputs and configuration (no
timestamps or generated IDs are added during selection).

## 10. Discovery-loop integration

- The engine calls `select_experiment_with_diagnostics` when available and keeps
  the selected candidate, then validates it exactly as before (in-batch,
  `ELIGIBLE`, oracle `validate_input`, duplicate re-check, budget reservation).
- The serialized decision is stored in
  `ExperimentExecutionRecord.selection_diagnostics` (frozen, deep snapshot) and
  appears in `DiscoveryRunResult.to_dict()`.
- If the selector returns no candidate, the engine records a
  `EXPERIMENT_SELECTION_UNAVAILABLE` warning containing the structured reason and
  terminates with the existing `TERMINATED_NO_EXPERIMENTS` /
  `EXPERIMENT_POOL_EXHAUSTED` semantics. It never silently substitutes a random
  or sequential choice.
- Query budgets, iteration limits, duplicate-query protection, evaluation/ranking
  refresh, terminal-state protection, and immutable history/result snapshots are
  unchanged. Selection never mutates prior history or evaluation records.
- The default planner remains `DeterministicOrderPlanner`; active selection is
  opt-in through the `planner=` constructor argument.

## 11. Ground-truth isolation

`experiment_selection.py` and `selection_contracts.py` import only domain
contracts, the model registry, ranking contracts, and Issue #9/#8 data types.
They do not import `app.adapters`, `app.evaluator`, `GroundTruthRule`, or oracle
internals, and tests assert the source dependencies plus the planner argument
list. Selection diagnostics contain inputs, hypothesis IDs, predictions,
evaluation-derived reasons, and configuration only; a recording oracle in the
integration tests fails the test if `_evaluator_get_ground_truth` is ever called,
and a canary string never appears in the serialized result.

This is a dependency-wiring and data-flow guarantee inside one process, not a
security sandbox.

## 12. Tests and synthetic comparison

```bash
cd backend
python -m pytest tests/test_experiment_selection.py -q   # focused
python -m pytest -q                                      # full backend regression
```

The focused suite covers the metric formulas (population/sample/peak-to-peak),
the agree/disagree synthetic case, negative and fractional predictions, extreme
finite overflow, non-finite predictions (asserted **not** zero-filled), malformed
specifications, insufficient hypotheses, deterministic ties and batch-order
reversal, empty and rejected pools, duplicate tolerance and disabling, explicit
fallback, rank-based eligibility, strict versus opted-in prediction sources,
seeded random draws, shared valid pools, planner compatibility, engine
integration, frozen diagnostics, and ground-truth isolation.

The engine integration test runs a real discovery loop with
`PolynomialLocalOracle`, seeded initial observations, and a two-query budget, and
asserts that the experiment recorded in history is the one submitted to the
oracle and that its diagnostics survive `to_dict()`.

The random-baseline tests are **smoke tests**, not evidence of superiority:
they verify reproducible seeded draws from the same valid pool. No claim is made
that active selection wins on every problem, and no statistically meaningful
query-efficiency benchmark has been run yet.

## 13. Known limitations

- **Scalar numerical output only.** One finite scalar prediction per hypothesis;
  categorical/vector/probabilistic outputs are unsupported.
- **Strict bootstrap by default.** With fewer than two successful fits, strict
  mode returns `INSUFFICIENT_HYPOTHESES`. `allow_candidate_specifications=True`
  is an explicit, uniformly applied workaround; it is not calibrated Bayesian
  prior evidence.
- **No uncertainty signal.** Uncertainty weighting is disabled and reported
  unavailable (§8).
- **Raw scale-sensitive variance.** Comparison across different output scales or
  metrics is not meaningful; no pool normalization is applied.
- **One-step greedy choice.** The selector scores the generated batch once per
  iteration; it does not look ahead over future queries.
- **Ranking dependency.** A supplied ranking is authoritative for
  contradicted/unevaluable exclusion; a stale or missing ranking changes the
  eligible set. Missing hypotheses are excluded rather than guessed.
- **No benchmark result.** Active-versus-random performance is not established
  by the current tests.
