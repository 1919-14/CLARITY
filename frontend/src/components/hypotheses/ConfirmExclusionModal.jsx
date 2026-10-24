import { useEffect, useId, useState } from 'react';
import { useModalFocusTrap } from '../../hooks/useModalFocusTrap.js';

export function ConfirmExclusionModal({
  open,
  hypothesis,
  action = 'exclude', // 'exclude' | 'restore'
  busy = false,
  onConfirm,
  onCancel,
}) {
  const titleId = useId();
  const descriptionId = useId();
  const [reason, setReason] = useState('');
  const containerRef = useModalFocusTrap({ open, onClose: onCancel, busy });

  useEffect(() => {
    if (open) {
      setReason('');
    }
  }, [open]);

  if (!open || !hypothesis) return null;

  const isExclude = action === 'exclude';
  const title = isExclude
    ? `Exclude hypothesis ${hypothesis.hypothesis_id}?`
    : `Restore hypothesis ${hypothesis.hypothesis_id}?`;

  const description = isExclude
    ? 'Excluding this hypothesis removes it from active search and selection without deleting its history, revisions, or evaluations. You can restore it to the active cohort at any time.'
    : 'Restoring this hypothesis returns it to the active candidate cohort for this run. Its previous revisions and evaluations remain intact.';

  const handleConfirm = () => {
    onConfirm({ reason: reason.trim() || undefined });
  };

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        ref={containerRef}
        tabIndex={-1}
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
      >
        <p className="volume-label">Lifecycle Action</p>
        <h2 className="dialog__title" id={titleId}>{title}</h2>
        <p className="dialog__body" id={descriptionId}>{description}</p>

        {isExclude && (
          <div className="field" style={{ marginTop: 'var(--space-4)' }}>
            <label className="field__label" htmlFor="exclusion-reason">
              Reason for exclusion <span className="muted">(optional)</span>
            </label>
            <input
              id="exclusion-reason"
              type="text"
              className="input"
              placeholder="e.g., Unphysical asymptotic behavior, duplicate formulation"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              disabled={busy}
            />
          </div>
        )}

        <div className="dialog__actions">
          <button
            type="button"
            className="button button--secondary"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className={`button button--${isExclude ? 'danger' : 'primary'}`}
            onClick={handleConfirm}
            disabled={busy}
            aria-busy={busy || undefined}
          >
            {busy ? 'Updating…' : isExclude ? 'Exclude candidate' : 'Restore candidate'}
          </button>
        </div>
      </div>
    </div>
  );
}
