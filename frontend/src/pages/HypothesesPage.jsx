import { useCallback, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';

import {
  proposeHypothesis,
  reevaluateHypothesis,
  updateHypothesisFlags,
} from '../api/hypotheses.js';
import { listRunHypotheses } from '../api/runs.js';
import { EmptyState } from '../components/feedback/EmptyState.jsx';


import { ErrorState } from '../components/feedback/ErrorState.jsx';
import { LoadingState } from '../components/feedback/LoadingState.jsx';
import { ConfirmExclusionModal } from '../components/hypotheses/ConfirmExclusionModal.jsx';
import { HypothesisComparisonModal } from '../components/hypotheses/HypothesisComparisonModal.jsx';
import { HypothesisDetailModal } from '../components/hypotheses/HypothesisDetailModal.jsx';
import { HypothesisFilterSortToolbar } from '../components/hypotheses/HypothesisFilterSortToolbar.jsx';
import { ProposeHypothesisModal } from '../components/hypotheses/ProposeHypothesisModal.jsx';
import { BanIcon, PinIcon, RefreshIcon } from '../components/icons.jsx';
import { OrnamentDivider, PageHeader } from '../components/layout/PageHeader.jsx';
import { RunPicker } from '../components/runs/RunPicker.jsx';
import { StatusBadge } from '../components/status/StatusBadge.jsx';
import { useResource } from '../hooks/useResource.js';
import { useRuns } from '../hooks/useRuns.js';
import { formatDateTime, formatDecimal, UNAVAILABLE } from '../utils/formatting.js';

export function HypothesesPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { runs, isLoading: runsLoading, error: runsError, reload: reloadRuns } = useRuns({ limit: 50 });
  const [selectedRunId, setSelectedRunId] = useState(searchParams.get('run') || '');
  const runId = selectedRunId || runs?.[0]?.run_id || '';

  const loader = useCallback(
    ({ signal }) => listRunHypotheses(runId, { includeExcluded: true, signal }),
    [runId],
  );
  const { data, isLoading, isRefreshing, error, reload } = useResource(loader, {
    enabled: Boolean(runId),
  });

  const rawHypotheses = data?.hypotheses ?? [];

  // Filter & Search states
  const [filterOrigin, setFilterOrigin] = useState('all');
  const [filterStatus, setFilterStatus] = useState('all');
  const [filterLifecycle, setFilterLifecycle] = useState('all');
  const [searchQuery, setSearchQuery] = useState('');

  // Sort states
  const [sortBy, setSortBy] = useState('rank');
  const [sortOrder, setSortOrder] = useState('asc');

  // Multi-select for side-by-side comparison
  const [selectedHypothesisIds, setSelectedHypothesisIds] = useState(new Set());

  // Modals & Action states
  const [detailHypothesis, setDetailHypothesis] = useState(null);
  const [comparisonOpen, setComparisonOpen] = useState(false);
  const [proposeOpen, setProposeOpen] = useState(false);
  const [proposeBackendError, setProposeBackendError] = useState(null);
  const [proposing, setProposing] = useState(false);

  const [exclusionState, setExclusionState] = useState({
    open: false,
    hypothesis: null,
    action: 'exclude',
    busy: false,
  });

  const [reevaluatingIds, setReevaluatingIds] = useState(new Set());
  const [actionError, setActionError] = useState(null);

  const changeRun = (nextRunId) => {
    setSelectedRunId(nextRunId);
    setSelectedHypothesisIds(new Set());
    setSearchParams(nextRunId ? { run: nextRunId } : {}, { replace: true });
  };

  // Toggle selection for comparison
  const toggleSelectHypothesis = (id) => {
    setSelectedHypothesisIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const selectAllFiltered = (filteredList) => {
    if (selectedHypothesisIds.size === filteredList.length) {
      setSelectedHypothesisIds(new Set());
    } else {
      setSelectedHypothesisIds(new Set(filteredList.map((h) => h.hypothesis_id)));
    }
  };

  // Filter & Sort computation
  const filteredAndSortedHypotheses = useMemo(() => {
    let result = [...rawHypotheses];

    // Filter: Origin
    if (filterOrigin !== 'all') {
      result = result.filter((h) => {
        const origin = h.origin || (h.latest_revision?.creation_source === 'researcher' ? 'researcher' : 'agent');
        return origin === filterOrigin;
      });
    }

    // Filter: Evaluation Status
    if (filterStatus !== 'all') {
      result = result.filter((h) => {
        const status = h.latest_evaluation?.status;
        if (filterStatus === 'unevaluated') return !h.latest_evaluation;
        if (filterStatus === 'pending') return status === 'pending' || reevaluatingIds.has(h.hypothesis_id);
        return status === filterStatus;
      });
    }

    // Filter: Lifecycle State
    if (filterLifecycle !== 'all') {
      result = result.filter((h) => {
        if (filterLifecycle === 'active') return !h.is_excluded;
        if (filterLifecycle === 'pinned') return Boolean(h.is_pinned);
        if (filterLifecycle === 'excluded') return Boolean(h.is_excluded);
        return true;
      });
    }

    // Filter: Search Query
    if (searchQuery.trim()) {
      const q = searchQuery.trim().toLowerCase();
      result = result.filter((h) => {
        const idMatch = h.hypothesis_id.toLowerCase().includes(q);
        const formulaMatch = h.latest_revision?.formula?.toLowerCase().includes(q);
        return idMatch || formulaMatch;
      });
    }

    // Sorting
    result.sort((a, b) => {
      let comparison = 0;

      switch (sortBy) {
        case 'rank': {
          const rankA = a.scientific_ranking?.within_cohort_rank ?? a.scientific_ranking?.rank;
          const rankB = b.scientific_ranking?.within_cohort_rank ?? b.scientific_ranking?.rank;
          if (rankA === null || rankA === undefined) return 1;
          if (rankB === null || rankB === undefined) return -1;
          comparison = rankA - rankB;
          break;
        }
        case 'held_out_mae': {
          const valA = a.latest_evaluation?.held_out_metrics?.mae;
          const valB = b.latest_evaluation?.held_out_metrics?.mae;
          if (valA === null || valA === undefined) return 1;
          if (valB === null || valB === undefined) return -1;
          comparison = valA - valB;
          break;
        }
        case 'held_out_rmse': {
          const valA = a.latest_evaluation?.held_out_metrics?.rmse;
          const valB = b.latest_evaluation?.held_out_metrics?.rmse;
          if (valA === null || valA === undefined) return 1;
          if (valB === null || valB === undefined) return -1;
          comparison = valA - valB;
          break;
        }
        case 'training_mae': {
          const valA = a.latest_evaluation?.metrics?.mae;
          const valB = b.latest_evaluation?.metrics?.mae;
          if (valA === null || valA === undefined) return 1;
          if (valB === null || valB === undefined) return -1;
          comparison = valA - valB;
          break;
        }
        case 'complexity': {
          const compA = a.latest_revision?.complexity;
          const compB = b.latest_revision?.complexity;
          if (compA === null || compA === undefined) return 1;
          if (compB === null || compB === undefined) return -1;
          comparison = compA - compB;
          break;
        }
        case 'revision': {
          comparison = (a.current_revision || 0) - (b.current_revision || 0);
          break;
        }
        case 'updated_at': {
          const dateA = new Date(a.updated_at || 0).getTime();
          const dateB = new Date(b.updated_at || 0).getTime();
          comparison = dateA - dateB;
          break;
        }
        default:
          comparison = 0;
      }

      return sortOrder === 'asc' ? comparison : -comparison;
    });

    return result;
  }, [
    rawHypotheses,
    filterOrigin,
    filterStatus,
    filterLifecycle,
    searchQuery,
    sortBy,
    sortOrder,
    reevaluatingIds,
  ]);

  // Selected hypotheses for comparison modal
  const selectedHypotheses = useMemo(() => {
    return rawHypotheses.filter((h) => selectedHypothesisIds.has(h.hypothesis_id));
  }, [rawHypotheses, selectedHypothesisIds]);

  // Actions
  const handlePinToggle = async (hypothesis) => {
    setActionError(null);
    try {
      await updateHypothesisFlags(hypothesis.hypothesis_id, {
        is_pinned: !hypothesis.is_pinned,
        expected_revision: hypothesis.current_revision,
      });
      await reload();
      if (detailHypothesis && detailHypothesis.hypothesis_id === hypothesis.hypothesis_id) {
        setDetailHypothesis((prev) => ({ ...prev, is_pinned: !hypothesis.is_pinned }));
      }
    } catch (err) {
      setActionError(err);
    }
  };

  const openExclusionModal = (hypothesis, action) => {
    setExclusionState({ open: true, hypothesis, action, busy: false });
  };

  const handleExclusionConfirm = async ({ reason }) => {
    const { hypothesis, action } = exclusionState;
    if (!hypothesis) return;

    setExclusionState((prev) => ({ ...prev, busy: true }));
    setActionError(null);
    try {
      await updateHypothesisFlags(hypothesis.hypothesis_id, {
        is_excluded: action === 'exclude',
        reason,
        expected_revision: hypothesis.current_revision,
      });
      await reload();
      if (detailHypothesis && detailHypothesis.hypothesis_id === hypothesis.hypothesis_id) {
        setDetailHypothesis((prev) => ({ ...prev, is_excluded: action === 'exclude' }));
      }
      setExclusionState({ open: false, hypothesis: null, action: 'exclude', busy: false });
    } catch (err) {
      setActionError(err);
      setExclusionState((prev) => ({ ...prev, busy: false }));
    }
  };

  const handleReevaluate = async (hypothesis) => {
    const id = hypothesis.hypothesis_id;
    setActionError(null);
    setReevaluatingIds((prev) => new Set(prev).add(id));
    try {
      await reevaluateHypothesis(id);
      await reload();
    } catch (err) {
      setActionError(err);
    } finally {
      setReevaluatingIds((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }
  };

  const handleProposeSubmit = async (payload) => {
    setProposing(true);
    setProposeBackendError(null);
    try {
      await proposeHypothesis(runId, payload);
      setProposeOpen(false);
      await reload();
    } catch (err) {
      setProposeBackendError(err);
    } finally {
      setProposing(false);
    }
  };

  return (
    <div className="page-inner">
      <PageHeader
        volume="Volume VI · Hypotheses"
        title="Candidate explanations"
        description="Explore, compare, and manage competing explanations generated by CLARITY. Scientific ranking is determined by validated predictive accuracy on held-out observations and structural parsimony; pinning is purely a visual organization workflow."
      />
      <OrnamentDivider />

      {runsError ? (
        <ErrorState title="Runs could not be loaded" error={runsError} onRetry={reloadRuns} compact />
      ) : null}

      {actionError ? (
        <div className="error-banner" role="alert" style={{ marginBottom: 'var(--space-4)' }}>
          <p className="error-banner__message">
            Action failed: {actionError.message || String(actionError)}
          </p>
        </div>
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
        rawHypotheses.length === 0 ? (
          <EmptyState
            title="No hypotheses recorded for this run"
            message="The discovery engine has not proposed candidates yet, or this run was created without execution."
            action={
              <button
                type="button"
                className="button button--primary"
                onClick={() => setProposeOpen(true)}
              >
                Propose first hypothesis
              </button>
            }
          />
        ) : (
          <div className="hypothesis-explorer-section">
            <HypothesisFilterSortToolbar
              filterOrigin={filterOrigin}
              setFilterOrigin={setFilterOrigin}
              filterStatus={filterStatus}
              setFilterStatus={setFilterStatus}
              filterLifecycle={filterLifecycle}
              setFilterLifecycle={setFilterLifecycle}
              searchQuery={searchQuery}
              setSearchQuery={setSearchQuery}
              sortBy={sortBy}
              setSortBy={setSortBy}
              sortOrder={sortOrder}
              setSortOrder={setSortOrder}
              selectedCount={selectedHypothesisIds.size}
              onCompare={() => setComparisonOpen(true)}
              onPropose={() => {
                setProposeBackendError(null);
                setProposeOpen(true);
              }}
            />

            {filteredAndSortedHypotheses.length === 0 ? (
              <EmptyState
                title="No candidates match active filters"
                message="Try adjusting your origin, status, lifecycle, or search criteria."
                action={
                  <button
                    type="button"
                    className="button button--secondary button--small"
                    onClick={() => {
                      setFilterOrigin('all');
                      setFilterStatus('all');
                      setFilterLifecycle('all');
                      setSearchQuery('');
                    }}
                  >
                    Reset filters
                  </button>
                }
              />
            ) : (
              <div className="table-scroll">
                <table className="data-table data-table--wide hypothesis-table">
                  <caption className="visually-hidden">
                    Hypotheses recorded for the selected run
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col" style={{ width: '40px' }}>
                        <span className="visually-hidden">Select for comparison</span>
                        <input
                          type="checkbox"
                          aria-label="Select all hypotheses for comparison"
                          checked={
                            filteredAndSortedHypotheses.length > 0 &&
                            selectedHypothesisIds.size === filteredAndSortedHypotheses.length
                          }
                          onChange={() => selectAllFiltered(filteredAndSortedHypotheses)}
                        />
                      </th>
                      <th scope="col">Hypothesis</th>
                      <th scope="col">Formula & Complexity</th>
                      <th scope="col">Scientific Rank</th>
                      <th scope="col">Predictive Accuracy</th>
                      <th scope="col">Flags & Workflow</th>
                      <th scope="col">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredAndSortedHypotheses.map((hypothesis) => {
                      const revision = hypothesis.latest_revision;
                      const evaluation = hypothesis.latest_evaluation;
                      const ranking = hypothesis.scientific_ranking;
                      const isSelected = selectedHypothesisIds.has(hypothesis.hypothesis_id);
                      const isReevaluating = reevaluatingIds.has(hypothesis.hypothesis_id);
                      const isResearcher = hypothesis.origin === 'researcher' || revision?.creation_source === 'researcher';

                      const rankDisplay = ranking?.within_cohort_rank !== null && ranking?.within_cohort_rank !== undefined
                        ? `#${ranking.within_cohort_rank}`
                        : ranking?.rank !== null && ranking?.rank !== undefined
                        ? `#${ranking.rank}`
                        : UNAVAILABLE;

                      return (
                        <tr
                          key={hypothesis.hypothesis_id}
                          className={hypothesis.is_excluded ? 'row--muted' : ''}
                        >
                          <td>
                            <input
                              type="checkbox"
                              aria-label={`Select ${hypothesis.hypothesis_id} for comparison`}
                              checked={isSelected}
                              onChange={() => toggleSelectHypothesis(hypothesis.hypothesis_id)}
                            />
                          </td>

                          {/* Stable ID, Origin, Revision */}
                          <td data-label="Hypothesis">
                            <div style={{ display: 'flex', alignItems: 'center', gap: 'var(--space-2)' }}>
                              <code className="id-text">{hypothesis.hypothesis_id}</code>
                              <span
                                className={`badge badge--${isResearcher ? 'accent' : 'neutral'}`}
                                style={{ fontSize: '0.65rem' }}
                              >
                                {isResearcher ? 'Researcher' : 'Agent'}
                              </span>
                            </div>
                            <span className="muted block-note">
                              revision {hypothesis.current_revision} · updated {formatDateTime(hypothesis.updated_at)}
                            </span>
                          </td>

                          {/* Formula, Family, Complexity */}
                          <td data-label="Formula & Complexity">
                            {revision ? (
                              <>
                                <code className="formula-text">{revision.formula}</code>
                                <span className="muted block-note">
                                  {hypothesis.model_family} · complexity{' '}
                                  {revision.complexity !== undefined && revision.complexity !== null
                                    ? revision.complexity
                                    : UNAVAILABLE}
                                </span>
                              </>
                            ) : (
                              UNAVAILABLE
                            )}
                          </td>

                          {/* Scientific Rank (Visually separate from pinning/exclusion!) */}
                          <td data-label="Scientific Rank">
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                              <strong style={{ fontSize: '1.05rem' }}>
                                {rankDisplay}
                              </strong>
                              {ranking?.plausibility_status ? (
                                <span
                                  className={`status-badge status-badge--${
                                    ranking.plausibility_status === 'PLAUSIBLE'
                                      ? 'success'
                                      : ranking.plausibility_status === 'CONTRADICTED'
                                      ? 'danger'
                                      : 'warning'
                                  }`}
                                  style={{ fontSize: '0.65rem' }}
                                >
                                  {ranking.plausibility_status}
                                </span>
                              ) : null}
                              <span className="muted" style={{ fontSize: '0.72rem' }}>
                                {ranking?.evaluation_tier || 'Tier unassigned'}
                              </span>
                            </div>
                          </td>

                          {/* Predictive Accuracy & Evaluation Status */}
                          <td data-label="Predictive Accuracy">
                            {isReevaluating ? (
                              <span className="status-badge status-badge--active">Evaluating…</span>
                            ) : !evaluation ? (
                              <span className="muted">Not evaluated</span>
                            ) : (
                              <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                                <StatusBadge
                                  status={evaluation.status}
                                  label={evaluation.status === 'completed' ? 'Evaluated' : undefined}
                                />
                                <span className="muted" style={{ fontSize: '0.78rem' }}>
                                  MAE {formatDecimal(evaluation.metrics?.mae)} · RMSE {formatDecimal(evaluation.metrics?.rmse)}
                                </span>
                                {evaluation.held_out_metrics ? (
                                  <span style={{ fontSize: '0.82rem' }}>
                                    Held-out MAE {formatDecimal(evaluation.held_out_metrics.mae)}
                                  </span>
                                ) : (
                                  <span className="muted" style={{ fontSize: '0.82rem' }}>
                                    Held-out metrics {UNAVAILABLE.toLowerCase()}
                                  </span>
                                )}
                              </div>
                            )}
                          </td>

                          {/* Lifecycle Flags (Pinned vs Excluded) */}
                          <td data-label="Flags & Workflow">
                            <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                              {hypothesis.is_pinned ? (
                                <span
                                  className="badge badge--brass"
                                  title="Pinned for researcher visual preference. Does not affect scientific ranking."
                                >
                                  ★ Pinned
                                </span>
                              ) : (
                                <span className="muted" style={{ fontSize: '0.78rem' }}>Unpinned</span>
                              )}

                              {hypothesis.is_excluded ? (
                                <span
                                  className="badge badge--danger"
                                  title="Excluded from active cohort without deleting history."
                                >
                                  Excluded
                                </span>
                              ) : (
                                <span className="muted" style={{ fontSize: '0.78rem' }}>Active cohort</span>
                              )}
                            </div>
                          </td>

                          {/* Action Buttons */}
                          <td data-label="Actions">
                            <div style={{ display: 'flex', gap: 'var(--space-1)', flexWrap: 'wrap' }}>
                              <button
                                type="button"
                                className="button button--secondary button--small"
                                onClick={() => setDetailHypothesis(hypothesis)}
                                title="Inspect hypothesis evidence, revisions, and evaluations"
                              >
                                Details
                              </button>

                              <button
                                type="button"
                                className="button button--ghost button--small"
                                onClick={() => handlePinToggle(hypothesis)}
                                title="Pinning is a visual preference to keep this candidate in view; it does not alter scientific scores."
                              >
                                <PinIcon size={13} />
                                <span>{hypothesis.is_pinned ? 'Unpin' : 'Pin'}</span>
                              </button>

                              <button
                                type="button"
                                className={`button button--ghost button--small ${hypothesis.is_excluded ? '' : 'text-danger'}`}
                                onClick={() => openExclusionModal(hypothesis, hypothesis.is_excluded ? 'restore' : 'exclude')}
                                title={hypothesis.is_excluded ? 'Restore to active cohort' : 'Exclude from search without deleting history'}
                              >
                                <BanIcon size={13} />
                                <span>{hypothesis.is_excluded ? 'Restore' : 'Exclude'}</span>
                              </button>

                              <button
                                type="button"
                                className="button button--ghost button--small"
                                onClick={() => handleReevaluate(hypothesis)}
                                disabled={hypothesis.is_excluded || isReevaluating}
                                title={hypothesis.is_excluded ? 'Excluded hypotheses cannot be re-evaluated' : 'Request fresh evaluation'}
                              >
                                <RefreshIcon size={13} />
                                <span>{isReevaluating ? 'Evaluating…' : 'Re-eval'}</span>
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )
      ) : null}

      {/* Detail & Evidence Modal */}
      <HypothesisDetailModal
        open={Boolean(detailHypothesis)}
        hypothesis={detailHypothesis}
        onClose={() => setDetailHypothesis(null)}
        onPinToggle={() => detailHypothesis && handlePinToggle(detailHypothesis)}
        onExcludeToggle={() => detailHypothesis && openExclusionModal(detailHypothesis, detailHypothesis.is_excluded ? 'restore' : 'exclude')}
        onReevaluate={() => detailHypothesis && handleReevaluate(detailHypothesis)}
        reevaluating={detailHypothesis ? reevaluatingIds.has(detailHypothesis.hypothesis_id) : false}
      />

      {/* Side-by-side Comparison Modal */}
      <HypothesisComparisonModal
        open={comparisonOpen}
        hypotheses={selectedHypotheses}
        onClose={() => setComparisonOpen(false)}
      />

      {/* Propose Researcher Hypothesis Modal */}
      <ProposeHypothesisModal
        open={proposeOpen}
        runId={runId}
        busy={proposing}
        onSubmit={handleProposeSubmit}
        onCancel={() => {
          setProposeOpen(false);
          setProposeBackendError(null);
        }}
        backendError={proposeBackendError}
      />

      {/* Confirm Exclusion / Restoration Modal */}
      <ConfirmExclusionModal
        open={exclusionState.open}
        hypothesis={exclusionState.hypothesis}
        action={exclusionState.action}
        busy={exclusionState.busy}
        onConfirm={handleExclusionConfirm}
        onCancel={() => setExclusionState({ open: false, hypothesis: null, action: 'exclude', busy: false })}
      />
    </div>
  );
}
