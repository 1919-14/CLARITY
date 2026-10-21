import React, { useState } from 'react';
import { StatusBadge } from './CommonStates';

export function RunDetailModal({ run, onClose, onCancelRun, onNavigateHypotheses, onNavigateReport, onNavigateBenchmark }) {
  const [isCancelling, setIsCancelling] = useState(false);
  const [cancelError, setCancelError] = useState(null);

  if (!run) return null;

  const isTerminal = ['completed', 'failed', 'stopped_budget', 'cancelled'].includes((run.status || '').toLowerCase());
  const canCancel = !isTerminal && (run.status || '').toLowerCase() === 'running';

  const handleCancel = async () => {
    if (!window.confirm(`Are you sure you want to cancel run ${run.run_id}?`)) {
      return;
    }

    setIsCancelling(true);
    setCancelError(null);
    try {
      await onCancelRun(run.run_id);
      setIsCancelling(false);
    } catch (err) {
      setIsCancelling(false);
      setCancelError(err?.message || 'Failed to cancel run.');
    }
  };

  const progress = run.progress || {};
  const usage = run.usage || {};
  const config = run.config || {};

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="run-detail-title">
      <div className="modal-container modal-lg">
        <header className="modal-header">
          <div>
            <div className="badge-row">
              <StatusBadge status={run.status} />
              <span className="mono-subtle">{run.run_id}</span>
            </div>
            <h3 id="run-detail-title" className="modal-title" style={{ marginTop: '0.25rem' }}>
              {run.name || 'Discovery Run Details'}
            </h3>
          </div>
          <button type="button" className="btn-icon" onClick={onClose} aria-label="Close dialog">
            ✕
          </button>
        </header>

        <div className="modal-body-scroll">
          {cancelError && (
            <div className="alert alert-danger" role="alert">
              {cancelError}
            </div>
          )}

          {/* Quick Actions / Navigation */}
          <div className="detail-actions-panel">
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onNavigateHypotheses(run.run_id)}
            >
              🔭 Inspect Hypotheses
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onNavigateReport(run.run_id)}
            >
              📄 Scientific Report
            </button>
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => onNavigateBenchmark(run.run_id)}
            >
              🏆 Ground-Truth Benchmark
            </button>
            {canCancel && (
              <button
                type="button"
                className="btn btn-danger btn-sm"
                onClick={handleCancel}
                disabled={isCancelling}
              >
                {isCancelling ? 'Cancelling...' : 'Cancel Run'}
              </button>
            )}
          </div>

          {/* Progress & Live Lifecycle */}
          <div className="detail-card">
            <h4 className="detail-card-title">Run Execution & Progress</h4>
            <div className="metric-strip">
              <div className="metric-cell">
                <span className="metric-label">Current Phase</span>
                <span className="metric-value font-highlight">{run.phase || run.status || 'Active'}</span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Oracle Queries Used</span>
                <span className="metric-value">
                  {(progress.queries_executed ?? usage.queries_executed) != null ? (
                    `${progress.queries_executed ?? usage.queries_executed} / ${config.max_oracle_queries ?? run.max_queries ?? '—'}`
                  ) : (
                    <span className="text-muted">Unavailable</span>
                  )}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Evaluations Completed</span>
                <span className="metric-value">
                  {(progress.evaluations_completed ?? usage.evaluations_completed) != null ? (
                    `${progress.evaluations_completed ?? usage.evaluations_completed} / ${config.max_evaluation_tasks ?? run.max_evaluations ?? '—'}`
                  ) : (
                    <span className="text-muted">Unavailable</span>
                  )}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Stop Reason</span>
                <span className="metric-value mono-text">
                  {run.stop_reason || (isTerminal ? 'Finished' : 'Running')}
                </span>
              </div>
            </div>
          </div>

          {/* Resource Usage & Cost Guardrails */}
          <div className="detail-card">
            <h4 className="detail-card-title">Compute Resource Usage & Billing</h4>
            <div className="metric-strip">
              <div className="metric-cell">
                <span className="metric-label">Execution Platform</span>
                <span className="metric-value">
                  {run.execution_mode === 'nodus' ? 'Nodus Compute (Remote)' : 'Local Deterministic'}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Estimated Spend</span>
                <span className="metric-value">
                  {usage.estimated_cost_usd != null ? `$${Number(usage.estimated_cost_usd).toFixed(4)}` : <span className="text-muted">Unavailable</span>}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Live / Accruing Spend</span>
                <span className="metric-value">
                  {usage.live_accruing_cost_usd != null ? `$${Number(usage.live_accruing_cost_usd).toFixed(4)}` : <span className="text-muted">Unavailable</span>}
                </span>
              </div>
              <div className="metric-cell">
                <span className="metric-label">Settled / Provider Confirmed</span>
                <span className="metric-value font-highlight">
                  {usage.settled_cost_usd != null ? `$${Number(usage.settled_cost_usd).toFixed(4)}` : <span className="text-muted">Unavailable</span>}
                </span>
              </div>
            </div>
            <p className="field-hint" style={{ marginTop: '0.75rem' }}>
              ℹ️ CLARITY guarantees scientific honesty: Unsettled compute costs are reported as <em>Unavailable</em> rather than fabricated as $0.00.
            </p>
          </div>

          {/* Configuration Snapshot */}
          <div className="detail-card">
            <h4 className="detail-card-title">Configured Enforcements</h4>
            <div className="config-grid">
              <div className="config-item">
                <span className="config-key">Input Domain:</span>
                <span className="config-val mono-text">{run.domain || config.oracle_domain || '1d_polynomial'}</span>
              </div>
              <div className="config-item">
                <span className="config-key">Max Concurrency:</span>
                <span className="config-val">{config.max_concurrency || run.max_concurrency || '4 tasks'}</span>
              </div>
              <div className="config-item">
                <span className="config-key">Duration Limit:</span>
                <span className="config-val">{config.max_runtime_seconds || run.max_runtime_seconds || '300s'}</span>
              </div>
              <div className="config-item">
                <span className="config-key">Created Timestamp:</span>
                <span className="config-val mono-text">{run.created_at || '—'}</span>
              </div>
            </div>
          </div>
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
