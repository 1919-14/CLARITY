import { useEffect, useId, useState } from 'react';
import { getHypothesisEvaluations, getHypothesisEvidence, getHypothesisRevisions } from '../../api/hypotheses.js';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';
import { formatDateTime, formatDecimal, formatInteger, UNAVAILABLE } from '../../utils/formatting.js';
import { EmptyState } from '../feedback/EmptyState.jsx';
import { ErrorState } from '../feedback/ErrorState.jsx';
import { LoadingState } from '../feedback/LoadingState.jsx';
import { BanIcon, PinIcon, RefreshIcon } from '../icons.jsx';
import { StatusBadge } from '../status/StatusBadge.jsx';

export function HypothesisDetailModal({
  open,
  hypothesis,
  busy = false,
  onClose,
  onPinToggle,
  onExcludeToggle,
  onReevaluate,
  reevaluating = false,
}) {
  const titleId = useId();
  const containerRef = useModalFocusTrap({ open, onClose, busy });
  const [activeTab, setActiveTab] = useState('overview'); // 'overview' | 'evidence' | 'revisions' | 'evaluations'

  // Async data for evidence, revisions, evaluations
  const [evidenceData, setEvidenceData] = useState(null);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [evidenceError, setEvidenceError] = useState(null);

  const [revisionsData, setRevisionsData] = useState(null);
  const [revisionsLoading, setRevisionsLoading] = useState(false);
  const [revisionsError, setRevisionsError] = useState(null);

  const [evaluationsData, setEvaluationsData] = useState(null);
  const [evaluationsLoading, setEvaluationsLoading] = useState(false);
  const [evaluationsError, setEvaluationsError] = useState(null);

  // Evidence partition/classification filter
  const [evidenceFilter, setEvidenceFilter] = useState('all'); // 'all' | 'supporting' | 'contradicting' | 'indeterminate'

  const hypId = hypothesis?.hypothesis_id;

  useEffect(() => {
    if (!open || !hypId) {
      setActiveTab('overview');
      setEvidenceData(null);
      setRevisionsData(null);
      setEvaluationsData(null);
      return;
    }

    // Load evidence
    setEvidenceLoading(true);
    setEvidenceError(null);
    getHypothesisEvidence(hypId)
      .then((res) => setEvidenceData(res))
      .catch((err) => setEvidenceError(err))
      .finally(() => setEvidenceLoading(false));

    // Load revisions
    setRevisionsLoading(true);
    setRevisionsError(null);
    getHypothesisRevisions(hypId)
      .then((res) => setRevisionsData(res?.revisions || []))
      .catch((err) => setRevisionsError(err))
      .finally(() => setRevisionsLoading(false));

    // Load evaluations
    setEvaluationsLoading(true);
    setEvaluationsError(null);
    getHypothesisEvaluations(hypId)
      .then((res) => setEvaluationsData(res?.evaluations || []))
      .catch((err) => setEvaluationsError(err))
      .finally(() => setEvaluationsLoading(false));
  }, [open, hypId]);

  if (!open || !hypothesis) return null;

  const rev = hypothesis.latest_revision;
  const evalRec = hypothesis.latest_evaluation;
  const ranking = hypothesis.scientific_ranking || hypothesis.ranking;

  const trainingMae = evalRec?.metrics?.mae;
  const trainingRmse = evalRec?.metrics?.rmse;
  const trainingR2 = evalRec?.metrics?.r2;
  const heldOutMae = evalRec?.held_out_metrics?.mae;
  const heldOutRmse = evalRec?.held_out_metrics?.rmse;
  const heldOutR2 = evalRec?.held_out_metrics?.r2;

  // Evidence observations: supports records or observations
  const observations = evidenceData?.records || evidenceData?.observations || [];
  const totalEvidenceCount =
    evidenceData?.summary?.total_observations ??
    evidenceData?.summary?.total_evaluated ??
    observations.length;

  const filteredObservations = observations.filter((obs) => {
    if (evidenceFilter === 'all') return true;
    return obs.classification === evidenceFilter;
  });

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget && !busy) onClose();
      }}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className="dialog dialog--large"
        style={{ width: 'min(100%, 54rem)', maxHeight: '90vh', display: 'flex', flexDirection: 'column' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: 'var(--space-3)' }}>
          <div>
            <p className="volume-label">Candidate Details & Evidence</p>
            <h2 className="dialog__title" id={titleId} style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-3)', margin: 0 }}>
              <code className="id-text" style={{ fontSize: '1.4rem' }}>{hypothesis.hypothesis_id}</code>
              <span className={`badge badge--${hypothesis.origin === 'researcher' ? 'accent' : 'neutral'}`}>
                {hypothesis.origin === 'researcher' ? 'Researcher Proposed' : 'Agent Generated'}
              </span>
              {hypothesis.is_pinned && <span className="badge badge--brass">Pinned</span>}
              {hypothesis.is_excluded && <span className="badge badge--danger">Excluded</span>}
            </h2>
          </div>

          <div style={{ display: 'flex', gap: 'var(--space-2)' }}>
            <button
              type="button"
              className="button button--secondary button--small"
              onClick={onPinToggle}
              disabled={busy}
              title="Pinning is a visual preference and does not alter scientific ranking."
            >
              <PinIcon size={14} />
              <span>{hypothesis.is_pinned ? 'Unpin' : 'Pin candidate'}</span>
            </button>

            <button
              type="button"
              className={`button button--${hypothesis.is_excluded ? 'secondary' : 'danger'} button--small`}
              onClick={onExcludeToggle}
              disabled={busy}
            >
              <BanIcon size={14} />
              <span>{hypothesis.is_excluded ? 'Restore' : 'Exclude'}</span>
            </button>

            <button
              type="button"
              className="button button--primary button--small"
              onClick={onReevaluate}
              disabled={busy || reevaluating || hypothesis.is_excluded}
              title={hypothesis.is_excluded ? 'Excluded hypotheses cannot be re-evaluated' : 'Request re-evaluation'}
            >
              <RefreshIcon size={14} />
              <span>{reevaluating ? 'Queuing…' : 'Re-evaluate'}</span>
            </button>
          </div>
        </div>

        {/* Tab Navigation */}
        <div className="tab-nav" role="tablist" style={{ display: 'flex', gap: 'var(--space-2)', borderBottom: '1px solid rgba(201,169,98,0.25)', marginTop: 'var(--space-4)', paddingBottom: 'var(--space-2)' }}>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'overview'}
            className={`tab-button ${activeTab === 'overview' ? 'tab-button--active' : ''}`}
            onClick={() => setActiveTab('overview')}
          >
            Overview & Metrics
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'evidence'}
            className={`tab-button ${activeTab === 'evidence' ? 'tab-button--active' : ''}`}
            onClick={() => setActiveTab('evidence')}
          >
            Evidence Observations {evidenceData ? `(${totalEvidenceCount})` : ''}
          </button>

          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'revisions'}
            className={`tab-button ${activeTab === 'revisions' ? 'tab-button--active' : ''}`}
            onClick={() => setActiveTab('revisions')}
          >
            Revisions {revisionsData ? `(${revisionsData.length})` : ''}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={activeTab === 'evaluations'}
            className={`tab-button ${activeTab === 'evaluations' ? 'tab-button--active' : ''}`}
            onClick={() => setActiveTab('evaluations')}
          >
            Evaluations {evaluationsData ? `(${evaluationsData.length})` : ''}
          </button>
        </div>

        {/* Tab Contents */}
        <div style={{ flex: 1, overflowY: 'auto', padding: 'var(--space-4) 0' }}>
          {activeTab === 'overview' && (
            <div className="detail-stack">
              <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
                <p className="volume-label">Formula & Formulation</p>
                <div style={{ marginTop: 'var(--space-2)' }}>
                  <code className="formula-text" style={{ fontSize: '1.25rem', display: 'block', padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)' }}>
                    {rev?.formula || UNAVAILABLE}
                  </code>
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 'var(--space-4)', marginTop: 'var(--space-4)' }}>
                  <div>
                    <span className="field__inline-label">Model Family:</span>{' '}
                    <strong>{hypothesis.model_family || UNAVAILABLE}</strong>
                  </div>
                  <div>
                    <span className="field__inline-label">Complexity:</span>{' '}
                    <strong>{rev?.complexity !== undefined && rev?.complexity !== null ? rev.complexity : UNAVAILABLE}</strong>
                  </div>
                  <div>
                    <span className="field__inline-label">Current Revision:</span>{' '}
                    <strong>rev {hypothesis.current_revision}</strong>
                  </div>
                  <div>
                    <span className="field__inline-label">Last Updated:</span>{' '}
                    <span>{formatDateTime(hypothesis.updated_at)}</span>
                  </div>
                </div>

                {rev?.parameters && Object.keys(rev.parameters).length > 0 && (
                  <div style={{ marginTop: 'var(--space-4)' }}>
                    <span className="field__inline-label">Fitted Parameters:</span>
                    <pre style={{ margin: 'var(--space-2) 0 0', padding: 'var(--space-2)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)', fontSize: '0.85rem' }}>
                      {JSON.stringify(rev.parameters, null, 2)}
                    </pre>
                  </div>
                )}
              </div>

              {/* Scientific Ranking & Plausibility */}
              <div className="card" style={{ padding: 'var(--space-4)', marginBottom: 'var(--space-4)' }}>
                <p className="volume-label">Scientific Assessment & Cohort Rank</p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 'var(--space-4)', marginTop: 'var(--space-3)' }}>
                  <div className="stat">
                    <span className="stat__label">Within-Cohort Rank</span>
                    <span className="stat__value" style={{ fontSize: '1.4rem' }}>
                      {ranking?.within_cohort_rank !== null && ranking?.within_cohort_rank !== undefined
                        ? `#${ranking.within_cohort_rank}`
                        : UNAVAILABLE}
                    </span>
                    <span className="stat__hint">Ranked among comparable candidates</span>
                  </div>

                  <div className="stat">
                    <span className="stat__label">Plausibility Status</span>
                    <span className="stat__value" style={{ fontSize: '1.2rem' }}>
                      {ranking?.plausibility_status ? (
                        <span className={`status-badge status-badge--${ranking.plausibility_status === 'PLAUSIBLE' ? 'success' : ranking.plausibility_status === 'CONTRADICTED' ? 'danger' : 'warning'}`}>
                          {ranking.plausibility_status}
                        </span>
                      ) : (
                        <span className="muted">{UNAVAILABLE}</span>
                      )}
                    </span>
                    <span className="stat__hint">Evidence consistency status</span>
                  </div>

                  <div className="stat">
                    <span className="stat__label">Evaluation Tier</span>
                    <span className="stat__value" style={{ fontSize: '1.1rem' }}>
                      {ranking?.evaluation_tier || UNAVAILABLE}
                    </span>
                    <span className="stat__hint">Validation comparability tier</span>
                  </div>
                </div>

                <p className="muted" style={{ fontSize: '0.82rem', marginTop: 'var(--space-3)' }}>
                  ℹ️ Scientific rankings reflect predictive accuracy on held-out validation partitions and model parsimony.
                  Pinning is purely a user workflow flag and is independent of scientific validation.
                </p>
              </div>

              {/* Performance Metrics */}
              <div className="card" style={{ padding: 'var(--space-4)' }}>
                <p className="volume-label">Predictive Error Metrics</p>
                <p className="muted" style={{ fontSize: '0.85rem', margin: 'var(--space-1) 0 var(--space-3)' }}>
                  Note: Lower MAE and RMSE values indicate superior predictive accuracy. Missing partitions stay unavailable.
                </p>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 'var(--space-4)' }}>
                  <div style={{ padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
                    <h4 style={{ margin: '0 0 var(--space-2)', fontSize: '0.95rem' }}>Held-out Validation Data</h4>
                    <div className="detail-list">
                      <div className="detail-list__row">
                        <span className="muted">MAE:</span>
                        <strong>{formatDecimal(heldOutMae)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">RMSE:</span>
                        <strong>{formatDecimal(heldOutRmse)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">R²:</span>
                        <strong>{formatDecimal(heldOutR2)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">Snapshot / Version:</span>
                        <span>{evalRec?.dataset_snapshot_id || evalRec?.dataset_version || UNAVAILABLE}</span>
                      </div>
                    </div>
                  </div>

                  <div style={{ padding: 'var(--space-3)', background: 'var(--color-bg)', border: 'var(--border-hairline)', borderRadius: 'var(--radius)' }}>
                    <h4 style={{ margin: '0 0 var(--space-2)', fontSize: '0.95rem' }}>Training Data</h4>
                    <div className="detail-list">
                      <div className="detail-list__row">
                        <span className="muted">MAE:</span>
                        <strong>{formatDecimal(trainingMae)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">RMSE:</span>
                        <strong>{formatDecimal(trainingRmse)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">R²:</span>
                        <strong>{formatDecimal(trainingR2)}</strong>
                      </div>
                      <div className="detail-list__row">
                        <span className="muted">Evaluated At:</span>
                        <span>{formatDateTime(evalRec?.evaluated_at)}</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          )}

          {activeTab === 'evidence' && (
            <div>
              {evidenceLoading && <LoadingState label="Loading evidence observations…" />}
              {evidenceError && <ErrorState title="Could not load evidence" error={evidenceError} />}
              {!evidenceLoading && !evidenceError && evidenceData && (
                <div>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: 'var(--space-3)', marginBottom: 'var(--space-4)' }}>
                    <div style={{ display: 'flex', gap: 'var(--space-3)' }}>
                      <span className="badge badge--success">
                        Supporting: {formatInteger(evidenceData.summary?.supporting_count)}
                      </span>
                      <span className="badge badge--danger">
                        Contradicting: {formatInteger(evidenceData.summary?.contradicting_count)}
                      </span>
                      <span className="badge badge--neutral">
                        Indeterminate: {formatInteger(evidenceData.summary?.indeterminate_count)}
                      </span>
                      {evidenceData.dataset_snapshot_id && (
                        <span className="badge badge--neutral">
                          Snapshot: {evidenceData.dataset_snapshot_id}
                        </span>
                      )}
                    </div>

                    <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                      <label htmlFor="filter-evidence" className="field__inline-label">Filter:</label>
                      <select
                        id="filter-evidence"
                        className="select select--small"
                        value={evidenceFilter}
                        onChange={(e) => setEvidenceFilter(e.target.value)}
                      >
                        <option value="all">All ({observations.length})</option>
                        <option value="supporting">Supporting only</option>
                        <option value="contradicting">Contradicting only</option>
                        <option value="indeterminate">Indeterminate only</option>
                      </select>
                    </div>
                  </div>

                  <p className="muted" style={{ fontSize: '0.85rem', marginBottom: 'var(--space-4)' }}>
                    Observations are classified by tolerance formula: $T = \tau_{'{'}abs{'}'} + \tau_{'{'}rel{'}'} \cdot |y_{'{'}obs{'}'}|$.
                    Residuals within tolerance are supporting; residuals exceeding tolerance contradict the hypothesis.
                    Oracle ground truth internals and evaluator hidden rules are never accessible.
                  </p>

                  {filteredObservations.length === 0 ? (
                    <EmptyState
                      title="No observations match filter"
                      message="There are no evidence observations in this classification category."
                    />
                  ) : (
                    <div className="table-scroll" style={{ maxHeight: '400px' }}>
                      <table className="data-table data-table--compact">
                        <thead>
                          <tr>
                            <th>Obs ID</th>
                            <th>Inputs (x)</th>
                            <th>Observed (y)</th>
                            <th>Predicted (ŷ)</th>
                            <th>Residual (|y - ŷ|)</th>
                            <th>Tolerance (T)</th>
                            <th>Partition</th>
                            <th>Classification</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredObservations.map((obs) => {
                            const isSupporting = obs.classification === 'supporting';
                            const isContradicting = obs.classification === 'contradicting';
                            const inputValues = obs.input_values ?? obs.inputs;
                            const toleranceValue = obs.tolerance ?? obs.tolerance_threshold;
                            return (
                              <tr key={obs.observation_id}>
                                <td><code className="id-text">{obs.observation_id}</code></td>
                                <td>
                                  {typeof inputValues === 'object' && inputValues !== null
                                    ? JSON.stringify(inputValues)
                                    : String(inputValues ?? UNAVAILABLE)}
                                </td>
                                <td><strong>{formatDecimal(obs.observed_value)}</strong></td>
                                <td>{formatDecimal(obs.predicted_value)}</td>
                                <td>{formatDecimal(obs.residual)}</td>
                                <td className="muted">{formatDecimal(toleranceValue)}</td>
                                <td>
                                  <span className="badge badge--neutral">{obs.partition || 'unspecified'}</span>
                                  {obs.membership_verified === false && (
                                    <span
                                      className="badge badge--warning"
                                      style={{ marginLeft: '4px', fontSize: '0.6rem' }}
                                      title="Observed after this evaluation was performed"
                                    >
                                      Post-eval
                                    </span>
                                  )}
                                </td>
                                <td>
                                  <span className={`status-badge status-badge--${isSupporting ? 'success' : isContradicting ? 'danger' : 'neutral'}`}>
                                    {obs.classification}
                                  </span>
                                </td>
                              </tr>
                            );
                          })}

                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {activeTab === 'revisions' && (
            <div>
              {revisionsLoading && <LoadingState label="Loading revisions…" />}
              {revisionsError && <ErrorState title="Could not load revision history" error={revisionsError} />}
              {!revisionsLoading && !revisionsError && revisionsData && (
                <div className="revision-timeline">
                  {revisionsData.length === 0 ? (
                    <EmptyState title="No revisions recorded" message="No previous revisions exist for this candidate." />
                  ) : (
                    revisionsData.map((revItem) => (
                      <div key={revItem.revision_number} className="card" style={{ padding: 'var(--space-3)', marginBottom: 'var(--space-3)' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <strong>Revision #{revItem.revision_number}</strong>
                          <span className="muted" style={{ fontSize: '0.85rem' }}>{formatDateTime(revItem.created_at)}</span>
                        </div>
                        <code className="formula-text" style={{ display: 'block', margin: 'var(--space-2) 0', fontSize: '1.05rem' }}>
                          {revItem.formula}
                        </code>
                        <div className="muted" style={{ fontSize: '0.85rem' }}>
                          Complexity: <strong>{revItem.complexity ?? UNAVAILABLE}</strong> · Source: <strong>{revItem.creation_source || 'agent'}</strong>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              )}
            </div>
          )}

          {activeTab === 'evaluations' && (
            <div>
              {evaluationsLoading && <LoadingState label="Loading evaluations…" />}
              {evaluationsError && <ErrorState title="Could not load evaluations" error={evaluationsError} />}
              {!evaluationsLoading && !evaluationsError && evaluationsData && (
                <div>
                  {evaluationsData.length === 0 ? (
                    <EmptyState title="No evaluations recorded" message="This candidate has not undergone evaluation yet." />
                  ) : (
                    <div className="table-scroll">
                      <table className="data-table">
                        <thead>
                          <tr>
                            <th>Eval ID</th>
                            <th>Status</th>
                            <th>Training MAE</th>
                            <th>Held-out MAE</th>
                            <th>Evaluated At</th>
                          </tr>
                        </thead>
                        <tbody>
                          {evaluationsData.map((ev) => (
                            <tr key={ev.evaluation_id}>
                              <td><code className="id-text">{ev.evaluation_id}</code></td>
                              <td><StatusBadge status={ev.status} /></td>
                              <td>{formatDecimal(ev.metrics?.mae)}</td>
                              <td>{formatDecimal(ev.held_out_metrics?.mae)}</td>
                              <td className="muted">{formatDateTime(ev.evaluated_at)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </div>
          )}
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
