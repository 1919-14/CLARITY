import { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { generateRunReport, getRunReport } from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { Notice } from '../components/feedback/Notice.jsx';
import { RefreshIcon, ScrollIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { RunPicker } from '../components/runs/RunPicker.jsx';
import { useResource } from '../hooks/useResource.js';
import { useRuns } from '../hooks/useRuns.js';
import {
  formatDateTime,
  formatDecimal,
  isUnavailable,
  titleize,
  UNAVAILABLE,
} from '../utils/formatting.js';

// The v1 report record stores limitations as one joined string (see
// docs/openapi_v1.yaml); the canonical report JSON uses a list. Accept both so a
// real report's caveats are never silently hidden.
function limitationList(limitations) {
  if (Array.isArray(limitations)) {
    return limitations.filter((item) => typeof item === 'string' && item.trim());
  }
  if (typeof limitations === 'string' && limitations.trim()) {
    return limitations
      .split(/;\s+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function RankedCandidate({ candidate }) {
  return (
    <article className="report-candidate">
      <header className="report-candidate__header">
        <p className="volume-label">Rank {candidate.rank}</p>
        <h3 className="report-candidate__title">
          <code className="id-text">{candidate.hypothesis_id}</code>
          <span className="report-candidate__formula">{candidate.formula}</span>
        </h3>
        <p className="panel-note">
          {candidate.model_family} · revision {candidate.revision_number} · status {candidate.status}
        </p>
      </header>
      <dl className="detail-list">
        <div className="detail-list__row">
          <dt>{titleize(candidate.primary_metric_name)} (held-out)</dt>
          <dd>{formatDecimal(candidate.held_out_score)}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Training score</dt>
          <dd>{formatDecimal(candidate.training_score)}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Complexity score</dt>
          <dd>{isUnavailable(candidate.complexity_score) ? UNAVAILABLE : candidate.complexity_score}</dd>
        </div>
        <div className="detail-list__row">
          <dt>Dataset version</dt>
          <dd>{isUnavailable(candidate.dataset_version) ? UNAVAILABLE : candidate.dataset_version}</dd>
        </div>
      </dl>
      {candidate.explanation ? <p className="prose">{candidate.explanation}</p> : null}
      {Array.isArray(candidate.uncertainty_and_limitations) &&
      candidate.uncertainty_and_limitations.length > 0 ? (
        <div>
          <p className="volume-label">Recorded limitations</p>
          <ul className="prose-list">
            {candidate.uncertainty_and_limitations.map((limitation) => (
              <li key={limitation}>{limitation}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </article>
  );
}

export function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { runs, isLoading: runsLoading, error: runsError, reload: reloadRuns } = useRuns({ limit: 50 });
  const [selectedRunId, setSelectedRunId] = useState(searchParams.get('run') || '');
  const runId = selectedRunId || runs?.[0]?.run_id || '';
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState(null);

  const loader = useCallback(({ signal }) => getRunReport(runId, { signal }), [runId]);
  const { data, isLoading, isRefreshing, error, reload } = useResource(loader, {
    enabled: Boolean(runId),
  });

  const report = data?.report ?? null;
  const reportState = data?.status ?? (report ? 'available' : 'pending');
  const limitations = limitationList(report?.limitations);

  const changeRun = (nextRunId) => {
    setSelectedRunId(nextRunId);
    setSearchParams(nextRunId ? { run: nextRunId } : {}, { replace: true });
    setGenerateError(null);
  };

  const handleGenerate = async () => {
    if (generating || !runId) return;
    setGenerating(true);
    setGenerateError(null);
    try {
      await generateRunReport(runId);
      reload();
    } catch (generationError) {
      setGenerateError(generationError);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume VII · Reports"
        title="Scientific reports"
        description="The ranked summary a run's records support, including the limitations the backend recorded. A report is never synthesised in the browser."
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
          label="Run to report on"
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
            <span>{isRefreshing ? 'Refreshing…' : 'Refresh report'}</span>
          </button>
        ) : null}
      </div>

      {generateError ? (
        <ErrorState
          compact
          title="The report could not be generated"
          error={generateError}
        />
      ) : null}

      {!runId && !runsLoading ? (
        <EmptyState
          title="No runs available"
          message="Reports are generated from a run's recorded hypotheses, observations and evaluations. Create and execute a run first."
          action={<Link className="button button--secondary" to="/runs/new">Create a run</Link>}
        />
      ) : null}

      {runId && isLoading ? <LoadingState label="Retrieving the scientific report…" /> : null}

      {runId && !isLoading && error ? (
        <ErrorState title="The report could not be retrieved" error={error} onRetry={reload} />
      ) : null}

      {runId && !isLoading && !error && !report ? (
        <EmptyState
          title="No report has been generated"
          message={`The backend reports “${reportState}” for this run. Generation can run once the run has recorded hypotheses and evaluations.`}
          action={
            <button
              type="button"
              className="button button--secondary"
              onClick={handleGenerate}
              disabled={generating}
              aria-busy={generating || undefined}
            >
              <ScrollIcon size={16} />
              <span>{generating ? 'Generating…' : 'Generate report'}</span>
            </button>
          }
        />
      ) : null}

      {runId && !isLoading && !error && report ? (
        <>
          <section className="panel" aria-labelledby="report-meta-heading">
            <div className="panel__header">
              <div>
                <p className="volume-label">Report record</p>
                <h2 id="report-meta-heading" className="panel__title">
                  {report.status === 'completed' ? 'Completed report' : titleize(report.status)}
                </h2>
              </div>
              <button
                type="button"
                className="button button--secondary button--small"
                onClick={handleGenerate}
                disabled={generating}
                aria-busy={generating || undefined}
              >
                <ScrollIcon size={16} />
                <span>{generating ? 'Regenerating…' : 'Regenerate report'}</span>
              </button>
            </div>
            <dl className="stat-grid">
              <div className="stat">
                <dt className="stat__label">Report</dt>
                <dd className="stat__value stat__value--mono">{report.report_id}</dd>
              </div>
              <div className="stat">
                <dt className="stat__label">Generated</dt>
                <dd className="stat__value">{formatDateTime(report.generated_at)}</dd>
              </div>
              <div className="stat">
                <dt className="stat__label">Stop reason</dt>
                <dd className="stat__value">{report.stop_reason ? titleize(report.stop_reason) : UNAVAILABLE}</dd>
              </div>
              <div className="stat">
                <dt className="stat__label">Candidates reported</dt>
                <dd className="stat__value">{report.ranked_candidates?.length ?? 0}</dd>
              </div>
            </dl>
            {limitations.length > 0 ? (
              <Notice tone="warning" title="Limitations recorded with this report">
                <ul className="prose-list">
                  {limitations.map((limitation) => (
                    <li key={limitation}>{limitation}</li>
                  ))}
                </ul>
              </Notice>
            ) : null}
          </section>

          {report.ranked_candidates?.length ? (
            report.ranked_candidates.map((candidate) => (
              <RankedCandidate key={candidate.hypothesis_id} candidate={candidate} />
            ))
          ) : (
            <EmptyState
              title="No ranked candidates"
              message="The report exists but contains no ranked candidates. That is an honest absence of evidence, not a zero score."
            />
          )}
        </>
      ) : null}
    </div>
  );
}
