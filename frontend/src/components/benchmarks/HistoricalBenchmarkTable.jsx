import { useState } from 'react';
import {
  formatDateTime,
  formatDecimal,
  formatInteger,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import { EmptyState } from '../feedback/EmptyState.jsx';
import { ErrorState } from '../feedback/ErrorState.jsx';
import { LoadingState } from '../feedback/LoadingState.jsx';
import { EyeIcon } from '../icons.jsx';

export function HistoricalBenchmarkTable({
  runs = [],
  scenarios = [],
  isLoading = false,
  error = null,
  onRetry,
  onInspectRun,
}) {
  const [scenarioFilter, setScenarioFilter] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');

  const filteredRuns = runs.filter((r) => {
    const sc = r.scenario_id || r.benchmark_name || '';
    if (scenarioFilter && sc !== scenarioFilter) return false;

    const st = r.status || r.evaluation_status || r.accuracy?.evaluation_status || 'unknown';
    if (statusFilter !== 'all' && st !== statusFilter) return false;

    return true;
  });

  return (
    <section className="panel" aria-labelledby="historical-benchmarks-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">Empirical Record Archive</p>
          <h2 id="historical-benchmarks-heading" className="panel__title">Historical Benchmark Runs</h2>
        </div>
      </div>

      <p className="panel-lead muted" style={{ fontSize: '0.9rem', marginBottom: 'var(--space-3)' }}>
        Complete auditable history of executed benchmark scenarios, trials, and comparisons.
        Each entry captures the empirical accuracy and query performance measured under documented conditions.
      </p>

      {/* Filter Toolbar */}
      <div className="toolbar" style={{ display: 'flex', gap: 'var(--space-3)', flexWrap: 'wrap', alignItems: 'center', marginBottom: 'var(--space-4)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <label htmlFor="hist-scenario-filter" className="field__inline-label">Scenario:</label>
          <select
            id="hist-scenario-filter"
            className="select select--small"
            value={scenarioFilter}
            onChange={(e) => setScenarioFilter(e.target.value)}
          >
            <option value="">All Scenarios ({scenarios.length})</option>
            {scenarios.map((s) => (
              <option key={s.scenario_id} value={s.scenario_id}>Scenario: {s.scenario_id}</option>
            ))}
          </select>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <label htmlFor="hist-status-filter" className="field__inline-label">Status:</label>
          <select
            id="hist-status-filter"
            className="select select--small"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="all">All Statuses</option>
            <option value="completed">Completed only</option>
            <option value="failed">Failed only</option>
            <option value="unknown">Unknown only</option>
          </select>
        </div>
      </div>

      {isLoading ? (
        <LoadingState label="Loading historical benchmark runs…" compact />
      ) : error ? (
        <ErrorState
          title="Historical benchmark runs could not be loaded"
          error={error}
          onRetry={onRetry}
          compact
        />
      ) : runs.length === 0 ? (
        <EmptyState
          title="No historical benchmark runs"
          message="No benchmark evaluations have been recorded yet. Absence of a record is not a zero score."
        />
      ) : filteredRuns.length === 0 ? (
        <EmptyState
          title="No historical benchmark runs match"
          message="No benchmark executions recorded for the selected scenario or status filter. Empirical runs appear here as soon as evaluations occur."
        />
      ) : (
        <div className="table-scroll">
          <table className="data-table data-table--compact">
            <caption className="visually-hidden">Historical benchmark runs ledger</caption>
            <thead>
              <tr>
                <th scope="col">Benchmark ID</th>
                <th scope="col">Scenario</th>
                <th scope="col">Hit@1</th>
                <th scope="col">MRR</th>
                <th scope="col">Held-out MAE</th>
                <th scope="col">Efficiency Gain</th>
                <th scope="col">Status</th>
                <th scope="col">Executed At</th>
                <th scope="col">Action</th>
              </tr>
            </thead>
            <tbody>
              {filteredRuns.map((item) => {
                const bId = item.benchmark_id || item.measurement_id || 'unspecified';
                const scenario = item.scenario_id || item.benchmark_name;
                const rawHit1 = item.hit_at_1 !== undefined ? item.hit_at_1 : item.accuracy?.hit_at_1?.value;
                const hit1 = rawHit1 === true ? 1.0 : rawHit1 === false ? 0.0 : rawHit1;
                const mrr = item.reciprocal_rank !== undefined ? item.reciprocal_rank : item.accuracy?.mrr?.value;
                const mae = item.held_out_mae !== undefined ? item.held_out_mae : item.accuracy?.held_out_mae?.value;
                const gain = item.efficiency_gain || item.efficiency_gain_ratio;
                const status = item.status || item.evaluation_status || item.accuracy?.evaluation_status || 'unknown';
                const executedAt = item.executed_at || item.timestamp;

                return (
                  <tr key={bId}>
                    <td>
                      <code className="id-text">{bId}</code>
                      {item.run_id && (
                        <span className="block-note muted">
                          Run: <code className="id-text" style={{ fontSize: '0.75rem' }}>{item.run_id}</code>
                        </span>
                      )}
                    </td>
                    <td>
                      <strong>{scenario}</strong>
                      {item.target_label && (
                        <span className="block-note muted">Target: {item.target_label}</span>
                      )}
                    </td>
                    <td>{hit1 !== null && hit1 !== undefined ? formatDecimal(hit1, 2) : UNAVAILABLE}</td>
                    <td>{mrr !== null && mrr !== undefined ? formatDecimal(mrr, 4) : UNAVAILABLE}</td>
                    <td>{mae !== null && mae !== undefined ? formatDecimal(mae, 4) : UNAVAILABLE}</td>
                    <td>
                      {gain ? (
                        <span className="badge badge--success">{gain}</span>
                      ) : (
                        <span className="muted">{UNAVAILABLE}</span>
                      )}
                    </td>
                    <td>
                      <span className={`status-badge status-badge--${status === 'completed' || status === 'target_reached' ? 'success' : status === 'failed' ? 'danger' : 'neutral'}`}>
                        {status}
                      </span>
                    </td>
                    <td className="muted">{formatDateTime(executedAt)}</td>
                    <td>
                      <button
                        type="button"
                        className="button button--secondary button--small"
                        onClick={() => onInspectRun(item)}
                        aria-label={`Inspect benchmark ${bId}`}
                      >
                        <EyeIcon size={14} />
                        <span>Inspect</span>
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
