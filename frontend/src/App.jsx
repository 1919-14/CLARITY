import { useState, useEffect } from 'react';
import { formatCost, formatSeconds, formatLimit, loadBudget } from './budgetUtils';
import './App.css';

function App() {
  const defaultApiUrl = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:5000';
  const [apiUrl, setApiUrl] = useState(defaultApiUrl);
  const [activeUrl, setActiveUrl] = useState(defaultApiUrl);
  const [checkTrigger, setCheckTrigger] = useState(0);
  const [status, setStatus] = useState('loading'); // 'loading' | 'healthy' | 'error'
  const [data, setData] = useState(null);
  const [errorDetails, setErrorDetails] = useState(null);
  const [latency, setLatency] = useState(null);

  // Issue #14 — per-run budget status (limits vs. actual usage, cost states).
  const [runId, setRunId] = useState('');
  const [budgetState, setBudgetState] = useState('idle'); // 'idle' | 'loading' | 'loaded' | 'error'
  const [budget, setBudget] = useState(null);
  const [budgetError, setBudgetError] = useState(null);

  useEffect(() => {
    let isCurrent = true;

    async function executeHealthCheck() {
      const startTime = performance.now();
      const normalizedUrl = activeUrl.replace(/\/+$/, '');
      const endpoint = `${normalizedUrl}/api/health`;

      try {
        const response = await fetch(endpoint, {
          method: 'GET',
          headers: {
            'Accept': 'application/json',
          },
        });

        const elapsed = Math.round(performance.now() - startTime);

        if (!response.ok) {
          throw new Error(`HTTP Error ${response.status}: ${response.statusText}`);
        }

        const json = await response.json();
        if (isCurrent) {
          setLatency(elapsed);
          if (json.status === 'healthy') {
            setData(json);
            setStatus('healthy');
          } else {
            throw new Error(`Unexpected backend status response: ${JSON.stringify(json)}`);
          }
        }
      } catch (err) {
        if (isCurrent) {
          const elapsed = Math.round(performance.now() - startTime);
          setLatency(elapsed);
          setStatus('error');
          setErrorDetails(
            err instanceof Error ? err.message : 'Unknown network failure while connecting to backend.'
          );
        }
      }
    }

    executeHealthCheck();

    return () => {
      isCurrent = false;
    };
  }, [activeUrl, checkTrigger]);

  const handleSubmitUrl = (e) => {
    e.preventDefault();
    const trimmed = apiUrl.trim();
    if (trimmed) {
      setStatus('loading');
      setErrorDetails(null);
      setData(null);
      if (trimmed === activeUrl) {
        setCheckTrigger((prev) => prev + 1);
      } else {
        setActiveUrl(trimmed);
      }
    }
  };

  const handleRetry = () => {
    setStatus('loading');
    setErrorDetails(null);
    setData(null);
    setCheckTrigger((prev) => prev + 1);
  };

  const handleLoadBudget = async (e) => {
    e.preventDefault();
    const requestedRunId = runId.trim();
    if (!requestedRunId) return;

    setBudgetState('loading');
    setBudgetError(null);
    setBudget(null);

    const result = await loadBudget(activeUrl, requestedRunId);
    if (result.state === 'loaded') {
      setBudget(result.budget);
      setBudgetState('loaded');
    } else {
      setBudgetState('error');
      setBudgetError(result.error);
    }
  };

  return (
    <div className="app-container">
      <header className="app-header">
        <div className="brand-badge">
          <span className="pulse-dot"></span>
          Hackyard 2026 &bull; Nodus Compute Challenge
        </div>
        <h1 className="app-title">CLARITY</h1>
        <p className="app-subtitle">
          Autonomous Black-Box Scientist — discovering unknown system behavior through active
          experimentation, competing hypotheses, and parallel remote evaluation.
        </p>
      </header>

      <main className="main-grid">
        {/* Backend Connectivity Status Card */}
        <section className="glass-card" aria-labelledby="status-card-title">
          <div className="card-header">
            <h2 id="status-card-title" className="card-title">
              Backend Health
            </h2>
            <div className={`status-pill ${status}`}>
              <span className="dot-indicator"></span>
              {status === 'loading' && 'Connecting...'}
              {status === 'healthy' && 'Online & Healthy'}
              {status === 'error' && 'Disconnected'}
            </div>
          </div>

          <form onSubmit={handleSubmitUrl} className="api-config-row">
            <label htmlFor="api-base-url-input">API Base URL (Configurable)</label>
            <div className="api-input-group">
              <input
                id="api-base-url-input"
                type="text"
                className="api-input"
                value={apiUrl}
                onChange={(e) => setApiUrl(e.target.value)}
                placeholder="http://127.0.0.1:5000"
              />
              <button
                type="submit"
                className="btn-primary"
                disabled={status === 'loading'}
              >
                {status === 'loading' ? 'Checking...' : 'Check'}
              </button>
            </div>
          </form>

          {status === 'healthy' && data && (
            <div className="result-box">
              <div className="meta-grid">
                <div className="meta-item">
                  <span className="meta-label">Service</span>
                  <span className="meta-value">{data.service}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Environment</span>
                  <span className="meta-value">{data.environment}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Response Time</span>
                  <span className="meta-value">{latency !== null ? `${latency} ms` : 'N/A'}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Status</span>
                  <span className="meta-value" style={{ color: 'var(--accent-emerald)' }}>
                    {data.status.toUpperCase()}
                  </span>
                </div>
              </div>

              <div className="meta-item" style={{ marginBottom: '0.75rem' }}>
                <span className="meta-label">Timestamp (UTC)</span>
                <span className="meta-value" style={{ fontSize: '0.8rem' }}>
                  {data.timestamp}
                </span>
              </div>

              <div className="meta-item">
                <span className="meta-label">Raw Response Payload</span>
                <pre className="json-preview">{JSON.stringify(data, null, 2)}</pre>
              </div>
            </div>
          )}

          {status === 'error' && (
            <div className="error-banner">
              <div className="error-title">Connection Check Failed</div>
              <div className="error-desc">{errorDetails}</div>
              <div style={{ marginTop: '0.75rem', fontSize: '0.75rem', color: 'var(--text-muted)' }}>
                <strong>Troubleshooting tips:</strong>
                <ul style={{ paddingLeft: '1.25rem', marginTop: '0.25rem' }}>
                  <li>Ensure the Flask backend is started (`python backend/run.py`).</li>
                  <li>Verify the backend is listening on the host and port specified above.</li>
                  <li>Check browser console for potential CORS or network restrictions.</li>
                </ul>
              </div>
              <button
                type="button"
                className="btn-primary"
                style={{ marginTop: '0.85rem' }}
                onClick={handleRetry}
              >
                Retry Connection
              </button>
            </div>
          )}
        </section>

        {/* Per-run Budget Card (Issue #14) */}
        <section className="glass-card" aria-labelledby="budget-card-title">
          <div className="card-header">
            <h2 id="budget-card-title" className="card-title">
              Run Budget
            </h2>
            {budgetState === 'loaded' && budget && (
              <div className={`status-pill ${budget.is_exhausted ? 'error' : 'healthy'}`}>
                <span className="dot-indicator"></span>
                {budget.is_exhausted ? budget.stop_reason || 'Exhausted' : 'Within budget'}
              </div>
            )}
          </div>

          <div className="section-subtitle implemented">
            <span>&bull;</span> Application-enforced limits. Unavailable cost data is shown as
            &quot;Unavailable&quot;, never as zero.
          </div>

          <form onSubmit={handleLoadBudget} className="api-config-row">
            <label htmlFor="run-id-input">Run ID</label>
            <div className="api-input-group">
              <input
                id="run-id-input"
                type="text"
                className="api-input"
                value={runId}
                onChange={(e) => setRunId(e.target.value)}
                placeholder="run-abc123"
              />
              <button
                type="submit"
                className="btn-primary"
                disabled={budgetState === 'loading' || !runId.trim()}
              >
                {budgetState === 'loading' ? 'Loading...' : 'Load budget'}
              </button>
            </div>
          </form>

          {budgetState === 'error' && (
            <div className="error-banner" role="alert">
              <div className="error-title">Budget unavailable</div>
              <div className="error-desc">{budgetError}</div>
            </div>
          )}

          {budgetState === 'loaded' && budget && (
            <div className="result-box">
              {budget.is_exhausted && (
                <div className="error-banner" role="status" style={{ marginBottom: '0.9rem' }}>
                  <div className="error-title">Stopped: {budget.stop_reason}</div>
                  <div className="error-desc">
                    Resource exhaustion is not a scientific discovery. Completed observations and
                    evaluations are preserved.
                  </div>
                </div>
              )}

              <div className="meta-grid">
                <div className="meta-item">
                  <span className="meta-label">Oracle queries used / limit</span>
                  <span className="meta-value">{formatLimit(budget.usage.oracle_queries)}</span>
                  <span className="meta-label">
                    remaining {budget.usage.oracle_queries.remaining ?? 'n/a'}
                  </span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Evaluations used / limit</span>
                  <span className="meta-value">{formatLimit(budget.usage.candidate_evaluations)}</span>
                  <span className="meta-label">
                    remaining {budget.usage.candidate_evaluations.remaining ?? 'n/a'}
                  </span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Hypotheses generated / limit</span>
                  <span className="meta-value">{formatLimit(budget.usage.generated_hypotheses)}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Remote concurrency</span>
                  <span className="meta-value">
                    {budget.usage.active_remote_tasks} / {budget.usage.max_concurrent_remote_tasks}
                  </span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Elapsed</span>
                  <span className="meta-value">{formatSeconds(budget.runtime.elapsed_seconds)}</span>
                  <span className="meta-label">
                    remaining {formatSeconds(budget.runtime.remaining_seconds)}
                  </span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Estimated cost</span>
                  <span className="meta-value">{formatCost(budget.cost.estimated_cost_usd)}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Accruing cost</span>
                  <span className="meta-value">{formatCost(budget.cost.accruing_cost_usd)}</span>
                </div>
                <div className="meta-item">
                  <span className="meta-label">Settled cost</span>
                  <span className="meta-value">{formatCost(budget.cost.settled_cost_usd)}</span>
                  <span className="meta-label">state {budget.cost.data_state}</span>
                </div>
              </div>

              <div className="meta-item" style={{ marginTop: '0.75rem' }}>
                <span className="meta-label">Cost enforcement</span>
                <span className="meta-value" style={{ fontSize: '0.8rem' }}>
                  {budget.cost.enforcement} · provider hard cap supported:{' '}
                  {budget.cost.provider_hard_cap_supported ? 'yes' : 'no'}
                </span>
              </div>

              {budget.limitations && budget.limitations.length > 0 && (
                <div className="meta-item" style={{ marginTop: '0.75rem' }}>
                  <span className="meta-label">Limitations</span>
                  <ul style={{ paddingLeft: '1.25rem', marginTop: '0.25rem', fontSize: '0.8rem' }}>
                    {budget.limitations.map((limitation) => (
                      <li key={limitation}>{limitation}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </section>

        {/* Foundation & Architecture Card */}
        <section className="glass-card" aria-labelledby="foundation-card-title">
          <div className="card-header">
            <h2 id="foundation-card-title" className="card-title">
              System Architecture & Status
            </h2>
            <span className="status-pill healthy">Issue #3 Baseline</span>
          </div>

          <div className="section-subtitle implemented">
            <span>&bull;</span> Implemented in Foundation Skeleton
          </div>
          <div className="info-list">
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-emerald)' }}>✓</span>
              <div>
                <strong>Flask Application Factory:</strong> Modular backend service with test/dev environments, strict production key enforcement, and predictable JSON health API.
                <span className="badge-tag implemented">Active</span>
              </div>
            </div>
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-emerald)' }}>✓</span>
              <div>
                <strong>Research Dashboard Skeleton:</strong> Configurable client with real-time connectivity monitoring, latency measurement, and error diagnostics.
                <span className="badge-tag implemented">Active</span>
              </div>
            </div>
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-emerald)' }}>✓</span>
              <div>
                <strong>Automated CI Verification:</strong> GitHub Actions pipeline enforcing syntax validity, pytest suite, frontend linting, and production builds.
                <span className="badge-tag implemented">Active</span>
              </div>
            </div>
          </div>

          <div className="section-subtitle planned">
            <span>&bull;</span> Target Capabilities (Upcoming Issues)
          </div>
          <div className="info-list">
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-amber)' }}>⏳</span>
              <div>
                <strong>Trust Isolation:</strong> Complete structural separation between Oracle (black box), Discovery Engine, and Ground-Truth Evaluator.
                <span className="badge-tag planned">Planned</span>
              </div>
            </div>
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-amber)' }}>⏳</span>
              <div>
                <strong>Nodus Remote Compute:</strong> Adapter-bounded remote task submission and parallel evaluation of candidate hypotheses.
                <span className="badge-tag planned">Planned</span>
              </div>
            </div>
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-amber)' }}>⏳</span>
              <div>
                <strong>Active Discovery & Limits:</strong> Uncertainty-driven experiment planning with strict query and spend limit enforcement.
                <span className="badge-tag planned">Planned</span>
              </div>
            </div>
          </div>
        </section>
      </main>

      <footer className="app-footer">
        <div>CLARITY Research System &bull; Team 1919-14</div>
        <div>Flask 3 + React 19 + MySQL + Nodus</div>
      </footer>
    </div>
  );
}

export default App;
