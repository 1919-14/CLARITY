/**
 * CLARITY API Client
 * 
 * Interacts exclusively with the Flask REST API.
 * Keeps presentation components independent of HTTP networking details.
 */

const DEFAULT_BASE_URL = 'http://127.0.0.1:5000';

export function getApiBaseUrl() {
  if (typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL.replace(/\/+$/, '');
  }
  return DEFAULT_BASE_URL;
}

/**
 * Standard HTTP error object for API failures.
 */
export class ApiError extends Error {
  constructor(message, status, details = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.details = details;
  }
}

/**
 * Low-level request wrapper with error handling and timeout.
 */
async function request(endpoint, options = {}, timeoutMs = 10000) {
  const baseUrl = getApiBaseUrl();
  const url = `${baseUrl}${endpoint.startsWith('/') ? endpoint : `/${endpoint}`}`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      ...options,
      signal: controller.signal,
      headers: {
        'Accept': 'application/json',
        ...(options.headers || {}),
      },
    });

    clearTimeout(timer);

    let json = null;
    const contentType = response.headers.get('content-type');
    if (contentType && contentType.includes('application/json')) {
      try {
        json = await response.json();
      } catch (err) {
        json = null;
      }
    }

    if (!response.ok) {
      const errorMsg = json?.message || json?.error || `HTTP ${response.status}: ${response.statusText}`;
      throw new ApiError(errorMsg, response.status, json?.details || json);
    }

    return json;
  } catch (err) {
    clearTimeout(timer);
    if (err.name === 'AbortError') {
      throw new ApiError('Request timed out while waiting for backend response.', 408);
    }
    if (err instanceof ApiError) {
      throw err;
    }
    throw new ApiError(
      err.message || 'Network error or backend is unavailable.',
      0,
      { originalError: String(err) }
    );
  }
}

/**
 * CLARITY REST API methods.
 */
export const api = {
  /**
   * Health check
   */
  async getHealth() {
    return request('/api/health');
  },

  /**
   * Fetch recent discovery runs
   */
  async getRuns() {
    return request('/api/runs');
  },

  /**
   * Fetch details for a specific run
   */
  async getRun(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}`);
  },

  /**
   * Create and launch a new discovery run
   */
  async createRun(runConfig) {
    return request('/api/runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(runConfig),
    });
  },

  /**
   * Cancel an active run
   */
  async cancelRun(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}/cancel`, {
      method: 'POST',
    });
  },

  /**
   * Fetch hypotheses for a run
   */
  async getHypotheses(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}/hypotheses`);
  },

  /**
   * Update researcher preference on a hypothesis (pin/exclude)
   */
  async updateHypothesis(runId, hypothesisId, patchData) {
    return request(`/api/runs/${encodeURIComponent(runId)}/hypotheses/${encodeURIComponent(hypothesisId)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patchData),
    });
  },

  /**
   * Fetch observations for a run
   */
  async getObservations(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}/observations`);
  },

  /**
   * Fetch final scientific report for a run
   */
  async getReport(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}/report`);
  },

  /**
   * Fetch benchmark evaluation for a run
   */
  async getBenchmark(runId) {
    return request(`/api/runs/${encodeURIComponent(runId)}/benchmark`);
  },
};
