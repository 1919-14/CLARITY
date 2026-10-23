export function EmptyState({ title, message, action = null, children }) {
  return (
    <div className="state-block state-block--empty">
      <p className="empty-mark" aria-hidden="true">❧</p>
      <h3 className="state-block__title">{title}</h3>
      {message ? <p className="state-block__message">{message}</p> : null}
      {children}
      {action ? <div className="state-block__action">{action}</div> : null}
    </div>
  );
}
