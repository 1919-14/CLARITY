import React, { useState, useEffect } from 'react';
import { api, ApiError } from '../api/client';
import { LoadingState, ErrorState, EmptyState } from './CommonStates';

export function ScientificViewModal({ runId, viewType, onClose }) {
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [data, setData] = useState(null);

  useEffect(() => {
    let isCurrent = true;

    async function fetchData() {
      setLoading(true);
      setError(null);
      try {
        let result = null;
        if (viewType === 'hypotheses') {
          result = await api.getHypotheses(runId);
        } else if (viewType === 'report') {
          result = await api.getReport(runId);
        } else if (viewType === 'benchmark') {
          result = await api.getBenchmark(runId);
        }
        if (isCurrent) {
          setData(result);
          setLoading(false);
        }
      } catch (err) {
        if (isCurrent) {
          setLoading(false);
          setError(err instanceof ApiError ? err.message : 'Failed to retrieve scientific data.');
        }
      }
    }

    if (runId && viewType) {
      fetchData();
    }

    return () => {
      isCurrent = false;
    };
  }, [runId, viewType]);

  const titles = {
    hypotheses: 'Candidate Hypotheses & Evidence',
    report: 'Final Scientific Discovery Report',
    benchmark: 'Ground-Truth Benchmark Performance',
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="scientific-view-title">
      <div className="modal-container modal-lg">
        <header className="modal-header">
          <div>
            <span className="mono-subtle">{runId}</span>
            <h3 id="scientific-view-title" className="modal-title">
              {titles[viewType] || 'Scientific Explorer'}
            </h3>
          </div>
          <button type="button" className="btn-icon" onClick={onClose} aria-label="Close dialog">
            ✕
          </button>
        </header>

        <div className="modal-body-scroll">
          {loading && <LoadingState message={`Fetching ${viewType} from backend...`} />}
          {error && <ErrorState title="Data Unavailable" error={error} />}

          {!loading && !error && !data && (
            <EmptyState
              title="No Scientific Records Generated"
              message="The run has either not produced candidates yet, or results are still pending synthesis."
            />
          )}

          {!loading && !error && data && (
            <div className="scientific-content">
              {viewType === 'hypotheses' && <HypothesesList hypotheses={data.hypotheses || data} />}
              {viewType === 'report' && <ReportView report={data.report || data} />}
              {viewType === 'benchmark' && <BenchmarkView benchmark={data.benchmark || data} />}
            </div>
          )}
        </div>

        <footer className="modal-footer">
          <button type="button" className="btn btn-secondary" onClick={onClose}>
            Close
          </button>
        </footer>
      </div>
    </div>
  );
}

function HypothesesList({ hypotheses }) {
  const items = Array.isArray(hypotheses) ? hypotheses : [];

  if (items.length === 0) {
    return <EmptyState title="No hypotheses available" message="Candidate generation has not executed for this run." />;
  }

  return (
    <div className="hypotheses-stack">
      {items.map((hyp) => (
        <div key={hyp.hypothesis_id} className="hypothesis-card">
          <div className="hypothesis-header">
            <span className="mono-badge">{hyp.hypothesis_id}</span>
            <span className="text-secondary text-sm">Complexity: {hyp.complexity ?? '—'}</span>
            <span className="text-secondary text-sm">Status: {hyp.status ?? 'candidate'}</span>
          </div>
          <div className="hypothesis-formula">
            <code>{hyp.formula || 'No formula summary'}</code>
          </div>
          {hyp.scientific_metrics && (
            <div className="hypothesis-metrics">
              <span>MAE: <strong>{hyp.scientific_metrics.mae?.toFixed(4) ?? 'Unavailable'}</strong></span>
              <span>RMSE: <strong>{hyp.scientific_metrics.rmse?.toFixed(4) ?? 'Unavailable'}</strong></span>
              <span>Rank: <strong>#{hyp.scientific_metrics.rank ?? '—'}</strong></span>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function ReportView({ report }) {
  return (
    <div className="detail-card">
      <h4 className="detail-card-title">Discovery Report Summary</h4>
      <div className="config-grid">
        <div className="config-item">
          <span className="config-key">Status:</span>
          <span className="config-val font-highlight">{report.status || 'Generated'}</span>
        </div>
        <div className="config-item">
          <span className="config-key">Stop Reason:</span>
          <span className="config-val mono-text">{report.stop_reason || '—'}</span>
        </div>
        <div className="config-item" style={{ gridColumn: 'span 2' }}>
          <span className="config-key">Documented Limitations:</span>
          <p className="state-message" style={{ marginTop: '0.25rem' }}>
            {report.limitations || 'None declared.'}
          </p>
        </div>
      </div>
    </div>
  );
}

function BenchmarkView({ benchmark }) {
  return (
    <div className="detail-card">
      <h4 className="detail-card-title">Evaluator Ground-Truth Scoring</h4>
      <div className="metric-strip">
        <div className="metric-cell">
          <span className="metric-label">Target Function</span>
          <span className="metric-value mono-text">{benchmark.target_label || '—'}</span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Hit@1 / Hit@3 / Hit@5</span>
          <span className="metric-value font-highlight">
            {benchmark.hit_at_1 ? 'Yes' : 'No'} / {benchmark.hit_at_3 ? 'Yes' : 'No'} / {benchmark.hit_at_5 ? 'Yes' : 'No'}
          </span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Reciprocal Rank (MRR)</span>
          <span className="metric-value font-highlight">
            {benchmark.reciprocal_rank != null ? benchmark.reciprocal_rank.toFixed(3) : 'Unavailable'}
          </span>
        </div>
        <div className="metric-cell">
          <span className="metric-label">Queries to Target</span>
          <span className="metric-value">
            {benchmark.queries_to_target ?? 'Unavailable'}
          </span>
        </div>
      </div>
    </div>
  );
}
