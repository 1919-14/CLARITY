import { Link } from 'react-router-dom';

import { isActiveRunState } from '../api/contracts.js';
import { ActiveRunPanel } from '../components/dashboard/ActiveRunPanel.jsx';
import { QuickLinks } from '../components/dashboard/QuickLinks.jsx';
import { RecentRunsTable } from '../components/dashboard/RecentRunsTable.jsx';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { PlusIcon, RefreshIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { useRuns } from '../hooks/useRuns.js';
import { formatTime } from '../utils/formatting.js';

export function DashboardPage() {
  const { runs, isLoading, isRefreshing, error, reload, loadedAt } = useRuns({ limit: 20 });

  const activeRun = (runs || []).find((run) => isActiveRunState(run.status)) || null;
  const activeCount = (runs || []).filter((run) => isActiveRunState(run.status)).length;

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume I · Overview"
        title="Research dashboard"
        description="Configuration, execution and scientific records for every CLARITY discovery run. Values shown here are read from the Flask API; unmeasured values are marked unavailable rather than assumed to be zero."
        lede
        actions={
          <Link to="/runs/new" className="button button--primary">
            <PlusIcon size={16} />
            <span>Create discovery run</span>
          </Link>
        }
      />

      <OrnamentDivider />

      {error && !runs ? (
        <ErrorState
          title="The run archive could not be opened"
          error={error}
          onRetry={reload}
        />
      ) : (
        <>
          {isLoading ? (
            <section className="panel panel--ornate" aria-labelledby="active-run-heading">
              <h2 id="active-run-heading" className="panel__title">Current investigation</h2>
              <LoadingState label="Reading active runs from the backend…" compact />
            </section>
          ) : activeRun ? (
            <>
              <ActiveRunPanel runId={activeRun.run_id} />
              {activeCount > 1 ? (
                <p className="panel-note">
                  {activeCount} runs are active; this panel follows the most recently created one.
                  Open the run ledger of another run from the table below.
                </p>
              ) : null}
            </>
          ) : (
            <section className="panel" aria-labelledby="active-run-heading">
              <h2 id="active-run-heading" className="panel__title">Current investigation</h2>
              <EmptyState
                title="No run is active"
                message="Queued or running runs appear here with their lifecycle phase, resource usage and cost state. Creating a run does not start discovery by itself."
                action={<Link className="button button--secondary" to="/runs/new">Configure a run</Link>}
              />
            </section>
          )}

          <section className="panel" aria-labelledby="recent-runs-heading">
            <div className="panel__header">
              <div>
                <p className="volume-label">Volume II · Ledger</p>
                <h2 id="recent-runs-heading" className="panel__title">Recent discovery runs</h2>
              </div>
              <div className="panel__header-actions">
                <button
                  type="button"
                  className="button button--ghost button--small"
                  onClick={reload}
                  disabled={isRefreshing}
                  aria-busy={isRefreshing || undefined}
                >
                  <RefreshIcon size={16} />
                  <span>{isRefreshing ? 'Refreshing…' : 'Refresh'}</span>
                </button>
              </div>
            </div>

            {isLoading ? <LoadingState label="Retrieving persisted runs…" /> : null}

            {!isLoading && error ? (
              <ErrorState
                compact
                title="Recent runs could not be refreshed"
                error={error}
                onRetry={reload}
              />
            ) : null}

            {!isLoading && !error && runs && runs.length === 0 ? (
              <EmptyState
                title="No runs recorded yet"
                message="The backend returned an empty run collection. Create the first discovery run to begin recording hypotheses, observations and evaluations."
                action={<Link className="button button--secondary" to="/runs/new">Create the first run</Link>}
              />
            ) : null}

            {!isLoading && !error && runs && runs.length > 0 ? (
              <>
                <RecentRunsTable runs={runs} />
                <p className="panel-note" role="status">
                  {runs.length} run{runs.length === 1 ? '' : 's'} loaded
                  {loadedAt ? ` · last read ${formatTime(loadedAt.toISOString())}` : ''}
                </p>
              </>
            ) : null}
          </section>
        </>
      )}

      <QuickLinks />
    </div>
  );
}
