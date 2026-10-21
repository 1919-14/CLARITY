/**
 * Pure validation logic for CLARITY discovery run configuration.
 * Extracted to ensure comprehensive testability without DOM rendering dependencies.
 */

export function validateRunConfig({
  name,
  maxQueries,
  maxEvaluations,
  maxConcurrency,
  maxRuntimeSeconds,
  spendCeiling,
}) {
  const errors = {};

  if (!name || typeof name !== 'string' || !name.trim()) {
    errors.name = 'Run name or description is required.';
  }

  // Strict integer validation helper: rejects floats like "1.5", scientific notation, strings with spaces, NaN
  const isStrictIntegerString = (val) => {
    if (typeof val === 'number') return Number.isInteger(val);
    if (typeof val !== 'string') return false;
    const trimmed = val.trim();
    return /^-?\d+$/.test(trimmed);
  };

  // 1. Oracle Query Budget
  if (!isStrictIntegerString(maxQueries)) {
    errors.maxQueries = 'Query budget must be a whole integer between 1 and 500.';
  } else {
    const q = Number(maxQueries);
    if (q < 1 || q > 500) {
      errors.maxQueries = 'Query budget must be a whole integer between 1 and 500.';
    }
  }

  // 2. Candidate Evaluation Limit
  if (!isStrictIntegerString(maxEvaluations)) {
    errors.maxEvaluations = 'Evaluation limit must be a whole integer between 1 and 2000.';
  } else {
    const e = Number(maxEvaluations);
    if (e < 1 || e > 2000) {
      errors.maxEvaluations = 'Evaluation limit must be a whole integer between 1 and 2000.';
    }
  }

  // 3. Max Concurrency
  if (!isStrictIntegerString(maxConcurrency)) {
    errors.maxConcurrency = 'Concurrency must be a whole integer between 1 and 32.';
  } else {
    const c = Number(maxConcurrency);
    if (c < 1 || c > 32) {
      errors.maxConcurrency = 'Concurrency must be a whole integer between 1 and 32.';
    }
  }

  // 4. Max Duration
  if (!isStrictIntegerString(maxRuntimeSeconds)) {
    errors.maxRuntimeSeconds = 'Run duration limit must be a whole integer between 10 and 3600 seconds.';
  } else {
    const r = Number(maxRuntimeSeconds);
    if (r < 10 || r > 3600) {
      errors.maxRuntimeSeconds = 'Run duration limit must be a whole integer between 10 and 3600 seconds.';
    }
  }

  // 5. Spend ceiling (optional, can be a valid float)
  if (spendCeiling !== '' && spendCeiling !== null && spendCeiling !== undefined) {
    const s = Number(spendCeiling);
    if (isNaN(s) || s < 0 || !isFinite(s)) {
      errors.spendCeiling = 'Spend limit must be a positive number if specified.';
    }
  }

  return {
    isValid: Object.keys(errors).length === 0,
    errors,
  };
}
