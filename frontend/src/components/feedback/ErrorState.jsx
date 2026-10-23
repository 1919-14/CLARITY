import { AlertIcon } from '../icons.jsx';

function guidanceFor(error) {
  if (!error) return [];
  if (error.kind === 'network') {
    return [
      'Start the Flask backend with `python backend/run.py` (or your configured command).',
      'Check that the API base URL in `frontend/.env` matches the port Flask is using.',
      'Confirm no firewall or proxy is blocking requests from the browser.',
    ];
  }
  if (error.kind === 'timeout') {
    return ['The backend did not answer in time. It may still be working; retry deliberately rather than in a loop.'];
  }
  if (error.kind === 'malformed') {
    return ['The backend responded with an unexpected body. Verify that the API base URL includes the `/api/v1` prefix.'];
  }
  if (error.status === 404) {
    return ['The requested record does not exist. It may have been removed or the identifier may be wrong.'];
  }
  if (error.status >= 500) {
    return ['This is a backend failure, not a scientific result. Retry after checking the Flask logs.'];
  }
  return [];
}

export function ErrorState({
  error,
  title = 'The records could not be retrieved',
  onRetry = null,
  retryLabel = 'Try again',
  compact = false,
}) {
  const guidance = guidanceFor(error);
  const fieldErrors = error?.fieldErrors ?? [];
  return (
    <div className={`state-block state-block--error${compact ? ' state-block--compact' : ''}`} role="alert">
      <p className="state-block__title">
        <AlertIcon size={18} />
        <span>{title}</span>
      </p>
      <p className="state-block__message">{error?.message || 'An unknown error occurred.'}</p>
      {error?.status ? (
        <p className="state-block__meta">
          HTTP {error.status}
          {error.code ? ` · ${error.code}` : ''}
        </p>
      ) : null}
      {fieldErrors.length > 0 ? (
        <ul className="state-block__list">
          {fieldErrors.map((entry, index) => (
            <li key={`${entry.field || 'field'}-${index}`}>
              {entry.field ? <strong>{entry.field}: </strong> : null}
              {entry.message}
            </li>
          ))}
        </ul>
      ) : null}
      {guidance.length > 0 ? (
        <ul className="state-block__list">
          {guidance.map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
      {onRetry ? (
        <button type="button" className="button button--secondary" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}
