/**
 * Client-side validation for run configuration.
 *
 * Bounds mirror `backend/app/domain/budget_contracts.py` (Issue #14) and the
 * field names mirror `POST /api/v1/runs` (Issue #19). The backend validates
 * again; these checks exist to give fast, plain-language feedback and never
 * replace server-side enforcement.
 */

export const EXECUTION_MODES = [
  {
    value: 'local',
    label: 'Local',
    description: 'Sequential evaluation on the Flask host. No remote compute.',
  },
  {
    value: 'nodus_remote',
    label: 'Nodus remote',
    description: 'Parallel evaluation through the configured Nodus adapter.',
  },
  {
    value: 'mock',
    label: 'Mock (backend simulation)',
    description: 'Backend simulation mode for integration testing. Not real compute.',
  },
];

export const DEFAULT_RUN_FORM = Object.freeze({
  name: '',
  domain: 'default',
  executionMode: 'local',
  maxOracleQueries: 10,
  maxCandidateEvaluations: 100,
  maxConcurrentRemoteTasks: 4,
  maxRuntimeSeconds: 3600,
  maxComputeSpendUsd: '',
});

export const LIMITS = Object.freeze({
  maxOracleQueries: { min: 0, max: 100_000, integer: true, label: 'Oracle query budget' },
  maxCandidateEvaluations: { min: 0, max: 1_000_000, integer: true, label: 'Candidate evaluation limit' },
  maxConcurrentRemoteTasks: { min: 0, max: 32, integer: true, label: 'Maximum concurrent tasks' },
  maxRuntimeSeconds: { min: 0, max: 604_800, integer: true, label: 'Maximum run duration (seconds)' },
  maxComputeSpendUsd: { min: 0, max: 10_000, integer: false, label: 'Compute spending limit (USD)' },
});

const INTEGER_FIELDS = ['maxOracleQueries', 'maxCandidateEvaluations', 'maxConcurrentRemoteTasks', 'maxRuntimeSeconds'];

function parseNumber(field, rawValue, { integer, min, max, label }) {
  const text = String(rawValue ?? '').trim();
  if (text === '') {
    return { error: `${label} is required. Enter a whole number between ${min} and ${max}.` };
  }
  if (!/^-?\d+(\.\d+)?$/.test(text)) {
    return { error: `${label} must be a number, not “${text}”.` };
  }
  const value = Number(text);
  if (!Number.isFinite(value)) {
    return { error: `${label} must be a finite number.` };
  }
  if (integer && !Number.isInteger(value)) {
    return { error: `${label} must be a whole number (no decimals).` };
  }
  if (!integer && Math.round(value * 100) !== Number((value * 100).toFixed(6))) {
    return { error: `${label} may have at most two decimal places.` };
  }
  if (value < min) {
    return { error: `${label} cannot be negative. The minimum is ${min}.` };
  }
  if (value > max) {
    return { error: `${label} exceeds the backend maximum of ${max.toLocaleString('en-GB')}.` };
  }
  return { value };
}

/**
 * Validate the configuration form.
 *
 * @param {object} values raw form values (numbers may be strings)
 * @returns {{errors: Object<string,string>, warnings: string[], payload: ?object}}
 */
export function validateRunForm(values) {
  const errors = {};
  const warnings = [];

  const name = String(values.name ?? '').trim();
  if (!name) {
    errors.name = 'A run name is required so the run can be identified later.';
  }

  const domain = String(values.domain ?? '').trim();
  if (!domain) {
    errors.domain = 'Choose the black-box input domain for this run (for example “default”).';
  }

  const executionMode = String(values.executionMode ?? '').trim();
  if (!EXECUTION_MODES.some((mode) => mode.value === executionMode)) {
    errors.executionMode = 'Choose one of the execution modes supported by the backend.';
  }

  const numbers = {};
  for (const field of [...INTEGER_FIELDS, 'maxComputeSpendUsd']) {
    if (field === 'maxComputeSpendUsd') {
      const raw = String(values[field] ?? '').trim();
      if (raw === '') {
        // Blank means the application-side spending estimate gate is disabled.
        numbers[field] = null;
        continue;
      }
    }
    const result = parseNumber(field, values[field], LIMITS[field]);
    if (result.error) {
      errors[field] = result.error;
    } else {
      numbers[field] = result.value;
    }
  }

  if (Object.keys(errors).length > 0) {
    return { errors, warnings, payload: null };
  }

  if (
    numbers.maxRuntimeSeconds === 0 ||
    (numbers.maxOracleQueries === 0 && numbers.maxCandidateEvaluations === 0)
  ) {
    warnings.push(
      'This budget permits no scientific work: a zero runtime, or zero queries and evaluations, prevents the run from producing observations.',
    );
  }

  const payload = {
    name,
    domain,
    execution_mode: executionMode,
    budget: {
      max_oracle_queries: numbers.maxOracleQueries,
      max_candidate_evaluations: numbers.maxCandidateEvaluations,
      max_concurrent_remote_tasks: numbers.maxConcurrentRemoteTasks,
      max_runtime_seconds: numbers.maxRuntimeSeconds,
      max_compute_spend_usd: numbers.maxComputeSpendUsd,
    },
  };

  return { errors, warnings, payload };
}
