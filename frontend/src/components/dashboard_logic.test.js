import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

/**
 * Logic extracted from App.jsx and RunDetailModal.jsx:
 * Evaluates whether polling should be active based on run list statuses.
 */
function shouldPollRuns(runs) {
  if (!Array.isArray(runs) || runs.length === 0) return false;
  return runs.some((r) => {
    const s = (r.status || '').toLowerCase();
    return s === 'running' || s === 'pending';
  });
}

/**
 * Logic determining if a run can be cancelled.
 */
function canCancelRun(run) {
  if (!run || !run.status) return false;
  const s = run.status.toLowerCase();
  const terminal = ['completed', 'failed', 'stopped_budget', 'budget_exhausted', 'cancelled'];
  return !terminal.includes(s) && s === 'running';
}

/**
 * Logic for formatting numeric metric or returning 'Unavailable'.
 * Guardrail: does not default missing to 0.
 */
function formatMetricCount(value) {
  if (value === null || value === undefined || typeof value !== 'number' || isNaN(value)) {
    return 'Unavailable';
  }
  return String(value);
}

describe('Dashboard Polling, Cancellation, and Metric Semantics', () => {
  it('activates polling when at least one run is active (running or pending)', () => {
    const runsWithRunning = [
      { run_id: 'run-1', status: 'completed' },
      { run_id: 'run-2', status: 'running' },
    ];
    assert.equal(shouldPollRuns(runsWithRunning), true);

    const runsWithPending = [
      { run_id: 'run-3', status: 'pending' },
      { run_id: 'run-4', status: 'failed' },
    ];
    assert.equal(shouldPollRuns(runsWithPending), true);
  });

  it('stops polling when all runs reach terminal states or run list is empty', () => {
    const terminalRuns = [
      { run_id: 'run-1', status: 'completed' },
      { run_id: 'run-2', status: 'stopped_budget' },
      { run_id: 'run-3', status: 'failed' },
      { run_id: 'run-4', status: 'cancelled' },
    ];
    assert.equal(shouldPollRuns(terminalRuns), false);
    assert.equal(shouldPollRuns([]), false);
  });

  it('permits cancellation strictly for running runs and disallows for terminal runs', () => {
    assert.equal(canCancelRun({ status: 'running' }), true);
    assert.equal(canCancelRun({ status: 'pending' }), false);
    assert.equal(canCancelRun({ status: 'completed' }), false);
    assert.equal(canCancelRun({ status: 'stopped_budget' }), false);
    assert.equal(canCancelRun({ status: 'failed' }), false);
    assert.equal(canCancelRun({ status: 'cancelled' }), false);
    assert.equal(canCancelRun(null), false);
  });

  it('distinguishes measured 0 from missing/unavailable counts', () => {
    // Measured zero should display as "0"
    assert.equal(formatMetricCount(0), '0');

    // Missing/null/undefined should display as "Unavailable"
    assert.equal(formatMetricCount(null), 'Unavailable');
    assert.equal(formatMetricCount(undefined), 'Unavailable');
    assert.equal(formatMetricCount(NaN), 'Unavailable');
    assert.equal(formatMetricCount(''), 'Unavailable');
  });
});
