/**
 * API client functions for hypothesis exploration, evidence inspection,
 * comparison, and researcher lifecycle actions.
 */

import { apiGet, apiPatch, apiPost } from './client.js';

const enc = (value) => encodeURIComponent(String(value));

/**
 * GET /runs/<run_id>/hypotheses?include_excluded=
 * Returns { hypotheses: [...], count: number }
 */
export function listRunHypotheses(runId, { includeExcluded = true, signal } = {}) {
  return apiGet(
    `/runs/${enc(runId)}/hypotheses?include_excluded=${includeExcluded ? 'true' : 'false'}`,
    { signal },
  );
}

/**
 * GET /hypotheses/<hypothesis_id>
 * Returns { hypothesis: {...} }
 */
export function getHypothesis(hypothesisId, { signal } = {}) {
  return apiGet(`/hypotheses/${enc(hypothesisId)}`, { signal });
}

/**
 * POST /runs/<run_id>/hypotheses
 * Propose a researcher-created hypothesis.
 */
export function proposeHypothesis(runId, payload, { signal } = {}) {
  return apiPost(`/runs/${enc(runId)}/hypotheses`, payload, { signal });
}

/**
 * PATCH /hypotheses/<hypothesis_id>
 * Update flags (is_pinned, is_excluded) with optimistic concurrency.
 */
export function updateHypothesisFlags(hypothesisId, payload, { signal } = {}) {
  return apiPatch(`/hypotheses/${enc(hypothesisId)}`, payload, { signal });
}

/**
 * POST /hypotheses/<hypothesis_id>/reevaluate
 * Request re-evaluation of an active candidate.
 */
export function reevaluateHypothesis(hypothesisId, { signal } = {}) {
  return apiPost(`/hypotheses/${enc(hypothesisId)}/reevaluate`, undefined, { signal });
}

/**
 * GET /hypotheses/<hypothesis_id>/evaluations
 * Retrieve evaluation history.
 */
export function getHypothesisEvaluations(hypothesisId, { signal } = {}) {
  return apiGet(`/hypotheses/${enc(hypothesisId)}/evaluations`, { signal });
}

/**
 * GET /hypotheses/<hypothesis_id>/revisions
 * Retrieve revision history.
 */
export function getHypothesisRevisions(hypothesisId, { signal } = {}) {
  return apiGet(`/hypotheses/${enc(hypothesisId)}/revisions`, { signal });
}

/**
 * GET /hypotheses/<hypothesis_id>/evidence
 * Retrieve classified supporting and contradicting observations and plausibility status.
 */
export function getHypothesisEvidence(hypothesisId, { signal } = {}) {
  return apiGet(`/hypotheses/${enc(hypothesisId)}/evidence`, { signal });
}

/**
 * GET /runs/<run_id>/observations
 * Retrieve observations for a run.
 */
export function listRunObservations(runId, { limit = 100, offset = 0, signal } = {}) {
  const params = new URLSearchParams();
  params.set('limit', String(limit));
  params.set('offset', String(offset));
  return apiGet(`/runs/${enc(runId)}/observations?${params.toString()}`, { signal });
}
