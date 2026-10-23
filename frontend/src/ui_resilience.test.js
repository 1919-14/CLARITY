/**
 * Scope 6 - UI Resilience Tests
 *
 * Tests for frontend utility logic and fetch error-handling contracts.
 * Converted from node:test to Vitest to work with the unified test runner.
 * Covers:
 *  - formatCost / formatLimit / formatSeconds edge cases (null, undefined, 0, extreme values)
 *  - fetch response handling for non-200 statuses (404, 409, 500, 502, 504)
 *  - Network disconnection (TypeError from fetch) triggers error state
 *  - JSON parse failure on non-JSON response body
 *  - Input boundary validation (empty run_id, whitespace run_id)
 *  - Large dataset label rendering doesn't throw
 */

import { describe, it, expect } from 'vitest';
import { formatCost, formatSeconds, formatLimit, loadBudget } from './budgetUtils.js';

// ============================================================================
// 1. formatCost — null/undefined/zero/extreme
// ============================================================================

describe('formatCost', () => {
  it('returns Unavailable for null', () => {
    expect(formatCost(null)).toBe('Unavailable');
  });

  it('returns Unavailable for undefined', () => {
    expect(formatCost(undefined)).toBe('Unavailable');
  });

  it('formats zero as $0.0000', () => {
    expect(formatCost(0)).toBe('$0.0000');
  });

  it('formats a typical cost value', () => {
    expect(formatCost(1.5)).toBe('$1.5000');
  });

  it('formats a very small fractional cost', () => {
    expect(formatCost(0.000001)).toBe('$0.0000');
  });

  it('handles a large cost value without throwing', () => {
    const result = formatCost(999999.99);
    expect(result.startsWith('$')).toBe(true);
  });
});

// ============================================================================
// 2. formatSeconds — null/undefined/zero/extreme
// ============================================================================

describe('formatSeconds', () => {
  it('returns Not measurable for null', () => {
    expect(formatSeconds(null)).toBe('Not measurable');
  });

  it('returns Not measurable for undefined', () => {
    expect(formatSeconds(undefined)).toBe('Not measurable');
  });

  it('formats zero as 0 s', () => {
    expect(formatSeconds(0)).toBe('0 s');
  });

  it('rounds fractional seconds', () => {
    expect(formatSeconds(3.7)).toBe('4 s');
  });

  it('handles very large runtime without throwing', () => {
    const result = formatSeconds(9999999);
    expect(result.endsWith(' s')).toBe(true);
  });
});

// ============================================================================
// 3. formatLimit — null/undefined entry, missing limit, valid entry
// ============================================================================

describe('formatLimit', () => {
  it('returns Unavailable for null entry', () => {
    expect(formatLimit(null)).toBe('Unavailable');
  });

  it('returns Unavailable for undefined entry', () => {
    expect(formatLimit(undefined)).toBe('Unavailable');
  });

  it('shows "no limit" when limit is null', () => {
    expect(formatLimit({ used: 5, limit: null })).toBe('5 used · no limit');
  });

  it('shows "no limit" when limit is undefined', () => {
    expect(formatLimit({ used: 3, limit: undefined })).toBe('3 used · no limit');
  });

  it('shows used/limit when both present', () => {
    expect(formatLimit({ used: 7, limit: 20 })).toBe('7 / 20');
  });

  it('handles zero used correctly', () => {
    expect(formatLimit({ used: 0, limit: 10 })).toBe('0 / 10');
  });
});

// ============================================================================
// 4. loadBudget — fetch error handling
// ============================================================================

describe('loadBudget fetch error handling', () => {
  it('returns error state for empty run_id without calling fetch', async () => {
    let fetchCalled = false;
    const result = await loadBudget('', async () => { fetchCalled = true; });
    expect(result.state).toBe('error');
    expect(result.error.length).toBeGreaterThan(0);
    expect(fetchCalled).toBe(false);
  });

  it('returns error state for whitespace-only run_id', async () => {
    let fetchCalled = false;
    const result = await loadBudget('   ', async () => { fetchCalled = true; });
    expect(result.state).toBe('error');
    expect(fetchCalled).toBe(false);
  });

  it('returns error state on 404 with message from response body', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 404,
      statusText: 'Not Found',
      json: async () => ({ message: 'Run not found.', status_code: 404 }),
    });
    const result = await loadBudget('run-phantom', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error).toContain('Run not found.');
  });

  it('returns error state on 409 Conflict', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 409,
      statusText: 'Conflict',
      json: async () => ({ message: 'Run is in a terminal state.', status_code: 409 }),
    });
    const result = await loadBudget('run-terminated', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error.length).toBeGreaterThan(0);
  });

  it('returns error state on 500 Internal Server Error', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 500,
      statusText: 'Internal Server Error',
      json: async () => ({ message: 'An unexpected server error occurred.', status_code: 500 }),
    });
    const result = await loadBudget('run-broken', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error.length).toBeGreaterThan(0);
  });

  it('returns error state on 502 Bad Gateway (upstream Nodus failure)', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 502,
      statusText: 'Bad Gateway',
      json: async () => null,
    });
    const result = await loadBudget('run-upstream-fail', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error.length).toBeGreaterThan(0);
  });

  it('returns error state on 504 Gateway Timeout', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 504,
      statusText: 'Gateway Timeout',
      json: async () => null,
    });
    const result = await loadBudget('run-timeout', mockFetch);
    expect(result.state).toBe('error');
  });

  it('returns error state when network disconnects (TypeError from fetch)', async () => {
    const mockFetch = async () => {
      throw new TypeError('Failed to fetch');
    };
    const result = await loadBudget('run-network-drop', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error).toContain('Failed to fetch');
  });

  it('returns error state when response.json() throws (non-JSON body)', async () => {
    const mockFetch = async () => ({
      ok: false,
      status: 200,
      statusText: 'OK',
      json: async () => { throw new SyntaxError('Unexpected token < in JSON'); },
    });
    const result = await loadBudget('run-bad-json', mockFetch);
    expect(result.state).toBe('error');
  });

  it('returns error state when successful response has no budget field', async () => {
    const mockFetch = async () => ({
      ok: true,
      status: 200,
      statusText: 'OK',
      json: async () => ({ run_id: 'run-1' }),
    });
    const result = await loadBudget('run-partial', mockFetch);
    expect(result.state).toBe('error');
    expect(result.error).toContain('Unexpected budget response');
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
    expect(result.state).toBe('loaded');
    expect(result.budget).toEqual(fakeBudget);
    expect(result.error).toBeNull();
  });
});

// ============================================================================
// 5. Large dataset display — formatLimit and formatCost don't throw
// ============================================================================

describe('Large dataset rendering safety', () => {
  it('formatLimit handles 10000 items per second without throwing', () => {
    for (let i = 0; i < 10000; i++) {
      const result = formatLimit({ used: i, limit: 10000 });
      expect(typeof result).toBe('string');
    }
  });

  it('formatCost handles 10000 varied inputs without throwing', () => {
    const values = [null, undefined, 0, 0.0001, 99.9999, 1000000, -1, NaN];
    for (let i = 0; i < 10000; i++) {
      const val = values[i % values.length];
      const result = formatCost(val);
      expect(typeof result).toBe('string');
    }
  });
});
