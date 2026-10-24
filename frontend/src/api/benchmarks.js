/**
 * API client functions for CLARITY Agent Performance & Scientific Benchmarks.
 *
 * Implements endpoints from Issues #19, #24, #26, and #27:
 * - Scenarios: GET /benchmarks/scenarios
 * - Measurements: GET /benchmarks/measurements, GET /benchmarks/measurements/<id>, POST /benchmarks/measurements
 * - Run Measurements: GET /benchmarks/runs/<run_id>/measurements
 * - Comparisons: GET /benchmarks/compare/<comparison_id>, POST /benchmarks/compare
 * - Run Benchmarks: GET /runs/<run_id>/benchmarks, POST /runs/<run_id>/benchmarks
 */

import { apiGet, apiPost } from './client.js';

const enc = (val) => encodeURIComponent(String(val));

/** GET /benchmarks/scenarios — List versioned benchmark scenarios */
export function listBenchmarkScenarios({ signal } = {}) {
  return apiGet('/benchmarks/scenarios', { signal });
}

/** GET /benchmarks/measurements — List historical performance measurement summaries */
export function listPerformanceMeasurements({ scenarioId = null, limit = 20, offset = 0, signal } = {}) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  if (scenarioId) params.set('scenario_id', scenarioId);
  return apiGet(`/benchmarks/measurements?${params.toString()}`, { signal });
}

/** GET /benchmarks/measurements/<measurement_id> — Retrieve performance measurement record by ID */
export function getPerformanceMeasurement(measurementId, { signal } = {}) {
  return apiGet(`/benchmarks/measurements/${enc(measurementId)}`, { signal });
}

/** GET /benchmarks/runs/<run_id>/measurements — Extract performance measurements for a specific run */
export function getRunPerformanceMeasurements(runId, { signal } = {}) {
  return apiGet(`/benchmarks/runs/${enc(runId)}/measurements`, { signal });
}

/** GET /benchmarks/compare/<comparison_id> — Retrieve comparison results by ID */
export function getBenchmarkComparison(comparisonId, { signal } = {}) {
  return apiGet(`/benchmarks/compare/${enc(comparisonId)}`, { signal });
}

/** POST /benchmarks/compare — Run reproducible active vs random comparison */
export function triggerBenchmarkComparison(payload, { signal } = {}) {
  return apiPost('/benchmarks/compare', payload, { signal });
}

/** POST /benchmarks/measurements — Record or execute reproducible performance measurements */
export function triggerPerformanceMeasurement(payload, { signal } = {}) {
  return apiPost('/benchmarks/measurements', payload, { signal });
}

/** GET /runs/<run_id>/benchmarks — Retrieve benchmark results for a run */
export function getRunBenchmarks(runId, { signal } = {}) {
  return apiGet(`/runs/${enc(runId)}/benchmarks`, { signal });
}

/** POST /runs/<run_id>/benchmarks — Execute a ground-truth benchmark evaluation on a run */
export function triggerRunBenchmark(runId, payload, { signal } = {}) {
  return apiPost(`/runs/${enc(runId)}/benchmarks`, payload, { signal });
}
