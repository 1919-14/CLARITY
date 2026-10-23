import { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { listRunHypotheses } from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { RefreshIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { RunPicker } from '../components/runs/RunPicker.jsx';
import { StatusBadge } from '../components/status/StatusBadge.jsx';
import { useResource } from '../hooks/useResource.js';
import { useRuns } from '../hooks/useRuns.js';
import { formatDateTime, formatDecimal, UNAVAILABLE } from '../utils/formatting.js';

function EvaluationSummary({ evaluation }) {
  if (!evaluation) return <span className="muted">Not evaluated</span>;
  const mae = evaluation.metrics?.mae;
  const rmse = evaluation.metrics?.rmse;
  return (
    <span className="evaluation-summary">
      <StatusBadge status={evaluation.status} label={evaluation.status === 'completed' ? 'Evaluated' : undefined} />
      <span className="muted">
        MAE {formatDecimal(mae)} · RMSE {formatDecimal(rmse)}
      </span>
      {evaluation.held_out_metrics ? (
        <span className="muted">Held-out MAE {formatDecimal(evaluation.held_out_metrics.mae)}</span>
      ) : (
        <span className="muted">Held-out metrics {UNAVAILABLE.toLowerCase()}</span>
      )}
    </span>
  );
}

export function HypothesesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { runs, isLoading: runsLoading, error: runsError, reload: reloadRuns } = useRuns({ limit: 50 });
  const [selectedRunId, setSelectedRunId] = useState(searchParams.get('run') || '');
  // Fall back to the newest persisted run without an effect round-trip.
  const runId = selectedRunId || runs?.[0]?.run_id || '';

  const loader = useCallback(({ signal }) => listRunHypotheses(runId, { signal }), [runId]);
  const { data, isLoading, isRefreshing, error, reload } = useResource(loader, {
    enabled: Boolean(runId),
  });

  const hypotheses = data?.hypotheses ?? [];

  const changeRun = (nextRunId) => {
    setSelectedRunId(nextRunId);
    setSearchParams(nextRunId ? { run: nextRunId } : {}, { replace: true });
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume VI · Hypotheses"
        title="Candidate explanations"
        description="Every hypothesis recorded for a run, with its revision history and the latest evaluation the backend stored. Metrics are formatted exactly as reported; missing metrics stay unavailable."
      />
      <OrnamentDivider />

      {runsError ? (
        <ErrorState title="Runs could not be loaded" error={runsError} onRetry={reloadRuns} compact />
      ) : null}

      <div className="toolbar">
        <RunPicker
          runs={runs}
          value={runId}
          onChange={changeRun}
          isLoading={runsLoading}
          label="Run to inspect"
        />
        {runId ? (
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={reload}
            disabled={isRefreshing}
            aria-busy={isRefreshing || undefined}
          >
            <RefreshIcon size={16} />
            <span>{isRefreshing ? 'Refreshing…' : 'Refresh hypotheses'}</span>
          </button>
        ) : null}
      </div>

      {!runId && !runsLoading ? (
        <EmptyState
          title="No runs available"
          message="Hypotheses belong to a run. Create a discovery run before exploring candidate explanations."
          action={<Link className="button button--secondary" to="/runs/new">Create a run</Link>}
        />
      ) : null}

      {runId && isLoading ? <LoadingState label="Retrieving hypotheses…" /> : null}

      {runId && !isLoading && error ? (
        <ErrorState
          title="Hypotheses could not be retrieved"
          error={error}
          onRetry={reload}
        />
      ) : null}

      {runId && !isLoading && !error ? (
        hypotheses.length === 0 ? (
          <EmptyState
            title="No hypotheses recorded for this run"
            message="The backend returned an empty hypothesis collection. The discovery engine may not have proposed candidates yet, or this run was created without execution."
            action={<Link className="text-link" to={`/runs/${encodeURIComponent(runId)}`}>Open the run ledger</Link>}
          />
        ) : (
          <div className="table-scroll">
            <table className="data-table data-table--wide">
              <caption className="visually-hidden">Hypotheses recorded for the selected run</caption>
              <thead>
                <tr>
                  <th scope="col">Hypothesis</th>
                  <th scope="col">Family</th>
                  <th scope="col">Latest revision</th>
                  <th scope="col">Flags</th>
                  <th scope="col">Latest evaluation</th>
                </tr>
              </thead>
              <tbody>
                {hypotheses.map((hypothesis) => {
                  const revision = hypothesis.latest_revision;
                  return (
                    <tr key={hypothesis.hypothesis_id}>
                      <td data-label="Hypothesis">
                        <code className="id-text">{hypothesis.hypothesis_id}</code>
                        <span className="muted block-note">
                          revision {hypothesis.current_revision} · updated {formatDateTime(hypothesis.updated_at)}
                        </span>
                      </td>
                      <td data-label="Family">{hypothesis.model_family}</td>
                      <td data-label="Latest revision">
                        {revision ? (
                          <>
                            <code className="formula-text">{revision.formula}</code>
                            <span className="muted block-note">
                              complexity {revision.complexity} · {revision.creation_source}
                            </span>
                          </>
                        ) : (
                          UNAVAILABLE
                        )}
                      </td>
                      <td data-label="Flags">
                        {hypothesis.is_pinned ? 'Pinned' : 'Not pinned'}
                        <span className="block-note muted">
                          {hypothesis.is_excluded ? 'Excluded from selection' : 'Included in selection'}
                        </span>
                      </td>
                      <td data-label="Latest evaluation">
                        <EvaluationSummary evaluation={hypothesis.latest_evaluation} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )
      ) : null}
    </div>
  );
}
