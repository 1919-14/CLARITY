import React, { useState, useEffect, useCallback, useRef } from 'react';
import { api, getApiBaseUrl, ApiError } from './api/client';
import { LoadingState, ErrorState, EmptyState, StatusBadge } from './components/CommonStates';
import { RunConfigModal } from './components/RunConfigModal';
import { RunDetailModal } from './components/RunDetailModal';
import { ScientificViewModal } from './components/ScientificViewModal';
import './App.css';

const POLLING_INTERVAL_MS = 3000;

function App() {
  const [runs, setRuns] = useState([]);
  const [loadingRuns, setLoadingRuns] = useState(true);
  const [runsError, setRunsError] = useState(null);

  // Modals & Navigation state
  const [isConfigModalOpen, setIsConfigModalOpen] = useState(false);
  const [selectedRun, setSelectedRun] = useState(null);
  const [scientificModal, setScientificModal] = useState(null); // { runId, viewType: 'hypotheses'|'report'|'benchmark' }

  // Health and backend status state
  const [backendHealth, setBackendHealth] = useState(null);
  const [_healthError, setHealthError] = useState(null);
  const [lastUpdated, setLastUpdated] = useState(null);

  // Polling tracker ref
  const pollTimerRef = useRef(null);

  /**
   * Fetch recent runs from the Flask API
   */
  const loadRuns = useCallback(async (isBackground = false) => {
    if (!isBackground) setLoadingRuns(true);
    setRunsError(null);

    try {
      const data = await api.getRuns();
      // Supports array directly or { runs: [...] }
      const runsList = Array.isArray(data) ? data : (data?.runs || []);
      setRuns(runsList);
      setLastUpdated(new Date().toLocaleTimeString());
      setLoadingRuns(false);

      // If a run is currently selected, update its reference with latest data
      setSelectedRun((prev) => {
        if (!prev) return null;
        const updated = runsList.find((r) => r.run_id === prev.run_id);
        return updated || prev;
      });
    } catch (err) {
      setLoadingRuns(false);
      setRunsError(err instanceof ApiError ? err.message : 'Unable to load discovery runs from backend.');
    }
  }, []);

  /**
   * Fetch backend service health
   */
  const checkHealth = useCallback(async () => {
    try {
      const health = await api.getHealth();
      setBackendHealth(health);
      setHealthError(null);
    } catch (err) {
      setBackendHealth(null);
      setHealthError(err instanceof ApiError ? err.message : 'Backend connection unavailable.');
    }
  }, []);

  /**
   * Initial load
   */
  useEffect(() => {
    checkHealth();
    loadRuns();
  }, [checkHealth, loadRuns]);

  /**
   * Dynamic bounded polling:
   * Only poll if at least one run is active ('running' or 'pending').
   * Stops automatically when all runs reach terminal states.
   */
  useEffect(() => {
    const hasActiveRuns = runs.some((r) => {
      const s = (r.status || '').toLowerCase();
      return s === 'running' || s === 'pending';
    });

    if (hasActiveRuns) {
      pollTimerRef.current = setInterval(() => {
        loadRuns(true);
      }, POLLING_INTERVAL_MS);
    } else {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
    }

    return () => {
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
      }
    };
  }, [runs, loadRuns]);

  /**
   * Handle successful run creation from modal
   */
  const handleRunCreated = (newRun) => {
    loadRuns();
    setSelectedRun(newRun);
  };

  /**
   * Handle run cancellation
   */
  const handleCancelRun = async (runId) => {
    await api.cancelRun(runId);
    await loadRuns(true);
  };

  // Compute high-level dashboard summaries
  const totalRuns = runs.length;
  const activeRunsCount = runs.filter((r) => ['running', 'pending'].includes((r.status || '').toLowerCase())).length;
  const completedRunsCount = runs.filter((r) => (r.status || '').toLowerCase() === 'completed').length;
  const budgetStoppedCount = runs.filter((r) => ['stopped_budget', 'budget_exhausted'].includes((r.status || '').toLowerCase())).length;

  return (
    <div className="clarity-app">
      {/* Top Navigation Bar */}
      <header className="navbar">
        <div className="navbar-brand">
          <span className="brand-logo" aria-hidden="true">⚛️</span>
          <div>
            <h1 className="brand-title">CLARITY</h1>
            <p className="brand-subtitle">Autonomous Black-Box Scientist Dashboard</p>
          </div>
        </div>

        <div className="navbar-controls">
          <div className="endpoint-indicator" title={`Backend URL: ${getApiBaseUrl()}`}>
            <span className={`status-dot ${backendHealth ? 'dot-online' : 'dot-offline'}`}></span>
            <span className="endpoint-text">
              {backendHealth ? 'Backend Connected' : 'Offline / Unreachable'}
            </span>
          </div>

          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => {
              checkHealth();
              loadRuns();
            }}
            title="Refresh runs from backend"
          >
            ↻ Refresh
          </button>

          <button
            type="button"
            className="btn btn-primary"
            onClick={() => setIsConfigModalOpen(true)}
          >
            + New Discovery Run
          </button>
        </div>
      </header>

      {/* Main Workspace Layout */}
      <main className="dashboard-layout">
        {/* Metric Cards Row */}
        <section className="overview-grid" aria-label="System Overview">
          <div className="stat-card">
            <span className="stat-label">Total Discovery Runs</span>
            <span className="stat-value">{totalRuns}</span>
            <span className="stat-sub">Persisted in storage</span>
          </div>

          <div className="stat-card">
            <span className="stat-label">Active Experimentation</span>
            <span className="stat-value font-active">{activeRunsCount}</span>
            <span className="stat-sub">{activeRunsCount > 0 ? 'Live polling active (3s)' : 'No runs in progress'}</span>
          </div>

          <div className="stat-card">
            <span className="stat-label">Completed Discoveries</span>
            <span className="stat-value font-success">{completedRunsCount}</span>
            <span className="stat-sub">Reached target stopping criteria</span>
          </div>

          <div className="stat-card">
            <span className="stat-label">Budget Limit Cutoffs</span>
            <span className="stat-value font-warning">{budgetStoppedCount}</span>
            <span className="stat-sub">Stopped by query/spend guardrails</span>
          </div>
        </section>

        {/* Runs Table Section */}
        <section className="runs-section">
          <div className="section-header">
            <div>
              <h2 className="section-title">Discovery Runs</h2>
              <p className="section-desc">
                Real-time experiment runs with verified resource limits and scientific evidence.
                {lastUpdated && <span className="last-sync"> Last synced: {lastUpdated}</span>}
              </p>
            </div>
            {runs.length > 0 && (
              <span className="count-pill">{runs.length} runs</span>
            )}
          </div>

          {loadingRuns && <LoadingState message="Connecting to Flask API and querying runs..." />}

          {runsError && (
            <ErrorState
              title="Unable to Retrieve Discovery Runs"
              error={runsError}
              onRetry={() => loadRuns()}
            />
          )}

          {!loadingRuns && !runsError && runs.length === 0 && (
            <EmptyState
              title="No Discovery Runs Found"
              message="No scientific experiments have been executed yet. Launch a new run to discover unknown system behavior."
              actionLabel="+ Configure First Discovery Run"
              onAction={() => setIsConfigModalOpen(true)}
            />
          )}

          {!loadingRuns && !runsError && runs.length > 0 && (
            <div className="table-responsive">
              <table className="runs-table">
                <thead>
                  <tr>
                    <th scope="col">Run Identifier</th>
                    <th scope="col">Status</th>
                    <th scope="col">Domain</th>
                    <th scope="col">Execution Platform</th>
                    <th scope="col">Oracle Queries</th>
                    <th scope="col">Evaluations</th>
                    <th scope="col">Settled Spend</th>
                    <th scope="col">Created At</th>
                    <th scope="col">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {runs.map((run) => {
                    const usage = run.usage || {};
                    const progress = run.progress || {};
                    const config = run.config || {};
                    const queriesUsed = progress.queries_executed ?? usage.queries_executed;
                    const maxQueries = config.max_oracle_queries ?? run.max_queries ?? '—';
                    const evalsDone = progress.evaluations_completed ?? usage.evaluations_completed;
                    const maxEvals = config.max_evaluation_tasks ?? run.max_evaluations ?? '—';

                    return (
                      <tr key={run.run_id} className="run-row">
                        <td className="cell-id">
                          <button
                            type="button"
                            className="link-btn font-mono"
                            onClick={() => setSelectedRun(run)}
                            title="Inspect run progress and metrics"
                          >
                            {run.run_id}
                          </button>
                          <div className="run-name-subtle">{run.name || 'Untitled Run'}</div>
                        </td>
                        <td>
                          <StatusBadge status={run.status} />
                        </td>
                        <td className="mono-text">{run.domain || config.oracle_domain || '1d_polynomial'}</td>
                        <td>
                          <span className="tag-pill">
                            {run.execution_mode === 'nodus' ? 'Nodus (Remote)' : 'Local'}
                          </span>
                        </td>
                        <td>
                          {queriesUsed != null ? (
                            <><span className="num-emphasis">{queriesUsed}</span> / {maxQueries}</>
                          ) : (
                            <span className="text-muted">Unavailable</span>
                          )}
                        </td>
                        <td>
                          {evalsDone != null ? (
                            <><span className="num-emphasis">{evalsDone}</span> / {maxEvals}</>
                          ) : (
                            <span className="text-muted">Unavailable</span>
                          )}
                        </td>
                        <td>
                          {usage.settled_cost_usd != null ? (
                            `$${Number(usage.settled_cost_usd).toFixed(4)}`
                          ) : (
                            <span className="text-muted" title="Unsettled or local run">Unavailable</span>
                          )}
                        </td>
                        <td className="mono-text text-sm">
                          {run.created_at ? new Date(run.created_at).toLocaleString() : '—'}
                        </td>
                        <td>
                          <div className="table-actions">
                            <button
                              type="button"
                              className="btn btn-outline btn-xs"
                              onClick={() => setSelectedRun(run)}
                            >
                              Inspect
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-xs"
                              onClick={() => setScientificModal({ runId: run.run_id, viewType: 'hypotheses' })}
                              title="Inspect competing hypotheses"
                            >
                              Hypotheses
                            </button>
                            <button
                              type="button"
                              className="btn btn-ghost btn-xs"
                              onClick={() => setScientificModal({ runId: run.run_id, viewType: 'benchmark' })}
                              title="Inspect ground-truth evaluator metrics"
                            >
                              Benchmark
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
        </section>
      </main>

      {/* Configuration Dialog */}
      <RunConfigModal
        isOpen={isConfigModalOpen}
        onClose={() => setIsConfigModalOpen(false)}
        onRunCreated={handleRunCreated}
      />

      {/* Run Detail Modal */}
      {selectedRun && (
        <RunDetailModal
          run={selectedRun}
          onClose={() => setSelectedRun(null)}
          onCancelRun={handleCancelRun}
          onNavigateHypotheses={(runId) => setScientificModal({ runId, viewType: 'hypotheses' })}
          onNavigateReport={(runId) => setScientificModal({ runId, viewType: 'report' })}
          onNavigateBenchmark={(runId) => setScientificModal({ runId, viewType: 'benchmark' })}
        />
      )}

      {/* Scientific Explorer Modal */}
      {scientificModal && (
        <ScientificViewModal
          runId={scientificModal.runId}
          viewType={scientificModal.viewType}
          onClose={() => setScientificModal(null)}
        />
      )}
    </div>
  );
}

export default App;
