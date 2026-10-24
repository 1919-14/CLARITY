import { useId } from 'react';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';
import { formatDateTime, formatDecimal, UNAVAILABLE } from '../../utils/formatting.js';
import { AlertIcon } from '../icons.jsx';

export function HypothesisComparisonModal({
  open,
  hypotheses = [],
  onClose,
}) {
  const titleId = useId();
  const containerRef = useModalFocusTrap({ open, onClose });

  if (!open || hypotheses.length < 2) return null;

  // Check comparability guards:
  // 1. Are they in different evaluation tiers?
  // 2. Are they on different dataset versions / snapshots?
  const tiers = new Set(
    hypotheses.map(
      (h) => (h.scientific_ranking || h.ranking)?.evaluation_tier || h.latest_evaluation?.tier || 'unknown',
    ),
  );
  const datasetVersions = new Set(
    hypotheses.map(
      (h) => h.latest_evaluation?.dataset_snapshot_id || h.latest_evaluation?.dataset_version || 'unknown',
    ),
  );

  const hasTierIncomparability = tiers.size > 1;
  const hasDatasetIncomparability = datasetVersions.size > 1 && !datasetVersions.has('unknown');
  const showComparabilityWarning = hasTierIncomparability || hasDatasetIncomparability;

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
        style={{ width: 'min(100%, 64rem)', maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >

        <p className="volume-label">Scientific Assessment</p>
        <h2 className="dialog__title" id={titleId}>
          Side-by-Side Candidate Comparison ({hypotheses.length})
        </h2>
        <p className="dialog__body">
          Compare scientific formulation, predictive accuracy, and evidence status across competing hypotheses.
        </p>

        {showComparabilityWarning && (
          <div
            className="warning-banner"
            role="alert"
            style={{
              display: 'flex',
              gap: 'var(--space-3)',
              alignItems: 'flex-start',
              padding: 'var(--space-3) var(--space-4)',
              background: 'rgba(217, 119, 6, 0.15)',
              border: '1px solid rgba(217, 119, 6, 0.5)',
              borderRadius: 'var(--radius)',
              margin: 'var(--space-3) 0',
            }}
          >
            <AlertIcon size={20} style={{ color: '#d97706', flexShrink: 0, marginTop: '2px' }} />
            <div>
              <strong style={{ color: '#d97706' }}>Comparability Warning:</strong>
              <p style={{ margin: '4px 0 0', fontSize: '0.88rem' }}>
                {hasTierIncomparability && hasDatasetIncomparability
                  ? 'Selected candidates have differing evaluation tiers AND dataset versions. Direct numerical comparisons may be misleading or scientifically invalid.'
                  : hasTierIncomparability
                  ? 'Selected candidates belong to different evaluation tiers (e.g. held-out validated vs. training-only or unevaluated). Within-cohort rankings are not directly interchangeable.'
                  : 'Selected candidates were evaluated against different dataset snapshots. Metric differences may reflect differing observation splits.'}
              </p>
            </div>
          </div>
        )}

        <div className="table-scroll" style={{ flex: 1, overflowY: 'auto', margin: 'var(--space-4) 0' }}>
          <table className="data-table data-table--compact comparison-table">
            <thead>
              <tr>
                <th scope="col" style={{ width: '200px' }}>Attribute</th>
                {hypotheses.map((h) => (
                  <th key={h.hypothesis_id} scope="col">
                    <code className="id-text">{h.hypothesis_id}</code>
                    <div style={{ marginTop: 'var(--space-1)' }}>
                      <span className={`badge badge--${h.origin === 'researcher' ? 'accent' : 'neutral'}`}>
                        {h.origin === 'researcher' ? 'Researcher' : 'Agent'}
                      </span>
                      {h.is_pinned && <span className="badge badge--brass" style={{ marginLeft: '4px' }}>Pinned</span>}
                      {h.is_excluded && <span className="badge badge--danger" style={{ marginLeft: '4px' }}>Excluded</span>}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Formula</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    <code className="formula-text" style={{ fontSize: '0.95rem' }}>
                      {h.latest_revision?.formula || UNAVAILABLE}
                    </code>
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Model Family</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>{h.model_family || UNAVAILABLE}</td>
                ))}
              </tr>

              <tr>
                <th scope="row">Complexity</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    {h.latest_revision?.complexity !== undefined && h.latest_revision?.complexity !== null
                      ? h.latest_revision.complexity
                      : UNAVAILABLE}
                  </td>
                ))}
              </tr>

              <tr style={{ background: 'rgba(201, 169, 98, 0.05)' }}>
                <th scope="row">Within-Cohort Rank</th>
                {hypotheses.map((h) => {
                  const rank = (h.scientific_ranking || h.ranking)?.within_cohort_rank;
                  return (
                    <td key={h.hypothesis_id}>
                      <strong>{rank !== null && rank !== undefined ? `#${rank}` : UNAVAILABLE}</strong>
                    </td>
                  );
                })}
              </tr>

              <tr>
                <th scope="row">Plausibility Status</th>
                {hypotheses.map((h) => {
                  const status = (h.scientific_ranking || h.ranking)?.plausibility_status;
                  return (
                    <td key={h.hypothesis_id}>
                      {status ? (
                        <span className={`status-badge status-badge--${status === 'PLAUSIBLE' ? 'success' : status === 'CONTRADICTED' ? 'danger' : 'warning'}`}>
                          {status}
                        </span>
                      ) : (
                        <span className="muted">{UNAVAILABLE}</span>
                      )}
                    </td>
                  );
                })}
              </tr>

              <tr>
                <th scope="row">Evaluation Tier</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    <code>{(h.scientific_ranking || h.ranking)?.evaluation_tier || UNAVAILABLE}</code>
                  </td>
                ))}

              </tr>

              <tr>
                <th scope="row">Held-out MAE (lower is better)</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    <strong>{formatDecimal(h.latest_evaluation?.held_out_metrics?.mae)}</strong>
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Held-out RMSE</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    {formatDecimal(h.latest_evaluation?.held_out_metrics?.rmse)}
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Held-out R²</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    {formatDecimal(h.latest_evaluation?.held_out_metrics?.r2)}
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Training MAE</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    {formatDecimal(h.latest_evaluation?.metrics?.mae)}
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Training RMSE</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    {formatDecimal(h.latest_evaluation?.metrics?.rmse)}
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Dataset Snapshot</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id}>
                    <span className="muted">
                      {h.latest_evaluation?.dataset_snapshot_id || h.latest_evaluation?.dataset_version || UNAVAILABLE}
                    </span>
                  </td>
                ))}
              </tr>

              <tr>
                <th scope="row">Evaluation Timestamp</th>
                {hypotheses.map((h) => (
                  <td key={h.hypothesis_id} className="muted">
                    {formatDateTime(h.latest_evaluation?.evaluated_at)}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>

        <div className="dialog__actions">
          <button type="button" className="button button--secondary" onClick={onClose}>
            Close comparison
          </button>
        </div>
      </div>
    </div>
  );
}
