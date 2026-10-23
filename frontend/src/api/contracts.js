/**
 * Frontend view of the Flask v1 response contracts (Issues #19, #20 and #14).
 *
 * These JSDoc typedefs document the fields the UI relies on. Optional and
 * nullable fields intentionally stay nullable: `null` means the backend has no
 * measured value, and the UI must render it as unavailable rather than zero.
 *
 * ---------------------------------------------------------------------------
 * Endpoint matrix
 * ---------------------------------------------------------------------------
 * | UI workflow                | Method + path                          | Response root                     |
 * |---------------------------|----------------------------------------|-----------------------------------|
 * | Recent runs               | GET  /api/v1/runs                      | { runs[], count, limit, offset }  |
 * | Create run                | POST /api/v1/runs                      | { run }                           |
 * | Run header                | GET  /api/v1/runs/{id}                 | { run }                           |
 * | Lifecycle progress        | GET  /api/v1/runs/{id}/status          | { status }                        |
 * | Budget / resource usage   | GET  /api/v1/runs/{id}/budget          | { run_id, budget }                |
 * | Cancel run                | POST /api/v1/runs/{id}/cancel          | { run, status, message }          |
 * | Hypotheses for a run      | GET  /api/v1/runs/{id}/hypotheses      | { hypotheses[], count }           |
 * | Scientific report         | GET  /api/v1/runs/{id}/report          | { report, status? }               |
 * | Generate report           | POST /api/v1/runs/{id}/report          | { report, message }               |
 * | Benchmark results for run | GET  /api/v1/runs/{id}/benchmarks      | { benchmarks[], count }           |
 * | Benchmark scenarios       | GET  /api/v1/benchmarks/scenarios      | { scenarios[], count }            |
 *
 * Error body for every endpoint: { error, message, status_code, details? }.
 * `details` is a list of { field, code, message } for validation failures.
 */

/**
 * @typedef {Object} Run
 * @property {string} run_id
 * @property {string} name
 * @property {string} domain
 * @property {'pending'|'queued'|'running'|'cancelling'|'completed'|'cancelled'|'failed'|'budget_exhausted'|'stopped_by_budget'} status
 * @property {string} execution_mode
 * @property {string} sampling_strategy
 * @property {?string} stop_reason
 * @property {number} max_queries
 * @property {number} max_evaluations
 * @property {number} max_runtime_seconds
 * @property {?number} spend_ceiling_usd  null = no application spend ceiling
 * @property {number} max_concurrent_remote_tasks
 * @property {?number} max_generated_hypotheses
 * @property {RunBudgetConfig} budget
 * @property {?string} created_at ISO-8601
 * @property {?string} started_at
 * @property {?string} completed_at
 */

/**
 * @typedef {Object} RunBudgetConfig
 * @property {number} max_oracle_queries
 * @property {number} max_candidate_evaluations
 * @property {number} max_concurrent_remote_tasks
 * @property {number} max_runtime_seconds
 * @property {?number} max_generated_hypotheses
 * @property {?number} max_compute_spend_usd
 */

/**
 * @typedef {Object} RunProgress
 * @property {string} run_id
 * @property {string} status
 * @property {string} phase
 * @property {?number} queries_used
 * @property {?number} queries_remaining
 * @property {?number} evaluations_completed
 * @property {?number} evaluations_remaining
 * @property {?number} elapsed_seconds
 * @property {?string} started_at
 * @property {?string} completed_at
 * @property {?string} current_activity
 * @property {?string} stop_reason
 * @property {*} failure_details
 * @property {string} last_updated_at
 */

/**
 * @typedef {Object} BudgetLimit
 * @property {?number} limit   null = disabled
 * @property {number} used
 * @property {?number} remaining
 * @property {boolean} exhausted
 * @property {'application'} enforcement
 * @property {boolean} disabled
 */

/**
 * @typedef {Object} RunBudget
 * @property {string} run_id
 * @property {string} status
 * @property {string} execution_mode
 * @property {RunBudgetConfig} limits
 * @property {Object} usage
 * @property {BudgetLimit} usage.oracle_queries
 * @property {BudgetLimit} usage.candidate_evaluations
 * @property {BudgetLimit} usage.generated_hypotheses
 * @property {Object} usage.query_outcomes attempted + completed/failed/released
 * @property {Object} usage.evaluation_outcomes
 * @property {number} usage.active_remote_tasks
 * @property {number} usage.max_concurrent_remote_tasks
 * @property {Object} runtime
 * @property {?number} runtime.elapsed_seconds
 * @property {?number} runtime.remaining_seconds
 * @property {Object} cost
 * @property {?number} cost.estimated_cost_usd
 * @property {?number} cost.accruing_cost_usd
 * @property {?number} cost.settled_cost_usd
 * @property {'unavailable'|'estimated'|'accruing'|'settled'|string} cost.data_state
 * @property {string} cost.enforcement
 * @property {boolean} cost.provider_hard_cap_supported
 * @property {boolean} is_exhausted
 * @property {?string} stop_reason
 * @property {string[]} limitations
 * @property {Object<string, string>} zero_semantics
 */

/**
 * @typedef {Object} HypothesisSummary
 * @property {string} hypothesis_id
 * @property {string} model_family
 * @property {boolean} is_pinned
 * @property {boolean} is_excluded
 * @property {?{revision_number:number, formula:string, parameters:Object, complexity:number, model_family:?string}} latest_revision
 * @property {?{status:string, metrics:Object, held_out_metrics:?Object, evaluated_at:?string}} latest_evaluation
 */

/**
 * @typedef {Object} ScientificReport
 * @property {string} report_id
 * @property {string} run_id
 * @property {string} status
 * @property {Object[]} ranked_candidates
 * @property {?string} stop_reason
 * @property {string|string[]} limitations  the v1 record joins items with "; "; canonical report JSON uses an array
 * @property {?string} artifact_uri
 * @property {?string} generated_at
 */

/**
 * @typedef {Object} BenchmarkResult
 * @property {string} benchmark_id
 * @property {string} run_id
 * @property {string} benchmark_name
 * @property {?string} target_label
 * @property {?number} hit_at_1
 * @property {?number} hit_at_3
 * @property {?number} hit_at_5
 * @property {?number} reciprocal_rank
 * @property {?string} best_candidate_id
 * @property {?number} queries_to_target
 * @property {?number} held_out_mae
 * @property {?number} held_out_rmse
 * @property {string} baseline_type
 * @property {?number} efficiency_gain
 * @property {?string} executed_at
 */

export const TERMINAL_RUN_STATES = Object.freeze([
  'completed',
  'cancelled',
  'failed',
  'budget_exhausted',
  'stopped_by_budget',
]);

export const CANCELLABLE_RUN_STATES = Object.freeze(['pending', 'queued', 'running']);

export const ACTIVE_RUN_STATES = Object.freeze([
  'pending',
  'queued',
  'running',
  'cancelling',
]);

export function isTerminalRunState(state) {
  return TERMINAL_RUN_STATES.includes(String(state || '').toLowerCase());
}

export function isActiveRunState(state) {
  return ACTIVE_RUN_STATES.includes(String(state || '').toLowerCase());
}

export function isCancellableRunState(state) {
  return CANCELLABLE_RUN_STATES.includes(String(state || '').toLowerCase());
}
