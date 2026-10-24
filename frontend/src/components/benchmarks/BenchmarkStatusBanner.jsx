import {
  CheckCircleIcon,
  ClockIcon,
  PlayIcon,
  RefreshIcon,
  WarningIcon,
} from '../icons.jsx';
import { formatDateTime, UNAVAILABLE } from '../../utils/formatting.js';

export function BenchmarkStatusBanner({
  latestSuccessful,
  latestAttempted,
  totalRunsCount = 0,
  scenarios = [],
  selectedScenario = '',
  onSelectScenario,
  onTriggerBenchmark,
  onRefresh,
  isLoading = false,
}) {
  const attemptedStatus = latestAttempted
    ? latestAttempted.status || latestAttempted.evaluation_status || latestAttempted.accuracy?.evaluation_status || 'unknown'
    : 'none';
  const isLatestFailed = attemptedStatus === 'failed' || attemptedStatus === 'error';
  const isLatestCompleted = attemptedStatus === 'completed' || attemptedStatus === 'target_reached';
  const isNeverEvaluated = !latestSuccessful && !latestAttempted;

  return (
    <section className="panel benchmark-status-banner" aria-labelledby="benchmark-status-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">System State & Provenance</p>
          <h2 id="benchmark-status-heading" className="panel__title">Overall Benchmark Status</h2>
        </div>
        <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={onRefresh}
            disabled={isLoading}
            aria-label="Refresh benchmark data"
          >
            <RefreshIcon size={16} />
            <span>{isLoading ? 'Refreshing…' : 'Refresh'}</span>
          </button>
          <button
            type="button"
            className="button button--primary button--small"
            onClick={onTriggerBenchmark}
          >
            <PlayIcon size={14} />
            <span>Run Benchmark</span>
          </button>
        </div>
      </div>

      <div className="benchmark-status-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 'var(--space-4)', marginTop: 'var(--space-3)' }}>
        {/* Most Recent Successful Evaluation */}
        <div className="stat-card" style={{ padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-1)' }}>
            <CheckCircleIcon size={16} style={{ color: 'var(--color-success)' }} />
            <span className="stat__label" style={{ fontSize: '0.85rem', fontWeight: 600 }}>Latest Successful Evaluation</span>
          </div>
          <div className="stat__value" style={{ fontSize: '1.25rem', margin: 'var(--space-1) 0' }}>
            {latestSuccessful ? (
              <span className="badge badge--success">{latestSuccessful.scenario_id || latestSuccessful.benchmark_name}</span>
            ) : (
              <span className="badge badge--neutral">Not evaluated</span>
            )}
          </div>
          <div className="stat__hint muted" style={{ fontSize: '0.8rem' }}>
            {latestSuccessful ? (
              <>
                ID: <code className="id-text">{latestSuccessful.benchmark_id}</code>
                <br />
                {formatDateTime(latestSuccessful.executed_at || latestSuccessful.timestamp)}
              </>
            ) : (
              'No completed benchmark evaluations recorded yet'
            )}
          </div>
        </div>

        {/* Most Recent Attempted Evaluation */}
        <div className="stat-card" style={{ padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', marginBottom: 'var(--space-1)' }}>
            <ClockIcon size={16} style={{ color: 'var(--color-accent)' }} />
            <span className="stat__label" style={{ fontSize: '0.85rem', fontWeight: 600 }}>Latest Attempted Evaluation</span>
          </div>
          <div className="stat__value" style={{ fontSize: '1.25rem', margin: 'var(--space-1) 0' }}>
            {latestAttempted ? (
              <span className={`badge badge--${isLatestFailed ? 'danger' : isLatestCompleted ? 'success' : 'neutral'}`}>
                {latestAttempted.scenario_id || latestAttempted.benchmark_name || 'unspecified'}
              </span>
            ) : (
              <span className="badge badge--neutral">None</span>
            )}
          </div>
          <div className="stat__hint muted" style={{ fontSize: '0.8rem' }}>
            {latestAttempted ? (
              <>
                Status: <span className={`badge badge--${isLatestFailed ? 'danger' : isLatestCompleted ? 'success' : 'neutral'}`} style={{ fontSize: '0.72rem', padding: '0.1rem 0.4rem' }}>{attemptedStatus}</span>
                <br />
                {latestAttempted.executed_at || latestAttempted.timestamp
                  ? formatDateTime(latestAttempted.executed_at || latestAttempted.timestamp)
                  : UNAVAILABLE}
              </>
            ) : (
              'No evaluation attempts initiated'
            )}
          </div>
        </div>

        {/* Evaluation Population & Scenario Scope */}
        <div className="stat-card" style={{ padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
          <span className="stat__label" style={{ fontSize: '0.85rem', fontWeight: 600 }}>Benchmark Scope & Controls</span>
          <div style={{ marginTop: 'var(--space-2)' }}>
            <label htmlFor="benchmark-scenario-select" className="visually-hidden">Filter by Benchmark Scenario</label>
            <select
              id="benchmark-scenario-select"
              className="select select--small"
              value={selectedScenario}
              onChange={(e) => onSelectScenario(e.target.value)}
              style={{ width: '100%', marginBottom: 'var(--space-2)' }}
            >
              <option value="">All registered scenarios ({scenarios.length})</option>
              {scenarios.map((s) => (
                <option key={s.scenario_id} value={s.scenario_id}>
                  {s.scenario_id} ({s.challenge_type})
                </option>
              ))}
            </select>
          </div>
          <div className="stat__hint muted" style={{ fontSize: '0.8rem' }}>
            {totalRunsCount} recorded benchmark trial{totalRunsCount === 1 ? '' : 's'} across {scenarios.length} scenarios
          </div>
        </div>
      </div>

      {isNeverEvaluated && (
        <div className="panel-note" style={{ marginTop: 'var(--space-3)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
          <WarningIcon size={16} style={{ color: 'var(--color-warning-soft)' }} />
          <span>
            <strong>Not evaluated:</strong> No scientific discovery benchmarks have been run yet. Metrics below remain unmeasured until empirical benchmark execution occurs.
          </span>
        </div>
      )}
    </section>
  );
}
