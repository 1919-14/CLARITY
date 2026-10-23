import { describe, expect, it } from 'vitest';

import { DEFAULT_RUN_FORM, validateRunForm } from './validation.js';

const validValues = {
  ...DEFAULT_RUN_FORM,
  name: 'Nocturnal growth response',
};

describe('run configuration validation', () => {
  it('accepts the backend defaults and builds the documented payload', () => {
    const { errors, payload, warnings } = validateRunForm(validValues);

    expect(errors).toEqual({});
    expect(warnings).toEqual([]);
    expect(payload).toEqual({
      name: 'Nocturnal growth response',
      domain: 'default',
      execution_mode: 'local',
      budget: {
        max_oracle_queries: 10,
        max_candidate_evaluations: 100,
        max_concurrent_remote_tasks: 4,
        max_runtime_seconds: 3600,
        max_compute_spend_usd: null,
      },
    });
  });

  it('requires a run name and rejects whitespace-only names', () => {
    const missing = validateRunForm({ ...validValues, name: '   ' });
    expect(missing.payload).toBeNull();
    expect(missing.errors.name).toMatch(/run name is required/i);
  });

  it('requires a non-empty input domain', () => {
    const missing = validateRunForm({ ...validValues, domain: '  ' });
    expect(missing.errors.domain).toMatch(/input domain/i);
  });

  it('rejects negative budgets in plain language', () => {
    const result = validateRunForm({ ...validValues, maxOracleQueries: '-1' });
    expect(result.payload).toBeNull();
    expect(result.errors.maxOracleQueries).toBe('Oracle query budget cannot be negative. The minimum is 0.');
  });

  it('rejects decimals for integer limits and non-numeric values', () => {
    const decimal = validateRunForm({ ...validValues, maxCandidateEvaluations: '12.5' });
    expect(decimal.errors.maxCandidateEvaluations).toMatch(/whole number/i);

    const text = validateRunForm({ ...validValues, maxConcurrentRemoteTasks: 'four' });
    expect(text.errors.maxConcurrentRemoteTasks).toMatch(/must be a number/i);
  });

  it('enforces the backend maximums', () => {
    const tooMany = validateRunForm({ ...validValues, maxRuntimeSeconds: '604801' });
    expect(tooMany.errors.maxRuntimeSeconds).toMatch(/backend maximum of 604,800/i);

    const tooConcurrent = validateRunForm({ ...validValues, maxConcurrentRemoteTasks: '33' });
    expect(tooConcurrent.errors.maxConcurrentRemoteTasks).toMatch(/maximum of 32/i);
  });

  it('treats a blank spending limit as disabled rather than zero', () => {
    const blank = validateRunForm({ ...validValues, maxComputeSpendUsd: '' });
    expect(blank.errors).toEqual({});
    expect(blank.payload.budget.max_compute_spend_usd).toBeNull();

    const zero = validateRunForm({ ...validValues, maxComputeSpendUsd: '0' });
    expect(zero.payload.budget.max_compute_spend_usd).toBe(0);
  });

  it('rejects spending limits with more than two decimal places', () => {
    const result = validateRunForm({ ...validValues, maxComputeSpendUsd: '1.234' });
    expect(result.errors.maxComputeSpendUsd).toMatch(/two decimal places/i);
  });

  it('warns when the budget permits no scientific work', () => {
    const result = validateRunForm({
      ...validValues,
      maxOracleQueries: '0',
      maxCandidateEvaluations: '0',
    });
    expect(result.payload).not.toBeNull();
    expect(result.warnings[0]).toMatch(/permits no scientific work/i);
  });

  it('rejects an unsupported execution mode', () => {
    const result = validateRunForm({ ...validValues, executionMode: 'quantum' });
    expect(result.errors.executionMode).toMatch(/execution modes supported by the backend/i);
  });
});
