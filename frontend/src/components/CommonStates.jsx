import React from 'react';

/**
 * Standard loading indicator component.
 */
export function LoadingState({ message = 'Loading discovery data...' }) {
  return (
    <div className="state-container loading-state" role="status" aria-live="polite">
      <div className="spinner" aria-hidden="true"></div>
      <p className="state-message">{message}</p>
    </div>
  );
}

/**
 * Standard error display component with retry action.
 */
export function ErrorState({ title = 'Failed to load data', error, onRetry }) {
  const errorMessage = error?.message || (typeof error === 'string' ? error : 'An unexpected error occurred.');

  return (
    <div className="state-container error-state" role="alert">
      <div className="state-icon error-icon" aria-hidden="true">⚠️</div>
      <div className="state-body">
        <h4 className="state-title">{title}</h4>
        <p className="state-message">{errorMessage}</p>
        {error?.details && (
          <pre className="state-details">
            {typeof error.details === 'object' ? JSON.stringify(error.details, null, 2) : error.details}
          </pre>
        )}
      </div>
      {onRetry && (
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/**
 * Standard empty state component.
 */
export function EmptyState({ title = 'No records found', message, actionLabel, onAction }) {
  return (
    <div className="state-container empty-state">
      <div className="state-icon empty-icon" aria-hidden="true">🔬</div>
      <div className="state-body">
        <h4 className="state-title">{title}</h4>
        {message && <p className="state-message">{message}</p>}
      </div>
      {actionLabel && onAction && (
        <button type="button" className="btn btn-primary" onClick={onAction}>
          {actionLabel}
        </button>
      )}
    </div>
  );
}

/**
 * Lifecycle and status badge component.
 */
export function StatusBadge({ status }) {
  const norm = (status || 'unknown').toLowerCase();
  let badgeClass = 'badge-neutral';
  let label = status || 'Unknown';

  switch (norm) {
    case 'running':
    case 'active':
      badgeClass = 'badge-active';
      label = 'Running';
      break;
    case 'completed':
    case 'success':
      badgeClass = 'badge-success';
      label = 'Completed';
      break;
    case 'stopped_budget':
    case 'budget_exhausted':
      badgeClass = 'badge-warning';
      label = 'Budget Limit Reached';
      break;
    case 'failed':
    case 'error':
      badgeClass = 'badge-danger';
      label = 'Failed';
      break;
    case 'cancelled':
      badgeClass = 'badge-muted';
      label = 'Cancelled';
      break;
    case 'pending':
    case 'initialized':
      badgeClass = 'badge-info';
      label = 'Pending';
      break;
    default:
      badgeClass = 'badge-neutral';
      label = status;
  }

  return (
    <span className={`status-badge ${badgeClass}`} title={`Run status: ${label}`}>
      <span className="badge-dot" aria-hidden="true"></span>
      {label}
    </span>
  );
}
