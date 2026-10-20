import { useState, useEffect } from 'react';
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

        {/* Foundation & Architecture Card */}
        <section className="glass-card" aria-labelledby="foundation-card-title">
          <div className="card-header">
            <h2 id="foundation-card-title" className="card-title">
              System Architecture & Status
            </h2>
            <span className="status-pill healthy">Issues #3–#4 Baseline</span>
          </div>

          <div className="section-subtitle implemented">
            <span>&bull;</span> Implemented and Verified
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
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-emerald)' }}>✓</span>
              <div>
                <strong>Scientific Data Contracts:</strong> Immutable experiment, observation, hypothesis, evaluation, and benchmark schemas with structured errors, a held-out/training leakage invariant, and a ground-truth denylist guard.
                <span className="badge-tag implemented">Active</span>
              </div>
            </div>
            <div className="info-item">
              <span className="info-icon" style={{ color: 'var(--accent-emerald)' }}>✓</span>
              <div>
                <strong>Black-Box Oracle &amp; Isolated Evaluator:</strong> Deterministic local simulator with hidden coefficients and structured validation errors, plus a separate benchmark evaluator for Hit@K and MRR scoring.
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
