export function LoadingState({ label = 'Retrieving records from the backend…', compact = false }) {
  return (
    <div className={`state-block state-block--loading${compact ? ' state-block--compact' : ''}`} role="status">
      <span className="quill-loader" aria-hidden="true" />
      <p className="state-block__message">{label}</p>
    </div>
  );
}
