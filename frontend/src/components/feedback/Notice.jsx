const TONES = {
  info: 'note',
  success: 'note note--success',
  warning: 'note note--warning',
  danger: 'note note--danger',
};

const GLYPHS = {
  info: 'ℹ',
  success: '✓',
  warning: '!',
  danger: '✕',
};

export function Notice({ tone = 'info', title, children, actions = null }) {
  const isUrgent = tone === 'danger';
  return (
    <div
      className={TONES[tone] || TONES.info}
      role={isUrgent ? 'alert' : 'status'}
      aria-live={isUrgent ? 'assertive' : 'polite'}
    >
      <p className="note__title">
        <span className="note__glyph" aria-hidden="true">{GLYPHS[tone] || GLYPHS.info}</span>
        {title}
      </p>
      {children ? <div className="note__body">{children}</div> : null}
      {actions ? <div className="note__actions">{actions}</div> : null}
    </div>
  );
}
