import React, { useState } from 'react';
import { api, ApiError } from '../api/client';

const DOMAIN_OPTIONS = [
  { id: '1d_polynomial', label: '1D Bounded Polynomial (Scalar x -> y)' },
];

const EXECUTION_MODES = [
  { id: 'local', label: 'Local Deterministic Runner' },
  { id: 'nodus', label: 'Nodus Compute (Remote Parallel)' },
];

export function RunConfigModal({ isOpen, onClose, onRunCreated }) {
  const [name, setName] = useState('Polynomial Discovery Test');
  const [domain, setDomain] = useState('1d_polynomial');
  const [executionMode, setExecutionMode] = useState('local');
  const [maxQueries, setMaxQueries] = useState(15);
  const [maxEvaluations, setMaxEvaluations] = useState(50);
  const [maxConcurrency, setMaxConcurrency] = useState(4);
  const [maxRuntimeSeconds, setMaxRuntimeSeconds] = useState(300);
  const [spendCeiling, setSpendCeiling] = useState('');

  const [validationErrors, setValidationErrors] = useState({});
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submissionError, setSubmissionError] = useState(null);

  if (!isOpen) return null;

  const validate = () => {
    const errors = {};

    if (!name.trim()) {
      errors.name = 'Run name or description is required.';
    }

    const q = parseInt(maxQueries, 10);
    if (isNaN(q) || q < 1 || q > 500) {
      errors.maxQueries = 'Query budget must be an integer between 1 and 500.';
    }

    const e = parseInt(maxEvaluations, 10);
    if (isNaN(e) || e < 1 || e > 2000) {
      errors.maxEvaluations = 'Evaluation limit must be an integer between 1 and 2000.';
    }

    const c = parseInt(maxConcurrency, 10);
    if (isNaN(c) || c < 1 || c > 32) {
      errors.maxConcurrency = 'Concurrency must be between 1 and 32 tasks.';
    }

    const r = parseInt(maxRuntimeSeconds, 10);
    if (isNaN(r) || r < 10 || r > 3600) {
      errors.maxRuntimeSeconds = 'Run duration limit must be between 10 and 3600 seconds.';
    }

    if (spendCeiling !== '' && spendCeiling !== null) {
      const s = parseFloat(spendCeiling);
      if (isNaN(s) || s < 0) {
        errors.spendCeiling = 'Spend limit must be a positive number if specified.';
      }
    }

    setValidationErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmissionError(null);

    if (!validate()) {
      return;
    }

    setIsSubmitting(true);

    const payload = {
      name: name.trim(),
      domain: domain,
      execution_mode: executionMode,
      max_oracle_queries: parseInt(maxQueries, 10),
      max_evaluation_tasks: parseInt(maxEvaluations, 10),
      max_concurrency: parseInt(maxConcurrency, 10),
      max_runtime_seconds: parseInt(maxRuntimeSeconds, 10),
      spend_ceiling_usd: spendCeiling ? parseFloat(spendCeiling) : null,
    };

    try {
      const createdRun = await api.createRun(payload);
      setIsSubmitting(false);
      onRunCreated(createdRun);
      onClose();
    } catch (err) {
      setIsSubmitting(false);
      if (err instanceof ApiError) {
        setSubmissionError(err.message);
      } else {
        setSubmissionError('Failed to submit run configuration to backend.');
      }
    }
  };

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div className="modal-container">
        <header className="modal-header">
          <h3 id="modal-title" className="modal-title">Configure New Discovery Run</h3>
          <button
            type="button"
            className="btn-icon"
            onClick={onClose}
            disabled={isSubmitting}
            aria-label="Close dialog"
          >
            ✕
          </button>
        </header>

        <form onSubmit={handleSubmit} className="modal-form" noValidate>
          {submissionError && (
            <div className="alert alert-danger" role="alert">
              <strong>Submission Error:</strong> {submissionError}
            </div>
          )}

          <div className="form-group">
            <label htmlFor="run-name">Run Name / Description <span className="req">*</span></label>
            <input
              id="run-name"
              type="text"
              className={`input-field ${validationErrors.name ? 'is-invalid' : ''}`}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={isSubmitting}
              placeholder="e.g. Bounded Polynomial Discovery Run"
              required
            />
            {validationErrors.name && <p className="field-error">{validationErrors.name}</p>}
          </div>

          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="run-domain">Black-Box Domain</label>
              <select
                id="run-domain"
                className="input-field"
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                disabled={isSubmitting}
              >
                {DOMAIN_OPTIONS.map((d) => (
                  <option key={d.id} value={d.id}>{d.label}</option>
                ))}
              </select>
            </div>

            <div className="form-group">
              <label htmlFor="run-exec-mode">Execution Platform</label>
              <select
                id="run-exec-mode"
                className="input-field"
                value={executionMode}
                onChange={(e) => setExecutionMode(e.target.value)}
                disabled={isSubmitting}
              >
                {EXECUTION_MODES.map((m) => (
                  <option key={m.id} value={m.id}>{m.label}</option>
                ))}
              </select>
            </div>
          </div>

          <div className="form-section-title">Enforced Application Budgets & Limits</div>
          <div className="form-grid">
            <div className="form-group">
              <label htmlFor="run-queries">Oracle Query Budget <span className="req">*</span></label>
              <input
                id="run-queries"
                type="number"
                min="1"
                max="500"
                className={`input-field ${validationErrors.maxQueries ? 'is-invalid' : ''}`}
                value={maxQueries}
                onChange={(e) => setMaxQueries(e.target.value)}
                disabled={isSubmitting}
                required
              />
              <span className="field-hint">Max queries submitted to the black-box oracle.</span>
              {validationErrors.maxQueries && <p className="field-error">{validationErrors.maxQueries}</p>}
            </div>

            <div className="form-group">
              <label htmlFor="run-evals">Evaluation Tasks Limit <span className="req">*</span></label>
              <input
                id="run-evals"
                type="number"
                min="1"
                max="2000"
                className={`input-field ${validationErrors.maxEvaluations ? 'is-invalid' : ''}`}
                value={maxEvaluations}
                onChange={(e) => setMaxEvaluations(e.target.value)}
                disabled={isSubmitting}
                required
              />
              <span className="field-hint">Upper ceiling of candidate evaluations.</span>
              {validationErrors.maxEvaluations && <p className="field-error">{validationErrors.maxEvaluations}</p>}
            </div>

            <div className="form-group">
              <label htmlFor="run-concurrency">Max Concurrent Tasks</label>
              <input
                id="run-concurrency"
                type="number"
                min="1"
                max="32"
                className={`input-field ${validationErrors.maxConcurrency ? 'is-invalid' : ''}`}
                value={maxConcurrency}
                onChange={(e) => setMaxConcurrency(e.target.value)}
                disabled={isSubmitting}
              />
              <span className="field-hint">Simultaneous worker/task allocations.</span>
              {validationErrors.maxConcurrency && <p className="field-error">{validationErrors.maxConcurrency}</p>}
            </div>

            <div className="form-group">
              <label htmlFor="run-runtime">Duration Limit (Seconds)</label>
              <input
                id="run-runtime"
                type="number"
                min="10"
                max="3600"
                className={`input-field ${validationErrors.maxRuntimeSeconds ? 'is-invalid' : ''}`}
                value={maxRuntimeSeconds}
                onChange={(e) => setMaxRuntimeSeconds(e.target.value)}
                disabled={isSubmitting}
              />
              <span className="field-hint">Automatic cutoff timer for execution.</span>
              {validationErrors.maxRuntimeSeconds && <p className="field-error">{validationErrors.maxRuntimeSeconds}</p>}
            </div>
          </div>

          <div className="form-group">
            <label htmlFor="run-spend">Provider Spending Limit (USD, Optional)</label>
            <input
              id="run-spend"
              type="number"
              step="0.01"
              min="0"
              className={`input-field ${validationErrors.spendCeiling ? 'is-invalid' : ''}`}
              value={spendCeiling}
              onChange={(e) => setSpendCeiling(e.target.value)}
              disabled={isSubmitting}
              placeholder="e.g. 5.00 (leave blank for unconstrained)"
            />
            <span className="field-hint">
              Enforced only when supported by the remote compute provider. Distinct from provider account balance.
            </span>
            {validationErrors.spendCeiling && <p className="field-error">{validationErrors.spendCeiling}</p>}
          </div>

          <footer className="modal-footer">
            <button
              type="button"
              className="btn btn-secondary"
              onClick={onClose}
              disabled={isSubmitting}
            >
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={isSubmitting}
            >
              {isSubmitting ? 'Submitting Run...' : 'Launch Discovery Run'}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
