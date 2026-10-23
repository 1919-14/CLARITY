/**
 * Fixtures shaped exactly like the Flask v1 serializers.
 *
 * These exist only for isolated component tests. Production views always read
 * the live API; there is no mock mode in the application itself.
 */

export function makeRun(overrides = {}) {
  return {
    run_id: 'run-4c1b0e7a2d9f',
    name: 'Nocturnal growth response',
    domain: 'default',
    status: 'running',
    execution_mode: 'local',
    sampling_strategy: 'active_disagreement',
    stop_reason: null,
    max_queries: 10,
    max_evaluations: 100,
    max_runtime_seconds: 3600,
    spend_ceiling_usd: null,
    max_concurrent_remote_tasks: 4,
    max_generated_hypotheses: null,
    budget: {
      max_oracle_queries: 10,
      max_candidate_evaluations: 100,
      max_concurrent_remote_tasks: 4,
      max_runtime_seconds: 3600,
      max_generated_hypotheses: null,
      max_compute_spend_usd: null,
    },
    created_at: '2026-10-24T08:00:00+00:00',
    started_at: '2026-10-24T08:00:05+00:00',
    completed_at: null,
    ...overrides,
  };
}

export function makeProgress(overrides = {}) {
  return {
    run_id: 'run-4c1b0e7a2d9f',
    status: 'running',
    phase: 'candidate_evaluation',
    queries_used: 2,
    queries_remaining: 8,
    evaluations_completed: 5,
    evaluations_remaining: 95,
    elapsed_seconds: 42.5,
    started_at: '2026-10-24T08:00:05+00:00',
    completed_at: null,
    current_activity: 'Fitting candidate 5 of 10',
    stop_reason: null,
    failure_details: null,
    last_updated_at: '2026-10-24T08:01:00+00:00',
    ...overrides,
  };
}

export function makeBudget(overrides = {}) {
  const { cost: costOverrides, limits: limitOverrides, usage: usageOverrides, runtime: runtimeOverrides, ...rest } =
    overrides;
  const cost = {
    estimated_cost_usd: null,
    accruing_cost_usd: null,
    settled_cost_usd: null,
    data_state: 'unavailable',
    enforcement: 'application_estimate',
    provider_hard_cap_supported: false,
    ...(costOverrides || {}),
  };

  return {
    run_id: 'run-4c1b0e7a2d9f',
    schema_version: '1.0',
    execution_mode: 'local',
    status: 'running',
    limits: {
      max_oracle_queries: 10,
      max_candidate_evaluations: 100,
      max_concurrent_remote_tasks: 4,
      max_runtime_seconds: 3600,
      max_generated_hypotheses: null,
      max_compute_spend_usd: null,
      ...(limitOverrides || {}),
    },
    usage: {
      oracle_queries: { limit: 10, used: 2, remaining: 8, exhausted: false, enforcement: 'application', disabled: false },
      candidate_evaluations: { limit: 100, used: 5, remaining: 95, exhausted: false, enforcement: 'application', disabled: false },
      generated_hypotheses: { limit: null, used: 0, remaining: null, exhausted: false, enforcement: 'application', disabled: true },
      query_outcomes: { attempted: 2, completed: 2, failed: 0, released: 0 },
      evaluation_outcomes: { attempted: 5, completed: 5, failed: 0 },
      active_remote_tasks: 0,
      max_concurrent_remote_tasks: 4,
      ...(usageOverrides || {}),
    },
    runtime: {
      started_at: '2026-10-24T08:00:05+00:00',
      deadline_at: '2026-10-24T09:00:05+00:00',
      elapsed_seconds: 42.5,
      remaining_seconds: 3557.5,
      ...(runtimeOverrides || {}),
    },
    cost,
    is_exhausted: false,
    stop_reason: null,
    raw_stop_reason: null,
    zero_semantics: {},
    limitations: ['No provider-enforced spending cap is available from the configured adapter.'],
    ...rest,
  };
}

export function makeHypothesis(overrides = {}) {
  return {
    hypothesis_id: 'hyp-001',
    run_id: 'run-4c1b0e7a2d9f',
    model_family: 'polynomial',
    current_revision: 2,
    version: 2,
    is_pinned: false,
    is_excluded: false,
    created_at: '2026-10-24T08:02:00+00:00',
    updated_at: '2026-10-24T08:05:00+00:00',
    latest_revision: {
      revision_id: 'rev-002',
      revision_number: 2,
      model_family: 'polynomial',
      formula: 'y = 2.5*x^2 + 1',
      parameters: { family: 'polynomial', degree: 2, coefficients: { c0: 1, c1: 0, c2: 2.5 } },
      complexity: 3,
      creation_source: 'researcher',
      previous_revision_id: 'rev-001',
      change_reason: 'curvature in the residual',
      actor: 'researcher',
      actor_id: null,
      created_at: '2026-10-24T08:05:00+00:00',
    },
    latest_evaluation: {
      evaluation_id: 'eva-001',
      revision_id: 'rev-002',
      status: 'completed',
      sample_count: 12,
      dataset_version: 12,
      dataset_snapshot_id: 'dset-abc',
      metrics: { mae: 0.045, rmse: 0.052 },
      held_out_metrics: null,
      error_message: null,
      evaluated_at: '2026-10-24T08:06:00+00:00',
    },
    ...overrides,
  };
}
