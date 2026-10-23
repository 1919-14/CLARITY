import { Link } from 'react-router-dom';

import { useRunStatus } from '../../hooks/useRunStatus.js';
import {
  formatCostUsd,
  formatInteger,
  formatLimit,
  formatRemaining,
  formatSeconds,
  formatTime,
  phaseLabel,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import { ErrorState } from '../feedback/ErrorState.jsx';
import { LoadingState } from '../feedback/LoadingState.jsx';
import { Notice } from '../feedback/Notice.jsx';
import { RefreshIcon } from '../icons.jsx';
import { Stat } from '../status/Stat.jsx';
import { StatusBadge } from '../status/StatusBadge.jsx';

/**
 * Most recent active run, refreshed with the same bounded polling used by the
 * run page. Stops automatically once the backend reports a terminal state.
 */
export function ActiveRunPanel({ runId, pollIntervalMs }) {
  const {
    run,
    progress,
    budget,
    isLoading,
    isRefreshing,
    isStale,
    error,
    refresh,
    isPolling,
  } = useRunStatus(runId, pollIntervalMs ? { pollIntervalMs } : undefined);

  if (isLoading) {
    return (
      <section className="panel panel--ornate" aria-labelledby="active-run-heading">
        <h2 id="active-run-heading" className="panel__title">Current investigation</h2>
        <LoadingState label="Reading the active run from the backend…" compact />
      </section>
    );
  }

  if (error && !run) {
    return (
      <section className="panel panel--ornate" aria-labelledby="active-run-heading">
        <h2 id="active-run-heading" className="panel__title">Current investigation</h2>
        <ErrorState
          compact
          error={error}
          title="The active run could not be refreshed"
          onRetry={refresh}
        />
      </section>
    );
  }

  if (!run) return null;

  const usage = budget?.usage;
  const cost = budget?.cost;
  const runtime = budget?.runtime;

  return (
    <section className="panel panel--ornate" aria-labelledby="active-run-heading">
      <div className="panel__header">
        <div>
          <p className="volume-label">Current investigation</p>
          <h2 id="active-run-heading" className="panel__title">{run.name}</h2>
          <p className="panel__subtitle">
            <code>{run.run_id}</code>
            <span aria-hidden="true"> · </span>
            <span>{phaseLabel(progress?.phase)}</span>
          </p>
        </div>
        <div className="panel__header-actions">
          <StatusBadge status={run.status} />
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={refresh}
            disabled={isRefreshing}
            aria-busy={isRefreshing || undefined}
          >
            <RefreshIcon size={16} />
            <span>{isRefreshing ? 'Refreshing…' : 'Refresh'}</span>
          </button>
        </div>
      </div>

      {isStale ? (
        <Notice tone="warning" title="Showing the last known state">
          The latest refresh failed. Values below are from{' '}
          {progress?.last_updated_at ? formatTime(progress.last_updated_at) : 'the previous successful read'}.
        </Notice>
      ) : null}

      {progress?.current_activity ? (
        <p className="activity-line">
          <span className="volume-label">Activity</span> {progress.current_activity}
        </p>
      ) : null}

      <dl className="stat-grid">
        <Stat
          label="Oracle queries"
          value={usage ? formatLimit(usage.oracle_queries) : UNAVAILABLE}
          hint={usage ? `remaining ${formatRemaining(usage.oracle_queries)}` : 'usage not reported'}
        />
        <Stat
          label="Candidate evaluations"
          value={usage ? formatLimit(usage.candidate_evaluations) : UNAVAILABLE}
          hint={usage ? `remaining ${formatRemaining(usage.candidate_evaluations)}` : 'usage not reported'}
        />
        <Stat
          label="Completed evaluations"
          value={
            progress && progress.evaluations_completed !== null && progress.evaluations_completed !== undefined
              ? formatInteger(progress.evaluations_completed)
              : UNAVAILABLE
          }
          hint="lifecycle progress"
        />
        <Stat
          label="Elapsed"
          value={runtime ? formatSeconds(runtime.elapsed_seconds) : formatSeconds(progress?.elapsed_seconds)}
          hint={runtime ? `remaining ${formatSeconds(runtime.remaining_seconds)}` : 'remaining unavailable'}
        />
        <Stat
          label="Estimated cost"
          value={formatCostUsd(cost?.estimated_cost_usd)}
          hint="application-side estimate, not an invoice"
        />
        <Stat
          label="Settled cost"
          value={formatCostUsd(cost?.settled_cost_usd)}
          hint={cost ? `data state: ${cost.data_state}` : 'cost data unavailable'}
        />
      </dl>

      <div className="panel__footer">
        <Link className="text-link" to={`/runs/${encodeURIComponent(run.run_id)}`}>
          Open the full run ledger
        </Link>
        <span className="poll-indicator" role="status">
          {isPolling ? 'Auto-refreshing every few seconds' : 'Polling stopped: run is no longer active'}
        </span>
      </div>
    </section>
  );
}
