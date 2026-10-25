import { useCallback, useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';

import { isCancellableRunState } from '../api/contracts.js';
import { cancelRun, startRun } from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { Notice } from '../components/feedback/Notice.jsx';
import { ConfirmDialog } from '../components/forms/ConfirmDialog.jsx';
import { ArrowLeftIcon, BanIcon, ChartIcon, FlaskIcon, PlayIcon, RefreshIcon, ScrollIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { BudgetPanel } from '../components/status/BudgetPanel.jsx';
import { Stat } from '../components/status/Stat.jsx';
import { StatusBadge } from '../components/status/StatusBadge.jsx';
import { DEFAULT_POLL_INTERVAL_MS, useRunStatus } from '../hooks/useRunStatus.js';
import {
  executionModeLabel,
  formatDateTime,
  formatSeconds,
  formatTime,
  phaseLabel,
  statusLabel,
  UNAVAILABLE,
} from '../utils/formatting.js';

function FailureDetails({ details }) {
  if (!details) return null;
  if (typeof details === 'string') return <p>{details}</p>;
  if (Array.isArray(details)) {
    return (
      <ul className="prose-list">
        {details.map((item, index) => (
          <li key={index}>{typeof item === 'string' ? item : JSON.stringify(item)}</li>
        ))}
      </ul>
    );
  }
  if (typeof details === 'object') {
    return (
      <dl className="detail-list">
        {Object.entries(details).map(([key, value]) => (
          <div className="detail-list__row" key={key}>
            <dt>{key}</dt>
            <dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd>
          </div>
        ))}
      </dl>
    );
  }
  return <p>{String(details)}</p>;
}

export function RunDetailsPage() {
  const { runId } = useParams();
  return <RunDetailsView runId={runId} />;
}

export function RunDetailsView({ runId, pollIntervalMs }) {
  const location = useLocation();
  const {
    run,
    progress,
    budget,
    isLoading,
    isRefreshing,
    isStale,
    error,
    status,
    isTerminal,
    isPolling,
    refresh,
  } = useRunStatus(runId, pollIntervalMs ? { pollIntervalMs } : undefined);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [cancelOutcome, setCancelOutcome] = useState(null);
  const [starting, setStarting] = useState(false);
  const [startOutcome, setStartOutcome] = useState(null);

  const createdRunId = location.state?.createdRunId;

  const handleStart = useCallback(async () => {
    setStarting(true);
    setStartOutcome(null);
    try {
      await startRun(runId);
      setStartOutcome({
        tone: 'success',
        title: 'Run started successfully',
        body: 'Discovery execution dispatched. Live scientific queries and evaluations are in progress.',
      });
      refresh();
    } catch (startError) {
      setStartOutcome({
        tone: 'danger',
        title: 'The run could not be started',
        body: startError?.message || 'An unexpected error occurred while starting the run.',
      });
    } finally {
      setStarting(false);
    }
  }, [runId, refresh]);

  const handleCancel = useCallback(async () => {
    setCancelling(true);
    setCancelOutcome(null);
    try {
      const response = await cancelRun(runId);
      setCancelOutcome({
        tone: 'success',
        title: response?.message || 'Cancellation requested.',
        body: 'The backend accepted the request. Refreshing the run state from the backend.',
      });
    } catch (cancelError) {
      if (cancelError?.status === 409) {
        setCancelOutcome({
          tone: 'warning',
          title: 'Cancellation was not accepted',
          body: 'The run has already reached a terminal state. The displayed state is being refreshed from the backend.',
        });
      } else {
        setCancelOutcome({
          tone: 'danger',
          title: 'The run could not be cancelled',
          body: cancelError?.message || 'An unknown error occurred while requesting cancellation.',
        });
      }
    } finally {
      setCancelling(false);
      setDialogOpen(false);
      refresh();
    }
  }, [runId, refresh]);

  const header = (
    <PageHeader
      volume="Volume III · Run ledger"
      title={run?.name || 'Run ledger'}
      description={
        run
          ? `${run.name} · ${formatDateTime(run.created_at)} · ${executionModeLabel(run.execution_mode)}`
          : 'Lifecycle state, resource accounting and scientific outputs for one persisted run.'
      }
      actions={
        <Link to="/" className="button button--ghost">
          <ArrowLeftIcon size={16} />
          <span>Dashboard</span>
        </Link>
      }
    />
  );

  if (isLoading) {
    return (
      <div className="page-inner">
        {header}
        <OrnamentDivider />
        <LoadingState label="Reading the run from the backend…" />
      </div>
    );
  }

  if (error && !run) {
    return (
      <div className="page-inner">
        {header}
        <OrnamentDivider />
        {error.status === 404 ? (
          <EmptyState
            title="This run does not exist"
            message={`The backend has no persisted run with id “${runId}”. It may have been mistyped or belongs to another environment.`}
            action={<Link className="button button--secondary" to="/">Return to the dashboard</Link>}
          />
        ) : (
          <ErrorState
            title="The run could not be read"
            error={error}
            onRetry={refresh}
          />
        )}
      </div>
    );
  }

  if (!run) {
    return (
      <div className="page-inner">
        {header}
        <OrnamentDivider />
        <EmptyState title="No run data" message="The backend returned no run payload." />
      </div>
    );
  }

  const canCancel = isCancellableRunState(run.status);
  const canStart = run.status === 'queued' || run.status === 'pending';

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume III · Run ledger"
        title={run.name}
        description={`${run.run_id} · ${formatDateTime(run.created_at)} · ${executionModeLabel(run.execution_mode)}`}
        actions={
          <>
            {canStart ? (
              <button
                type="button"
                className="button button--primary button--small"
                onClick={handleStart}
                disabled={starting || isRefreshing}
                aria-busy={starting || undefined}
              >
                <PlayIcon size={16} />
                <span>{starting ? 'Starting discovery…' : 'Start run'}</span>
              </button>
            ) : null}
            <button
              type="button"
              className="button button--secondary button--small"
              onClick={() => refresh()}
              disabled={isRefreshing}
              aria-busy={isRefreshing || undefined}
            >
              <RefreshIcon size={16} />
              <span>{isRefreshing ? 'Refreshing…' : 'Refresh'}</span>
            </button>
            {canCancel ? (
              <button
                type="button"
                className="button button--danger button--small"
                onClick={() => setDialogOpen(true)}
                disabled={cancelling}
              >
                <BanIcon size={16} />
                <span>{cancelling ? 'Cancelling…' : 'Cancel run'}</span>
              </button>
            ) : null}
            <Link to="/" className="button button--ghost button--small">
              <ArrowLeftIcon size={16} />
              <span>Dashboard</span>
            </Link>
          </>
        }
      />

      <OrnamentDivider />

      {startOutcome ? (
        <Notice tone={startOutcome.tone} title={startOutcome.title}>
          {startOutcome.body}
        </Notice>
      ) : null}

      {createdRunId === run.run_id ? (
        <Notice tone="success" title="Run created">
          The backend persisted run{' '}
          <code className="id-text">{run.run_id}</code> with status{' '}
          <strong>{statusLabel(run.status)}</strong>.{' '}
          {canStart ? 'Click “Start run” above to begin discovery.' : 'Configuration is recorded.'}
        </Notice>
      ) : null}

      {isStale ? (
        <Notice tone="warning" title="Showing the last known state">
          The latest refresh failed{error?.message ? `: ${error.message}` : '.'} The values below
          are the most recent successful read
          {progress?.last_updated_at ? ` from ${formatTime(progress.last_updated_at)}` : ''}. Polling
          continues until the run reaches a terminal state.
        </Notice>
      ) : null}

      {cancelOutcome ? (
        <Notice tone={cancelOutcome.tone} title={cancelOutcome.title}>
          {cancelOutcome.body}
        </Notice>
      ) : null}

      <section className="panel panel--ornate" aria-labelledby="lifecycle-heading">
        <div className="panel__header">
          <div>
            <p className="volume-label">I · Lifecycle</p>
            <h2 id="lifecycle-heading" className="panel__title">Execution state</h2>
          </div>
          <StatusBadge status={run.status} />
        </div>

        <dl className="stat-grid">
          <Stat label="Lifecycle state" value={statusLabel(run.status)} />
          <Stat label="Phase" value={phaseLabel(progress?.phase)} hint="reported by the lifecycle manager" />
          <Stat
            label="Current activity"
            value={progress?.current_activity ? String(progress.current_activity) : UNAVAILABLE}
          />
          <Stat
            label="Queries used"
            value={
              progress && progress.queries_used !== null && progress.queries_used !== undefined
                ? String(progress.queries_used)
                : UNAVAILABLE
            }
            hint={
              progress && progress.queries_remaining !== null && progress.queries_remaining !== undefined
                ? `remaining ${progress.queries_remaining}`
                : 'remaining unavailable'
            }
          />
          <Stat
            label="Evaluations completed"
            value={
              progress && progress.evaluations_completed !== null && progress.evaluations_completed !== undefined
                ? String(progress.evaluations_completed)
                : UNAVAILABLE
            }
            hint={
              progress && progress.evaluations_remaining !== null && progress.evaluations_remaining !== undefined
                ? `remaining ${progress.evaluations_remaining}`
                : 'remaining unavailable'
            }
          />
          <Stat label="Elapsed" value={formatSeconds(progress?.elapsed_seconds)} />
          <Stat label="Started" value={formatDateTime(run.started_at || progress?.started_at)} />
          <Stat label="Completed" value={formatDateTime(run.completed_at || progress?.completed_at)} />
          <Stat
            label="Last progress update"
            value={progress?.last_updated_at ? formatTime(progress.last_updated_at) : UNAVAILABLE}
          />
        </dl>

        {(() => {
          const queriesUsed = Number(progress?.queries_used ?? 0);
          const queriesRemaining = Number(progress?.queries_remaining ?? 0);
          const totalQueries = queriesUsed + queriesRemaining;
          const queryPct = totalQueries > 0 ? Math.min(100, Math.round((queriesUsed / totalQueries) * 100)) : 0;

          return (
            <div style={{ margin: '1.25rem 0 0.5rem', padding: '1rem', background: 'rgba(212, 175, 55, 0.05)', borderRadius: '4px', border: '1px solid rgba(212, 175, 55, 0.2)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.5rem', fontSize: '0.85rem' }}>
                <span style={{ color: 'var(--color-brass-400, #d4af37)', fontWeight: 600 }}>Discovery Progress</span>
                <span style={{ color: 'var(--color-text-secondary, #a0a0a0)' }}>
                  {queriesUsed} / {totalQueries || '10'} queries ({queryPct}%)
                </span>
              </div>
              <div style={{ height: '8px', background: 'rgba(255, 255, 255, 0.08)', borderRadius: '4px', overflow: 'hidden' }}>
                <div
                  style={{
                    height: '100%',
                    width: `${queryPct}%`,
                    background: 'linear-gradient(90deg, #d4af37, #f3e5ab)',
                    transition: 'width 0.4s ease',
                  }}
                />
              </div>
            </div>
          );
        })()}

        <div className="panel__footer">
          <span className="poll-indicator" role="status">
            {isPolling
              ? `Auto-refreshing every ${Math.round((pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS) / 1000)} seconds while the run is active`
              : isTerminal
                ? `Polling stopped: the run is ${statusLabel(status)}.`
                : 'Polling is not active.'}
          </span>
          {progress?.last_updated_at ? (
            <span className="panel-note">Backend progress timestamp {formatTime(progress.last_updated_at)}</span>
          ) : null}
        </div>
      </section>

      {run.stop_reason || progress?.stop_reason || progress?.failure_details ? (
        <section className="panel" aria-labelledby="failure-heading">
          <p className="volume-label">II · Outcome</p>
          <h2 id="failure-heading" className="panel__title">
            {run.status === 'failed' ? 'Failure record' : 'Stop record'}
          </h2>
          <Notice tone={run.status === 'failed' ? 'danger' : 'warning'} title={run.stop_reason || progress?.stop_reason || 'Run stopped'}>
            <FailureDetails details={progress?.failure_details} />
            <p>
              Failed or budget-stopped work is not a scientific result. Completed observations,
              evaluations and revisions remain recorded.
            </p>
          </Notice>
        </section>
      ) : null}

      <section className="panel" aria-labelledby="resources-heading">
        <p className="volume-label">III · Accounting</p>
        <h2 id="resources-heading" className="panel__title">Resources and spending</h2>
        <BudgetPanel budget={budget} />
      </section>

      <section className="panel" aria-labelledby="outputs-heading">
        <p className="volume-label">IV · Outputs</p>
        <h2 id="outputs-heading" className="panel__title">Scientific workflows for this run</h2>
        <div className="workflow-grid">
          <Link className="workflow-card" to={`/hypotheses?run=${encodeURIComponent(run.run_id)}`}>
            <span className="workflow-card__icon" aria-hidden="true"><FlaskIcon size={22} /></span>
            <span className="workflow-card__body">
              <span className="workflow-card__title">Hypotheses</span>
              <span className="workflow-card__description">Revisions and latest evaluation for this run.</span>
            </span>
          </Link>
          <Link className="workflow-card" to={`/reports?run=${encodeURIComponent(run.run_id)}`}>
            <span className="workflow-card__icon" aria-hidden="true"><ScrollIcon size={22} /></span>
            <span className="workflow-card__body">
              <span className="workflow-card__title">Scientific report</span>
              <span className="workflow-card__description">Ranked summary and recorded limitations.</span>
            </span>
          </Link>
          <Link className="workflow-card" to={`/benchmarks?run=${encodeURIComponent(run.run_id)}`}>
            <span className="workflow-card__icon" aria-hidden="true"><ChartIcon size={22} /></span>
            <span className="workflow-card__body">
              <span className="workflow-card__title">Benchmarks</span>
              <span className="workflow-card__description">Stored benchmark results for this run.</span>
            </span>
          </Link>
        </div>
      </section>

      <ConfirmDialog
        open={dialogOpen}
        title={`Cancel “${run.name}”?`}
        description="Cancellation asks the backend to stop admitting work for this run. Completed observations, evaluations and revisions are preserved; confirming does not claim success until the backend responds."
        confirmLabel="Request cancellation"
        cancelLabel="Keep running"
        tone="danger"
        busy={cancelling}
        onConfirm={handleCancel}
        onCancel={() => setDialogOpen(false)}
      />
    </div>
  );
}
