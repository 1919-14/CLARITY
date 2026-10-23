import { formatDateTime, statusLabel } from '../../utils/formatting.js';

export function RunPicker({ runs, value, onChange, isLoading = false, label = 'Run', id = 'run-picker' }) {
  return (
    <div className="field field--inline">
      <label className="field__label" htmlFor={id}>{label}</label>
      <select
        id={id}
        className="input"
        value={value || ''}
        onChange={(event) => onChange(event.target.value)}
        disabled={isLoading || !runs || runs.length === 0}
      >
        {isLoading ? <option value="">Loading runs…</option> : null}
        {!isLoading && (!runs || runs.length === 0) ? (
          <option value="">No persisted runs available</option>
        ) : null}
        {(runs || []).map((run) => (
          <option key={run.run_id} value={run.run_id}>
            {run.name} — {run.run_id} — {statusLabel(run.status)} — {formatDateTime(run.created_at)}
          </option>
        ))}
      </select>
    </div>
  );
}
