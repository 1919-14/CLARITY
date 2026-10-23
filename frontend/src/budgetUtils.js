/**
 * Budget utility formatters and API fetch logic.
 * Extracted from App.jsx for testability and reuse.
 */

/**
 * Formats a numeric USD cost to 4 decimal places, or 'Unavailable' if null/undefined.
 * @param {number|string|null|undefined} value
 * @returns {string}
 */
export const formatCost = (value) =>
  value === null || value === undefined ? 'Unavailable' : `$${Number(value).toFixed(4)}`;

/**
 * Formats a duration in seconds, rounded to integer, or 'Not measurable' if null/undefined.
 * @param {number|string|null|undefined} value
 * @returns {string}
 */
export const formatSeconds = (value) =>
  value === null || value === undefined ? 'Not measurable' : `${Math.round(value)} s`;

/**
 * Formats a budget limit object { used, limit }.
 * @param {{ used: number, limit: number|null }|null|undefined} entry
 * @returns {string}
 */
export const formatLimit = (entry) => {
  if (!entry) return 'Unavailable';
  if (entry.limit === null || entry.limit === undefined) {
    return `${entry.used} used · no limit`;
  }
  return `${entry.used} / ${entry.limit}`;
};

/**
 * Fetches the budget for a run from the CLARITY backend API.
 * Supports:
 *   loadBudget(runId, fetchFn)
 *   loadBudget(baseUrl, runId)
 *   loadBudget(baseUrl, runId, fetchFn)
 *
 * @param {string} baseUrl - Base API URL (e.g. 'http://127.0.0.1:5000') or runId when called with 1-2 args
 * @param {string|Function} [runIdOrFetchFn] - Run ID or fetch implementation
 * @param {Function} [fetchFn] - Fetch implementation
 * @returns {Promise<{ state: 'loaded'|'error', budget: object|null, error: string|null }>}
 */
export async function loadBudget(baseUrl, runIdOrFetchFn, fetchFn) {
  let actualBaseUrl = baseUrl;
  let actualRunId = runIdOrFetchFn;
  let actualFetch = fetchFn;

  if (typeof actualRunId === 'function') {
    // Called as loadBudget(runId, fetchFn)
    actualFetch = actualRunId;
    actualRunId = actualBaseUrl;
    actualBaseUrl = '';
  } else if (actualRunId === undefined && typeof actualBaseUrl === 'string') {
    // Called as loadBudget(runId)
    actualRunId = actualBaseUrl;
    actualBaseUrl = '';
  }

  if (!actualFetch) {
    actualFetch = globalThis.fetch;
  }

  const trimmed = (actualRunId || '').trim();
  if (!trimmed) {
    return { state: 'error', budget: null, error: 'Run ID must not be empty.' };
  }

  const normalizedUrl = (actualBaseUrl || '').replace(/\/+$/, '');
  const endpoint = `${normalizedUrl}/api/v1/runs/${encodeURIComponent(trimmed)}/budget`;

  try {
    const response = await actualFetch(endpoint, {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });
    const json = await response.json().catch(() => null);

    if (!response.ok) {
      throw new Error(json?.message || `HTTP Error ${response.status}: ${response.statusText}`);
    }
    if (!json || !json.budget) {
      throw new Error('Unexpected budget response from the backend.');
    }
    return { state: 'loaded', budget: json.budget, error: null };
  } catch (err) {
    return {
      state: 'error',
      budget: null,
      error: err instanceof Error ? err.message : 'Unable to load the run budget.',
    };
  }
}
