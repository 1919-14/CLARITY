import { useId } from 'react';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';
import {
  formatDateTime,
  formatDecimal,
  formatInteger,
  UNAVAILABLE,
} from '../../utils/formatting.js';
import { CheckCircleIcon, CloseIcon, InfoIcon, ShieldIcon } from '../icons.jsx';

function formatHitRate(val, digits = 2) {
  if (val === true) return (1.0).toFixed(digits);
  if (val === false) return (0.0).toFixed(digits);
  if (typeof val === 'number') return formatDecimal(val, digits);
  return UNAVAILABLE;
}

export function BenchmarkRunDetailModal({
  open,
  runItem,
  onClose,
}) {
  const titleId = useId();
  const containerRef = useModalFocusTrap({ open, onClose });

  if (!open || !runItem) return null;

  // Supports either full RunPerformanceMeasurement or serialized BenchmarkResultModel
  const accuracy = runItem.accuracy || {};
  const efficiency = runItem.query_efficiency || {};
  const runtime = runItem.runtime || {};
  const cost = runItem.cost || {};
  const config = runItem.configuration || {};

  const benchmarkId = runItem.benchmark_id || runItem.measurement_id || 'unspecified';
  const runId = runItem.run_id || UNAVAILABLE;
  const scenarioId = runItem.scenario_id || runItem.benchmark_name || 'unspecified';
  const timestamp = runItem.executed_at || runItem.timestamp;
  const status = runItem.status || runItem.evaluation_status || runItem.accuracy?.evaluation_status || 'unknown';

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className="dialog dialog--large"
        style={{ width: 'min(100%, 52rem)', maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', borderBottom: '1px solid rgba(201,169,98,0.25)', paddingBottom: 'var(--space-3)' }}>
          <div>
            <p className="volume-label">Benchmark Run Ledger & Provenance</p>
            <h2 className="dialog__title" id={titleId} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)', margin: 0 }}>
              <code className="id-text" style={{ fontSize: '1.3rem' }}>{benchmarkId}</code>
              <span className={`badge badge--${status === 'completed' || status === 'target_reached' ? 'success' : status === 'failed' ? 'danger' : 'neutral'}`}>
                {status}
              </span>
            </h2>
          </div>
          <button
            type="button"
            className="button button--ghost button--small"
            onClick={onClose}
            aria-label="Close benchmark details"
          >
            <CloseIcon size={18} />
          </button>
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-4) 0' }}>
          {/* Metadata & Provenance */}
          <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
            <span className="volume-label">Methodology & Execution Metadata</span>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 'var(--space-3)', marginTop: 'var(--space-2)' }}>
              <div>
                <span className="stat__label">Discovery Run ID</span>
                <span className="stat__value" style={{ fontSize: '1rem', display: 'block' }}>
                  {runId !== UNAVAILABLE ? <code className="id-text">{runId}</code> : UNAVAILABLE}
                </span>
              </div>

              <div>
                <span className="stat__label">Scenario & Version</span>
                <span className="stat__value" style={{ fontSize: '1rem', display: 'block' }}>
                  <strong>{scenarioId}</strong>
                  {runItem.scenario_version || runItem.version ? ` (v${runItem.scenario_version || runItem.version})` : ''}
                </span>
              </div>

              <div>
                <span className="stat__label">Evaluation Timestamp</span>
                <span className="stat__value" style={{ fontSize: '0.9rem', display: 'block' }}>
                  {formatDateTime(timestamp)}
                </span>
              </div>

              <div>
                <span className="stat__label">Metric Definitions Version</span>
                <span className="stat__value" style={{ fontSize: '0.9rem', display: 'block' }}>
                  {runItem.metric_definition_version || runItem.measurement_method_version || accuracy.benchmark_version
                    ? `v${runItem.metric_definition_version || runItem.measurement_method_version || accuracy.benchmark_version}`
                    : UNAVAILABLE}
                </span>
              </div>
            </div>
          </div>

          {/* Accuracy & Discovery Metrics */}
          <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
            <span className="volume-label">Measured Scientific Accuracy</span>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 'var(--space-3)', marginTop: 'var(--space-2)' }}>
              <div className="stat">
                <span className="stat__label">Hit@1</span>
                <span className="stat__value">{formatHitRate(accuracy.hit_at_1?.value ?? runItem.hit_at_1, 2)}</span>
              </div>
              <div className="stat">
                <span className="stat__label">Hit@3</span>
                <span className="stat__value">{formatHitRate(accuracy.hit_at_3?.value ?? runItem.hit_at_3, 2)}</span>
              </div>
              <div className="stat">
                <span className="stat__label">Hit@5</span>
                <span className="stat__value">{formatHitRate(accuracy.hit_at_5?.value ?? runItem.hit_at_5, 2)}</span>
              </div>
              <div className="stat">
                <span className="stat__label">MRR</span>
                <span className="stat__value">{formatDecimal(accuracy.mrr?.value ?? runItem.reciprocal_rank, 4)}</span>
              </div>
              <div className="stat">
                <span className="stat__label">Held-out MAE</span>
                <span className="stat__value">{formatDecimal(accuracy.held_out_mae?.value ?? runItem.held_out_mae, 4)}</span>
              </div>
              <div className="stat">
                <span className="stat__label">Held-out RMSE</span>
                <span className="stat__value">{formatDecimal(accuracy.held_out_rmse?.value ?? runItem.held_out_rmse, 4)}</span>
              </div>
            </div>
          </div>

          {/* Query Efficiency & Runtime */}
          <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
            <span className="volume-label">Query Efficiency & Execution Duration</span>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 'var(--space-3)', marginTop: 'var(--space-2)' }}>
              <div>
                <span className="stat__label">Queries Executed</span>
                <span className="stat__value" style={{ fontSize: '1.2rem' }}>
                  {formatInteger(efficiency.queries_executed ?? runItem.queries_executed)}
                  {efficiency.query_budget && ` / ${efficiency.query_budget}`}
                </span>
              </div>

              <div>
                <span className="stat__label">Queries to Target</span>
                <span className="stat__value" style={{ fontSize: '1.2rem' }}>
                  {efficiency.queries_to_target !== null && efficiency.queries_to_target !== undefined
                    ? formatInteger(efficiency.queries_to_target)
                    : runItem.queries_to_target ?? UNAVAILABLE}
                </span>
              </div>

              <div>
                <span className="stat__label">Wall-clock Time</span>
                <span className="stat__value" style={{ fontSize: '1.2rem' }}>
                  {runtime.total_wall_clock_ms ? `${(runtime.total_wall_clock_ms / 1000).toFixed(2)}s` : UNAVAILABLE}
                </span>
              </div>

              <div>
                <span className="stat__label">Compute Cost</span>
                <span className="stat__value" style={{ fontSize: '1.2rem' }}>
                  {cost.settled_cost_usd !== null && cost.settled_cost_usd !== undefined
                    ? `$${cost.settled_cost_usd.toFixed(4)}`
                    : cost.estimated_cost_usd !== null && cost.estimated_cost_usd !== undefined
                    ? `$${cost.estimated_cost_usd.toFixed(4)} (est)`
                    : UNAVAILABLE}
                </span>
              </div>
            </div>
          </div>

          {/* Privacy & Secrets Protection */}
          <div style={{ padding: 'var(--space-3)', background: 'rgba(201,169,98,0.06)', borderRadius: 'var(--radius)', display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
            <ShieldIcon size={18} style={{ color: 'var(--color-accent)' }} />
            <span className="muted" style={{ fontSize: '0.82rem' }}>
              <strong>Evaluator Security Invariant:</strong> True governing equations, parameters, and secret seeds are kept strictly inside the evaluator boundary and are never stored or leaked.
            </span>
          </div>
        </div>

        <div className="dialog__actions" style={{ borderTop: '1px solid rgba(201,169,98,0.25)', paddingTop: 'var(--space-3)' }}>
          <button type="button" className="button button--secondary" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
