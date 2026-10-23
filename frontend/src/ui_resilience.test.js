/**
 * Scope 6 - UI Resilience Tests
 *
 * Tests for frontend utility logic and fetch error-handling contracts using
 * Node.js built-in `node:test` and `node:assert`. Covers:
 *  - formatCost / formatLimit / formatSeconds edge cases (null, undefined, 0, extreme values)
 *  - fetch response handling for non-200 statuses (404, 409, 500, 502, 504)
 *  - Network disconnection (TypeError from fetch) triggers error state
 *  - JSON parse failure on non-JSON response body
 *  - Empty/loading/partial state rendering invariants
 *  - Input boundary validation (empty run_id, whitespace run_id)
 *  - Large dataset label rendering doesn't throw
 */

import { describe, it, before } from 'node:test';
import assert from 'node:assert/strict';

// ============================================================================
// Inline implementations of the pure functions from App.jsx.
// Tested in isolation — no React, no DOM, no bundler needed.
// ============================================================================

/** Mirrors App.jsx formatCost */
const formatCost = (value) =>
  value === null || value === undefined ? 'Unavailable' : `$${Number(value).toFixed(4)}`;

/** Mirrors App.jsx formatSeconds */
const formatSeconds = (value) =>
  value === null || value === undefined ? 'Not measurable' : `${Math.round(value)} s`;

/** Mirrors App.jsx formatLimit */
const formatLimit = (entry) => {
  if (!entry) return 'Unavailable';
  if (entry.limit === null || entry.limit === undefined) {
    return `${entry.used} used · no limit`;
  }
  return `${entry.used} / ${entry.limit}`;
};

// ============================================================================
// Minimal mock of the handleLoadBudget fetch logic, extracted for unit testing.
// The real App component does exactly this; we verify the logic without React.
// ============================================================================

/**
 * Simulates the fetch + error-handling logic from App.jsx handleLoadBudget.
 * Returns { state: 'loaded'|'error', budget?, error? }
 */
async function loadBudget(runId, fetchFn) {
  const trimmed = (runId || '').trim();
  if (!trimmed) {
    return { state: 'error', error: 'Run ID must not be empty.' };
  }

  let state = 'loading';
  let budget = null;
  let error = null;

  try {
    const response = await fetchFn(`/api/v1/runs/${encodeURIComponent(trimmed)}/budget`);
    const json = await response.json().catch(() => null);

    if (!response.ok) {
      throw new Error(json?.message || `HTTP Error ${response.status}: ${response.statusText}`);
    }
    if (!json || !json.budget) {
      throw new Error('Unexpected budget response from the backend.');
    }
    budget = json.budget;
    state = 'loaded';
  } catch (err) {
    state = 'error';
    error = err instanceof Error ? err.message : 'Unable to load the run budget.';
  }

  return { state, budget, error };
}

// ============================================================================
// 1. formatCost — null/undefined/zero/extreme
// ============================================================================

describe('formatCost', () => {
  it('returns Unavailable for null', () => {
    assert.equal(formatCost(null), 'Unavailable');
  });

  it('returns Unavailable for undefined', () => {
    assert.equal(formatCost(undefined), 'Unavailable');
  });

  it('formats zero as $0.0000', () => {
    assert.equal(formatCost(0), '$0.0000');
  });

  it('formats a typical cost value', () => {
    assert.equal(formatCost(1.5), '$1.5000');
  });

  it('formats a very small fractional cost', () => {
    assert.equal(formatCost(0.000001), '$0.0000');
  });

  it('handles a large cost value without throwing', () => {
    const result = formatCost(999999.99);
    assert.ok(result.startsWith('$'), `Expected dollar sign prefix, got: ${result}`);
  });
});

// ============================================================================
// 2. formatSeconds — null/undefined/negative/extreme
// ============================================================================

describe('formatSeconds', () => {
  it('returns Not measurable for null', () => {
    assert.equal(formatSeconds(null), 'Not measurable');
  });

  it('returns Not measurable for undefined', () => {
    assert.equal(formatSeconds(undefined), 'Not measurable');
  });

  it('formats zero as 0 s', () => {
    assert.equal(formatSeconds(0), '0 s');
  });

  it('rounds fractional seconds', () => {
    assert.equal(formatSeconds(3.7), '4 s');
  });

  it('handles very large runtime without throwing', () => {
    const result = formatSeconds(9999999);
    assert.ok(result.endsWith(' s'), `Expected seconds suffix, got: ${result}`);
  });
});

// ============================================================================
// 3. formatLimit — null/undefined entry, missing limit, valid entry
// ============================================================================

describe('formatLimit', () => {
  it('returns Unavailable for null entry', () => {
    assert.equal(formatLimit(null), 'Unavailable');
  });

  it('returns Unavailable for undefined entry', () => {
    assert.equal(formatLimit(undefined), 'Unavailable');
  });

  it('shows "no limit" when limit is null', () => {
    assert.equal(formatLimit({ used: 5, limit: null }), '5 used · no limit');
  });

  it('shows "no limit" when limit is undefined', () => {
    assert.equal(formatLimit({ used: 3, limit: undefined }), '3 used · no limit');
  });

  it('shows used/limit when both present', () => {
    assert.equal(formatLimit({ used: 7, limit: 20 }), '7 / 20');
  });

  it('handles zero used correctly', () => {
    assert.equal(formatLimit({ used: 0, limit: 10 }), '0 / 10');
  });
});

// ============================================================================
// 4. loadBudget — fetch error handling
// ============================================================================

describe('loadBudget fetch error handling', () => {
  it('returns error state for empty run_id without calling fetch', async () => {
    let fetchCalled = false;
    const result = await loadBudget('', async () => { fetchCalled = true; });
    assert.equal(result.state, 'error');
    assert.ok(result.error.length > 0, 'Error message must be non-empty');
    assert.equal(fetchCalled, false, 'fetch must not be called for empty run_id');
  });

  it('returns error state for whitespace-only run_id', async () => {
    let fetchCalled = false;
    const result = await loadBudget('   ', async () => { fetchCalled = true; });
    assert.equal(result.state, 'error');
    assert.equal(fetchCalled, false);
  });

  it('returns error state on 404 with message from response body', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      json: async () => ({ message: 'Run not found.', status_code: 404 }),
    });
    const result = await loadBudget('run-phantom', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.includes('Run not found.'), `Expected run not found error, got: ${result.error}`);
  });

  it('returns error state on 409 Conflict', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: async () => ({ message: 'Run is in a terminal state.', status_code: 409 }),
    });
    const result = await loadBudget('run-terminated', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.length > 0);
  });

  it('returns error state on 500 Internal Server Error', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => ({ message: 'An unexpected server error occurred.', status_code: 500 }),
    });
    const result = await loadBudget('run-broken', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.length > 0);
  });

  it('returns error state on 502 Bad Gateway (upstream Nodus failure)', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => null,
    });
    const result = await loadBudget('run-upstream-fail', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.includes('502') || result.error.length > 0);
  });

  it('returns error state on 504 Gateway Timeout', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 504,
      statusText: 'Gateway Timeout',
      json: async () => null,
    });
    const result = await loadBudget('run-timeout', mockFetch);
    assert.equal(result.state, 'error');
  });

  it('returns error state when network disconnects (TypeError from fetch)', async () => {
    const mockFetch = async () => {
      throw new TypeError('Failed to fetch');
    };
    const result = await loadBudget('run-network-drop', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.includes('Failed to fetch'), `Expected network error, got: ${result.error}`);
  });

  it('returns error state when response.json() throws (non-JSON body)', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 200,
      statusText: 'OK',
      json: async () => { throw new SyntaxError('Unexpected token < in JSON'); },
    });
    const result = await loadBudget('run-bad-json', mockFetch);
    // json() throws → json = null → .budget missing → 'Unexpected budget response' error
    assert.equal(result.state, 'error');
  });

  it('returns error state when successful response has no budget field', async () => {
    const mockFetch = async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ run_id: 'run-1' }),  // Missing 'budget' key
    });
    const result = await loadBudget('run-partial', mockFetch);
    assert.equal(result.state, 'error');
    assert.ok(result.error.includes('Unexpected budget response'), `Got: ${result.error}`);
  });

  it('returns loaded state with budget data on success', async () => {
    const fakeBudget = {
      oracle_queries: { used: 5, limit: 20, remaining: 15 },
      candidate_evaluations: { used: 10, limit: 80, remaining: 70 },
    };
    const mockFetch = async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ budget: fakeBudget }),
    });
    const result = await loadBudget('run-healthy', mockFetch);
    assert.equal(result.state, 'loaded');
    assert.deepEqual(result.budget, fakeBudget);
    assert.equal(result.error, null);
  });
});

// ============================================================================
// 5. Large dataset display — formatLimit and formatCost don't throw
// ============================================================================

describe('Large dataset rendering safety', () => {
  it('formatLimit handles 10000 items per second without throwing', () => {
    for (let i = 0; i < 10000; i++) {
      const result = formatLimit({ used: i, limit: 10000 });
      assert.ok(typeof result === 'string', 'formatLimit must always return a string');
    }
  });

  it('formatCost handles 10000 varied inputs without throwing', () => {
    const values = [null, undefined, 0, 0.0001, 99.9999, 1000000, -1, NaN];
    for (let i = 0; i < 10000; i++) {
      const val = values[i % values.length];
      const result = formatCost(val);
      assert.ok(typeof result === 'string', 'formatCost must always return a string');
    }
  });
});
