/**
 * Presentation helpers.
 *
 * The central rule: `null`/`undefined` mean "the backend has no measurement for
 * this" and must never be formatted as zero. Only a real numeric zero prints as
 * zero.
 */

export const UNAVAILABLE = 'Unavailable';

const dateTimeFormatter = new Intl.DateTimeFormat('en-GB', {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
  timeZone: 'UTC',
  hourCycle: 'h23',
});

const timeFormatter = new Intl.DateTimeFormat('en-GB', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  timeZone: 'UTC',
  hourCycle: 'h23',
});

export function isUnavailable(value) {
  return value === null || value === undefined || value === '';
}

export function formatDateTime(isoString) {
  if (isUnavailable(isoString)) return UNAVAILABLE;
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return UNAVAILABLE;
  return `${dateTimeFormatter.format(date)} UTC`;
}

export function formatTime(isoString) {
  if (isUnavailable(isoString)) return UNAVAILABLE;
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return UNAVAILABLE;
  return `${timeFormatter.format(date)} UTC`;
}

export function formatInteger(value) {
  if (isUnavailable(value) || typeof value !== 'number' || Number.isNaN(value)) {
    return UNAVAILABLE;
  }
  return new Intl.NumberFormat('en-GB').format(value);
}

export function formatDecimal(value, digits = 4) {
  if (isUnavailable(value) || typeof value !== 'number' || Number.isNaN(value)) {
    return UNAVAILABLE;
  }
  return Number(value).toFixed(digits);
}

export function formatCostUsd(value) {
  if (isUnavailable(value) || typeof value !== 'number' || Number.isNaN(value)) {
    return UNAVAILABLE;
  }
  return `$${Number(value).toFixed(4)}`;
}

export function formatSeconds(value) {
  if (isUnavailable(value) || typeof value !== 'number' || Number.isNaN(value)) {
    return UNAVAILABLE;
  }
  const total = Math.max(0, Math.round(value));
  if (total < 60) return `${total} s`;
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  if (minutes < 60) return `${minutes} m ${String(seconds).padStart(2, '0')} s`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours < 24) return `${hours} h ${String(remainingMinutes).padStart(2, '0')} m`;
  const days = Math.floor(hours / 24);
  return `${days} d ${String(hours % 24).padStart(2, '0')} h`;
}

export function formatLimit(entry) {
  if (!entry || isUnavailable(entry.used)) return UNAVAILABLE;
  if (isUnavailable(entry.limit)) return `${formatInteger(entry.used)} · no limit`;
  return `${formatInteger(entry.used)} / ${formatInteger(entry.limit)}`;
}

export function formatRemaining(entry) {
  if (!entry) return UNAVAILABLE;
  if (entry.disabled || isUnavailable(entry.limit)) return 'No limit';
  return formatInteger(entry.remaining);
}

export function shortId(value, { head = 12, tail = 6 } = {}) {
  if (isUnavailable(value)) return UNAVAILABLE;
  const text = String(value);
  if (text.length <= head + tail + 1) return text;
  return `${text.slice(0, head)}…${text.slice(-tail)}`;
}

export function titleize(value) {
  if (isUnavailable(value)) return UNAVAILABLE;
  return String(value)
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

const STATUS_LABELS = {
  pending: 'Pending',
  queued: 'Queued',
  running: 'Running',
  cancelling: 'Cancelling',
  completed: 'Completed',
  cancelled: 'Cancelled',
  failed: 'Failed',
  budget_exhausted: 'Budget exhausted',
  stopped_by_budget: 'Stopped by budget',
};

const STATUS_TONES = {
  pending: 'neutral',
  queued: 'neutral',
  running: 'active',
  cancelling: 'warning',
  completed: 'success',
  cancelled: 'warning',
  failed: 'danger',
  budget_exhausted: 'warning',
  stopped_by_budget: 'warning',
};

export function statusLabel(status) {
  if (isUnavailable(status)) return UNAVAILABLE;
  return STATUS_LABELS[status] || titleize(status);
}

export function statusTone(status) {
  return STATUS_TONES[status] || 'neutral';
}

const MODE_LABELS = {
  local: 'Local',
  local_sequential: 'Local (sequential)',
  nodus_remote: 'Nodus remote',
  nodus_parallel: 'Nodus parallel',
  mock: 'Mock (backend simulation)',
};

export function executionModeLabel(mode) {
  if (isUnavailable(mode)) return UNAVAILABLE;
  return MODE_LABELS[mode] || titleize(mode);
}

const PHASE_LABELS = {
  created: 'Created',
  initializing: 'Initialising',
  candidate_generation: 'Candidate generation',
  candidate_evaluation: 'Candidate evaluation',
  experiment_selection: 'Experiment selection',
  oracle_query: 'Oracle query',
  observation_persistence: 'Observation persistence',
  report_generation: 'Report generation',
  completed: 'Completed',
  terminated: 'Terminated',
};

export function phaseLabel(phase) {
  if (isUnavailable(phase)) return UNAVAILABLE;
  return PHASE_LABELS[phase] || titleize(phase);
}

export function pluralize(count, singular, plural = `${singular}s`) {
  if (isUnavailable(count)) return UNAVAILABLE;
  return `${formatInteger(count)} ${count === 1 ? singular : plural}`;
}
