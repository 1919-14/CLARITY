/**
 * Endpoint functions for discovery runs, their lifecycle state, resource
 * accounting, and the scientific workflows reachable from a run.
 *
 * Response shapes are the Flask v1 serializers documented in `contracts.js`.
 * Nothing here invents fields the backend does not return.
 */

import { apiGet, apiPost } from './client.js';

const enc = (value) => encodeURIComponent(String(value));

/** GET /runs?limit=&offset=&status= */
export function listRuns({ limit = 20, offset = 0, status = null, signal } = {}) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  if (status) params.set('status', status);
  return apiGet(`/runs?${params.toString()}`, { signal });
}

/** POST /runs */
export function createRun(payload, { signal } = {}) {
  return apiPost('/runs', payload, { signal });
}

/** GET /runs/<run_id> */
export function getRun(runId, { signal } = {}) {
  return apiGet(`/runs/${enc(runId)}`, { signal });
}

/** GET /runs/<run_id>/status */
export function getRunStatus(runId, { signal } = {}) {
  return apiGet(`/runs/${enc(runId)}/status`, { signal });
}

/** GET /runs/<run_id>/budget */
export function getRunBudget(runId, { signal } = {}) {
  return apiGet(`/runs/${enc(runId)}/budget`, { signal });
}

/** POST /runs/<run_id>/start */
export function startRun(runId, { signal } = {}) {
  return apiPost(`/runs/${enc(runId)}/start`, undefined, { signal });
}

/** POST /runs/<run_id>/cancel */
export function cancelRun(runId, { reason, signal } = {}) {
  return apiPost(
    `/runs/${enc(runId)}/cancel`,
    reason ? { reason } : undefined,
    { signal },
  );
}

/** GET /runs/<run_id>/hypotheses?include_excluded= */
export function listRunHypotheses(runId, { includeExcluded = true, signal } = {}) {
  return apiGet(
    `/runs/${enc(runId)}/hypotheses?include_excluded=${includeExcluded ? 'true' : 'false'}`,
    { signal },
  );
}

/** GET /runs/<run_id>/report */
export function getRunReport(runId, { signal } = {}) {
  return apiGet(`/runs/${enc(runId)}/report`, { signal });
}

/** POST /runs/<run_id>/report */
export function generateRunReport(runId, { signal } = {}) {
  return apiPost(`/runs/${enc(runId)}/report`, undefined, { signal });
}

export {
  getBenchmarkComparison,
  getPerformanceMeasurement,
  getRunBenchmarks,
  getRunPerformanceMeasurements,
  listBenchmarkScenarios,
  listPerformanceMeasurements,
  triggerBenchmarkComparison,
  triggerPerformanceMeasurement,
  triggerRunBenchmark,
} from './benchmarks.js';
