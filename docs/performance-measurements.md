# CLARITY Performance Measurement Layer

This document describes the reproducible performance-measurement architecture for CLARITY (Issue #27). It quantifies scientific accuracy, oracle-query efficiency, execution runtime, and compute cost using actual benchmark and persistence records.

---

## 1. Core Principles & Trust Boundaries

1. **Measurable Evidence Over Assumptions**: Metrics report measured empirical outcomes under documented experimental conditions. Speedups or accuracy advantages are never claimed unless demonstrated by empirical data.
2. **Evaluator Isolation**: Ground truth targets and evaluation parameters remain strictly inside the evaluator boundary. Discovery agents, selection planners, and public API serializers never receive ground-truth objects.
3. **Explicit Accounting**: Failed trials, non-converged runs, and budget-exhausted runs are explicitly recorded and preserved in individual trial outcomes and in denominators for statistical averages.
4. **Distinction of Measured Values from Estimates**:
   - Application estimates are clearly labeled with their calculation method and pricing rates.
   - Provider-reported charges are recorded only when received from the provider.
   - Missing provider cost data is reported as `unavailable` with `None` spend—it is never coerced or assumed to be $0.00.

---

## 2. Measurement Dimensions

### 2.1 Scientific Accuracy

Scientific accuracy metrics measure hypothesis discovery and prediction fidelity when reference ground truth is available.

| Metric | Units | Direction | Definition / Formula | Source |
| :--- | :--- | :--- | :--- | :--- |
| `hit_at_1` | `ratio` | `higher_is_better` | Binary (1.0 or 0.0) indicating whether the top-ranked candidate model matches the reference target within tolerance. | Ground-truth evaluator |
| `hit_at_3` | `ratio` | `higher_is_better` | Binary indicator whether target is in top-3 candidates. | Ground-truth evaluator |
| `hit_at_5` | `ratio` | `higher_is_better` | Binary indicator whether target is in top-5 candidates. | Ground-truth evaluator |
| `mrr` | `ratio` | `higher_is_better` | Mean Reciprocal Rank ($1 / \text{rank}$) of the first matching candidate. | Ground-truth evaluator |
| `held_out_mae` | `y_units` | `lower_is_better` | Mean Absolute Error $\frac{1}{N} \sum \|y_{\text{pred}} - y_{\text{true}}\|$ on reproducible held-out test points. | Evaluator held-out set |
| `held_out_rmse` | `y_units` | `lower_is_better` | Root Mean Squared Error $\sqrt{\frac{1}{N} \sum (y_{\text{pred}} - y_{\text{true}})^2}$. | Evaluator held-out set |
| `model_complexity` | `count` | `lower_is_better` | Hypothesis parameter or degree count. | Hypothesis revision |

*Every result records `sample_count`, `benchmark_version`, `dataset_version`, and `metric_definitions_version` (`1.0.0`).*

---

### 2.2 Oracle Query Efficiency

Query efficiency quantifies the experimental resource consumption needed to achieve a target accuracy threshold.

| Field | Units | Description |
| :--- | :--- | :--- |
| `queries_executed` | `queries` | Total executed oracle queries from persisted observations. *Proposed candidate experiments are tracked separately and NEVER counted as queries.* |
| `query_budget` | `queries` | Configured maximum oracle query limit for the trial. |
| `remaining_budget` | `queries` | $\max(0, \text{query\_budget} - \text{queries\_executed})$. |
| `target_reached` | `boolean` | Whether `hit_at_1` is true or held-out MAE $\le \text{success\_threshold}$. |
| `queries_to_target` | `queries` | Number of executed queries at the point target was reached (`None` if target was not reached). |
| `budget_exhausted` | `boolean` | True if the run exhausted its query budget before convergence. |
| `status` | `string` | Categorical state: `target_reached`, `budget_exhausted`, `in_progress`, or `failed`. |

---

### 2.3 Runtime & Execution Behavior

Runtime measurements use consistent boundaries, monotonic clocks, and differentiate sequential wall-clock duration from parallel task execution.

| Field | Units | Description |
| :--- | :--- | :--- |
| `total_wall_clock_ms` | `milliseconds` | Elapsed wall-clock time from run start to completion, measured via monotonic clock or timestamp delta. |
| `local_processing_ms` | `milliseconds` | Time spent executing local compute tasks and selection logic. |
| `remote_execution_ms` | `milliseconds` | Provider-reported execution duration for remote tasks (e.g. Nodus). |
| `task_execution_duration_ms` | `milliseconds` | Sum of individual task execution durations. |
| `task_queue_duration_ms` | `milliseconds` | Average queue wait time ($\text{started\_at} - \text{created\_at}$) for compute tasks. |
| `parallel_overlap_factor` | `ratio` | $\frac{\sum \text{task\_durations}}{\text{total\_wall\_clock\_ms}}$. Overlapping parallel task durations are NOT summed and presented as wall-clock time. |
| Task Counters | `count` | `tasks_total`, `tasks_submitted`, `tasks_completed`, `tasks_failed`, `tasks_timed_out`, `tasks_cancelled`. |

---

### 2.4 Compute Cost Classification

Compute costs are tracked with explicit state separation:

1. **`estimated`**: Calculated via application-side pricing model:
   - Nodus remote execution: $\$0.05$ / task minute ($\$3.00$ / compute hour).
   - Local sequential execution: $\$0.00$ / compute hour.
   - Oracle query: $\$0.01$ / executed query.
   - Candidate evaluation: $\$0.005$ / evaluation.
2. **`accruing`**: In-flight provider-reported unfinalized charges.
3. **`settled`**: Finalized provider-reported charges.
4. **`unavailable`**: Insufficient provider telemetry. Amount is explicitly `None` and accompanied by explanatory notes. It is **never** coerced to $\$0.00$.

*An estimate is never labelled as settled cost.*

---

## 3. Active vs Baseline Comparison Rules

When comparing active experiment selection against random or uniform baselines:

1. **Equivalent Conditions**: Trials run on the identical benchmark scenario, ground truth, initial observations, and query budget.
2. **Absolute Differences**: Calculated as $\text{active\_metric} - \text{baseline\_metric}$.
3. **Safe Relative Changes**:
   - For `lower_is_better` metrics: $\text{Reduction \%} = \frac{\text{baseline} - \text{active}}{\|\text{baseline}\|} \times 100\%$.
   - For `higher_is_better` metrics: $\text{Gain \%} = \frac{\text{active} - \text{baseline}}{\|\text{baseline}\|} \times 100\%$.
   - **Division by Zero Protection**: If the baseline value is $0.0$ (or either value is `None`), relative change is marked `invalid` with `relative_change_pct = None` and a reason code to prevent undefined percentages.
4. **Honest Advantage Claims**: An advantage is asserted **only** when active selection achieves a higher success rate, lower queries-to-target, or lower held-out MAE. If neither strategy converges or active does not outperform, it is explicitly reported that no advantage was demonstrated.
5. **Aggregation Policy**: Denominators for averages and success rates include all trials (successful and unsuccessful). Unsuccessful trials are never omitted.

---

## 4. REST API Endpoints

### 4.1 Trigger Performance Measurement
`POST /api/v1/benchmarks/measurements`

**Measure Existing Run Payload**:
```json
{
  "run_id": "run-12345"
}
```

**Execute Measured Benchmark Comparison Payload**:
```json
{
  "scenario_id": "polynomial_linear_benchmark",
  "seeds": [42, 100, 2026],
  "max_query_budget": 10
}
```

### 4.2 Retrieve Run Performance Measurements
`GET /api/v1/benchmarks/runs/<run_id>/measurements`

Returns complete structured metrics (accuracy, query efficiency, runtime, cost) for the specified run ID.

### 4.3 Retrieve Benchmark Measurement by ID
`GET /api/v1/benchmarks/measurements/<measurement_id>`

Returns the complete comparison report, aggregate statistics, and individual trial measurements.

### 4.4 List Historical Measurements
`GET /api/v1/benchmarks/measurements?scenario_id=...&limit=20&offset=0`

Returns paginated summaries of historical benchmark measurements.

---

## 5. Sample Redacted Measurement Payload

```json
{
  "measurement_id": "pm-comp-9a4f1b",
  "scenario_id": "polynomial_linear_benchmark",
  "scenario_version": "1.0.0",
  "paired_trials_count": 3,
  "query_budget_per_trial": 10,
  "success_threshold": 0.05,
  "active_aggregates": {
    "strategy": "active_selection",
    "total_trials": 3,
    "successful_trials": 3,
    "success_rate": 1.0,
    "mean_queries_used": 4.0,
    "std_queries_used": 0.0,
    "mean_queries_to_target": 4.0,
    "mean_held_out_mae": 0.0125,
    "hit_at_1_rate": 1.0,
    "mean_mrr": 1.0,
    "mean_wall_clock_ms": 115.4,
    "mean_estimated_cost_usd": 0.04
  },
  "baseline_aggregates": {
    "strategy": "random_baseline",
    "total_trials": 3,
    "successful_trials": 2,
    "success_rate": 0.6667,
    "mean_queries_used": 8.67,
    "std_queries_used": 1.89,
    "mean_queries_to_target": 8.0,
    "mean_held_out_mae": 0.0682,
    "hit_at_1_rate": 0.6667,
    "mean_mrr": 0.7222,
    "mean_wall_clock_ms": 178.2,
    "mean_estimated_cost_usd": 0.0867
  },
  "metric_comparisons": {
    "queries_to_target": {
      "metric_name": "queries_to_target",
      "active_value": 4.0,
      "baseline_value": 8.0,
      "units": "queries",
      "direction": "lower_is_better",
      "absolute_difference": -4.0,
      "relative_change_pct": 50.0,
      "relative_change_valid": true,
      "better_strategy": "active"
    },
    "held_out_mae": {
      "metric_name": "held_out_mae",
      "active_value": 0.0125,
      "baseline_value": 0.0682,
      "units": "y_units",
      "direction": "lower_is_better",
      "absolute_difference": -0.0557,
      "relative_change_pct": 81.67,
      "relative_change_valid": true,
      "better_strategy": "active"
    }
  },
  "efficiency_gain_ratio": "2.0x",
  "claims_advantage": true,
  "advantage_summary": "Active selection demonstrated higher success rate (100% vs 67%). Active selection required fewer queries to target (4.0 vs 8.0). Active selection achieved lower held-out MAE (0.0125 vs 0.0682)."
}
```

---

## 6. Known Limitations

1. **Ground Truth Dependency**: Quantitative accuracy metrics (`Hit@K`, held-out MAE) require reference targets. For exploratory discovery without reference targets, only query efficiency, runtime, and cost metrics are available.
2. **Local vs Remote Venue Comparability**: Local wall-clock duration cannot be compared to remote provider execution time as an indicator of provider speedup due to venue discrepancies.
3. **Provider Telemetry Latency**: When remote compute billing settlement is delayed, cost status remains `unavailable` or `accruing` until finalized metadata is delivered.
