import { statusLabel, statusTone } from '../../utils/formatting.js';

const GLYPHS = {
  neutral: '○',
  active: '◐',
  success: '✓',
  warning: '◑',
  danger: '✕',
};

export function StatusBadge({ status, label = null }) {
  const tone = statusTone(status);
  return (
    <span className={`status-badge status-badge--${tone}`} data-status={status || 'unknown'}>
      <span className="status-badge__glyph" aria-hidden="true">
        {GLYPHS[tone] || GLYPHS.neutral}
      </span>
      <span>{label ?? statusLabel(status)}</span>
    </span>
  );
}
