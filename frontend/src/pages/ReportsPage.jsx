import { useCallback, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import { exportRunReport, generateRunReport, getRunReport } from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';
import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { Notice } from '../components/feedback/Notice.jsx';
import { DownloadIcon, RefreshIcon, ScrollIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { RunPicker } from '../components/runs/RunPicker.jsx';
import { Stat } from '../components/status/Stat.jsx';
import { useResource } from '../hooks/useResource.js';
import { useRuns } from '../hooks/useRuns.js';
import {
  formatDateTime,
  formatDecimal,
  formatInteger,
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

const METRIC_LABELS = { mae: 'MAE', rmse: 'RMSE', r2: 'R²' };

// Formats the backend can render. The download is produced server-side so the
// browser never assembles a report of its own.
const EXPORTS = [
  { format: 'pdf', label: 'PDF', extension: 'pdf' },
  { format: 'markdown', label: 'Markdown', extension: 'md' },
  { format: 'json', label: 'JSON', extension: 'json' },
];

function saveBlob(blob, filename) {
  if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
    throw new Error('This browser cannot prepare a file download.');
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Release the object URL once the browser has started the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function metricLabel(name) {
  if (isUnavailable(name)) return 'Primary metric';
  return METRIC_LABELS[String(name).toLowerCase()] || titleize(name);
}

// The ranking engine explains a display position as a " | " separated ledger.
// Those segments are shown as labelled rows below, so the fragments that are
// already presented as fields (position, cohort rank, status, tier, complexity,
// evidence counts) are dropped instead of repeated inside a paragraph.
const RATIONALE_DUPLICATES = [
  /^display position \d+$/i,
  /^within-cohort scientific rank\b/i,
  /^status:/i,
  /^tier:/i,
  /^complexity:/i,
  /^evidence:/i,
];

function rankingRationale(candidate) {
  const text = typeof candidate.explanation === 'string' ? candidate.explanation.trim() : '';
  if (!text) return { lines: [], prose: '' };

  const segments = text
    .split(' | ')
    .map((segment) => segment.trim())
    .filter(Boolean);
  if (segments.length < 2) return { lines: [], prose: text };

  const lines = segments
    .filter((segment) => segment !== candidate.hypothesis_id)
    .filter((segment) => !RATIONALE_DUPLICATES.some((pattern) => pattern.test(segment)))
    .map((segment) => {
      // Only short prefixes become a label column; a full sentence stays a
      // full-width line instead of being squeezed into the label gutter.
      const match = /^([^:]{2,26}):\s*(.+)$/.exec(segment);
      return match ? { label: match[1], text: match[2] } : { label: null, text: segment };
    });

  return { lines, prose: '' };
}

const PLAUSIBILITY_TONES = {
  plausible: 'success',
  contradicted: 'danger',
  uncertain: 'warning',
};

function parameterLabel(key) {
  // Symbolic coefficients such as "w0" keep their notation; longer names read
  // better title-cased ("coefficient_order" → "Coefficient Order").
  return /^[a-z]\d+$/i.test(key) ? key : titleize(key);
}

function parameterValue(value) {
  if (value === null || value === undefined) return UNAVAILABLE;
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return value;
  return JSON.stringify(value);
}

function formatInputValues(values) {
  if (!values || typeof values !== 'object') return UNAVAILABLE;
  const entries = Object.entries(values);
  if (!entries.length) return UNAVAILABLE;
  return entries.map(([key, value]) => `${key} = ${parameterValue(value)}`).join(', ');
}

function evidenceRows(candidate) {
  const supporting = Array.isArray(candidate.supporting_evidence) ? candidate.supporting_evidence : [];
  const contradicting = Array.isArray(candidate.contradicting_evidence)
    ? candidate.contradicting_evidence
    : [];
  return [
    ...supporting.map((item) => ({ ...item, verdict: 'supporting' })),
    ...contradicting.map((item) => ({ ...item, verdict: 'contradicting' })),
  ];
}

function RankedCandidate({ candidate }) {
  const tone = PLAUSIBILITY_TONES[String(candidate.status || '').toLowerCase()] || 'neutral';
  const metric = metricLabel(candidate.primary_metric_name);
  const supporting = Array.isArray(candidate.supporting_evidence) ? candidate.supporting_evidence : [];
  const contradicting = Array.isArray(candidate.contradicting_evidence)
    ? candidate.contradicting_evidence
    : [];
  const indeterminate = isUnavailable(candidate.indeterminate_count)
    ? UNAVAILABLE
    : candidate.indeterminate_count;
  const evidence = evidenceRows(candidate);
  const parameters =
    candidate.parameters && typeof candidate.parameters === 'object'
      ? Object.entries(candidate.parameters)
      : [];
  const { lines, prose } = rankingRationale(candidate);
  const limitations = Array.isArray(candidate.uncertainty_and_limitations)
    ? candidate.uncertainty_and_limitations.filter((item) => typeof item === 'string' && item.trim())
    : [];

  return (
    <article className="report-candidate">
      <header className="report-candidate__header">
        <div
          className="report-candidate__rank"
          title="Display position — presentation order only, not a scientific rank"
        >
          <span className="report-candidate__rank-label">Display</span>
          <span className="report-candidate__rank-value">
            {isUnavailable(candidate.rank) ? UNAVAILABLE : candidate.rank}
          </span>
        </div>

        <div className="report-candidate__heading">
          <h3 className="report-candidate__title">
            <span className="report-candidate__formula">{candidate.formula || UNAVAILABLE}</span>
          </h3>
          <p className="report-candidate__meta">
            <code className="id-text">{candidate.hypothesis_id}</code>
            <span aria-hidden="true">·</span>
            <span>{titleize(candidate.model_family)}</span>
            <span aria-hidden="true">·</span>
            <span>revision {isUnavailable(candidate.revision_number) ? UNAVAILABLE : candidate.revision_number}</span>
            {candidate.evaluation_id ? (
              <>
                <span aria-hidden="true">·</span>
                <span>
                  evaluation <code className="id-text">{candidate.evaluation_id}</code>
                </span>
              </>
            ) : null}
          </p>
          <div className="report-candidate__badges">
            <span className={`status-badge status-badge--${tone}`} data-status={candidate.status}>
              {candidate.status ? titleize(candidate.status) : UNAVAILABLE}
            </span>
            <span className="badge badge--neutral">{titleize(candidate.evaluation_tier)}</span>
            <span className="badge badge--accent">
              {isUnavailable(candidate.within_cohort_rank)
                ? 'No within-cohort rank'
                : `Cohort rank ${candidate.within_cohort_rank}`}
            </span>
            <span className="badge badge--success">Supporting {formatInteger(supporting.length)}</span>
            <span className="badge badge--danger">
              Contradicting {formatInteger(contradicting.length)}
            </span>
            <span className="badge badge--neutral">Indeterminate {formatInteger(indeterminate)}</span>
          </div>
        </div>
      </header>

      <dl className="stat-grid report-candidate__stats">
        <Stat
          label={`${metric} · held out`}
          value={formatDecimal(candidate.held_out_score)}
          hint={isUnavailable(candidate.held_out_score) ? 'No held-out evaluation recorded.' : null}
        />
        <Stat label={`${metric} · training`} value={formatDecimal(candidate.training_score)} />
        <Stat
          label="Complexity"
          value={isUnavailable(candidate.complexity_score) ? UNAVAILABLE : candidate.complexity_score}
        />
        <Stat
          label="Dataset version"
          value={isUnavailable(candidate.dataset_version) ? UNAVAILABLE : candidate.dataset_version}
        />
        <Stat
          label="Within-cohort rank"
          value={isUnavailable(candidate.within_cohort_rank) ? UNAVAILABLE : candidate.within_cohort_rank}
        />
      </dl>

      {lines.length > 0 || prose ? (
        <section className="report-candidate__section">
          <h4 className="report-candidate__section-heading">Ranking rationale</h4>
          {prose ? (
            <p className="prose">{prose}</p>
          ) : (
            <ul className="report-rationale">
              {lines.map((line, index) => (
                <li key={`${line.label || 'note'}-${index}`}>
                  {line.label ? <span className="report-rationale__label">{line.label}</span> : null}
                  <span
                    className={`report-rationale__text${
                      line.label ? '' : ' report-rationale__text--full'
                    }`}
                  >
                    {line.text}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {parameters.length > 0 ? (
        <section className="report-candidate__section">
          <h4 className="report-candidate__section-heading">Fitted parameters</h4>
          <dl className="detail-list">
            {parameters.map(([key, value]) => (
              <div className="detail-list__row" key={key}>
                <dt>{parameterLabel(key)}</dt>
                <dd>
                  <code className="id-text">{parameterValue(value)}</code>
                </dd>
              </div>
            ))}
          </dl>
        </section>
      ) : null}

      {evidence.length > 0 ? (
        <section className="report-candidate__section">
          <details className="report-disclosure">
            <summary className="report-disclosure__summary">
              Evidence records ({formatInteger(evidence.length)})
            </summary>
            <div className="table-scroll">
              <table className="data-table data-table--compact">
                <thead>
                  <tr>
                    <th>Observation</th>
                    <th>Inputs</th>
                    <th>Observed</th>
                    <th>Predicted</th>
                    <th>Residual</th>
                    <th>Tolerance</th>
                    <th>Partition</th>
                    <th>Verdict</th>
                  </tr>
                </thead>
                <tbody>
                  {evidence.map((item, index) => (
                    <tr key={`${item.verdict}-${item.observation_id || 'obs'}-${index}`}>
                      <td data-label="Observation">
                        <code className="id-text">
                          {item.observation_id || UNAVAILABLE}
                          {isUnavailable(item.sequence_number) ? '' : ` · #${item.sequence_number}`}
                        </code>
                      </td>
                      <td data-label="Inputs">{formatInputValues(item.input_values)}</td>
                      <td data-label="Observed" className="numeric">
                        {formatDecimal(item.observed_value)}
                      </td>
                      <td data-label="Predicted" className="numeric">
                        {formatDecimal(item.predicted_value)}
                      </td>
                      <td data-label="Residual" className="numeric">
                        {formatDecimal(item.residual)}
                      </td>
                      <td data-label="Tolerance" className="numeric">
                        {formatDecimal(item.tolerance)}
                      </td>
                      <td data-label="Partition">
                        <span className="badge badge--neutral">
                          {item.is_held_out ? 'Held out' : 'Training'}
                        </span>
                      </td>
                      <td data-label="Verdict">
                        <span
                          className={`status-badge status-badge--${
                            item.verdict === 'supporting' ? 'success' : 'danger'
                          }`}
                        >
                          {titleize(item.verdict)}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </section>
      ) : null}

      {limitations.length > 0 ? (
        <section className="report-candidate__section">
          <h4 className="report-candidate__section-heading">Recorded limitations</h4>
          <ul className="prose-list">
            {limitations.map((limitation, index) => (
              <li key={`${limitation}-${index}`}>{limitation}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </article>
  );
}

export function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { runs, isLoading: runsLoading, error: runsError, reload: reloadRuns } = useRuns({ limit: 50 });
  const [selectedRunId, setSelectedRunId] = useState(searchParams.get('run') || '');
  const runId = selectedRunId || runs?.[0]?.run_id || '';
  const activeRun = runs?.find((run) => run.run_id === runId) ?? null;
  const [generating, setGenerating] = useState(false);
  const [generateError, setGenerateError] = useState(null);
  const [exportingFormat, setExportingFormat] = useState(null);
  const [exportError, setExportError] = useState(null);

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
    setExportError(null);
  };

  const handleExport = async (format, extension) => {
    if (!runId || exportingFormat) return;
    setExportingFormat(format);
    setExportError(null);
    try {
      const { blob, filename } = await exportRunReport(runId, format);
      saveBlob(blob, filename || `report_${runId}.${extension}`);
    } catch (error) {
      if (!error?.isAborted) setExportError(error);
    } finally {
      setExportingFormat(null);
    }
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
                {activeRun?.name ? (
                  <p className="panel__subtitle">
                    {activeRun.name} · <code className="id-text">{report.run_id}</code>
                  </p>
                ) : null}
              </div>
              <div className="panel__header-actions">
                <div className="export-actions" role="group" aria-label="Export this report">
                  <span className="export-actions__label">Export</span>
                  {EXPORTS.map((entry) => (
                    <button
                      key={entry.format}
                      type="button"
                      className={`button button--small ${
                        entry.format === 'pdf' ? 'button--secondary' : 'button--ghost'
                      }`}
                      onClick={() => handleExport(entry.format, entry.extension)}
                      disabled={Boolean(exportingFormat) || generating}
                      aria-busy={exportingFormat === entry.format || undefined}
                      title={`Download the backend-rendered report as ${entry.label}`}
                    >
                      {entry.format === 'pdf' ? <DownloadIcon size={14} /> : null}
                      <span>{exportingFormat === entry.format ? 'Preparing…' : entry.label}</span>
                    </button>
                  ))}
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
            </div>

            {exportError ? (
              <ErrorState
                compact
                title="The report could not be exported"
                error={exportError}
              />
            ) : null}

            <dl className="stat-grid">
              <Stat label="Report" value={report.report_id} mono />
              <Stat label="Generated" value={formatDateTime(report.generated_at)} />
              <Stat
                label="Stop reason"
                value={report.stop_reason ? titleize(report.stop_reason) : UNAVAILABLE}
              />
              <Stat label="Candidates reported" value={formatInteger(report.ranked_candidates?.length ?? 0)} />
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
            <section aria-labelledby="ranked-candidates-heading">
              <div className="section-heading-row">
                <p className="volume-label">Ranked candidates</p>
                <h2 id="ranked-candidates-heading" className="section-heading">
                  Reported candidates
                </h2>
                <p className="panel-note">
                  Display position is presentation order only. The within-cohort rank is the
                  scientific ordering within a comparable evaluation cohort.
                </p>
              </div>
              {report.ranked_candidates.map((candidate) => (
                <RankedCandidate
                  key={`${candidate.hypothesis_id}-${candidate.rank ?? 'unranked'}`}
                  candidate={candidate}
                />
              ))}
            </section>
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
