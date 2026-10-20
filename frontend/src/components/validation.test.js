import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { validateRunConfig } from './validation.js';

describe('Run Configuration Validation', () => {
  const validBase = {
    name: 'Polynomial Discovery Run',
    maxQueries: '15',
    maxEvaluations: '50',
    maxConcurrency: '4',
    maxRuntimeSeconds: '300',
    spendCeiling: '',
  };

  it('accepts valid configuration', () => {
    const res = validateRunConfig(validBase);
    assert.equal(res.isValid, true);
    assert.deepEqual(res.errors, {});
  });

  it('rejects empty or whitespace-only name', () => {
    const res = validateRunConfig({ ...validBase, name: '   ' });
    assert.equal(res.isValid, false);
    assert.ok(res.errors.name);
  });

  it('rejects floating-point numbers for integer fields (prevents silent truncation like 1.5 -> 1)', () => {
    const resQueries = validateRunConfig({ ...validBase, maxQueries: '1.5' });
    assert.equal(resQueries.isValid, false);
    assert.equal(resQueries.errors.maxQueries, 'Query budget must be a whole integer between 1 and 500.');

    const resEvals = validateRunConfig({ ...validBase, maxEvaluations: '50.2' });
    assert.equal(resEvals.isValid, false);
    assert.equal(resEvals.errors.maxEvaluations, 'Evaluation limit must be a whole integer between 1 and 2000.');

    const resConcurrency = validateRunConfig({ ...validBase, maxConcurrency: '4.8' });
    assert.equal(resConcurrency.isValid, false);
    assert.equal(resConcurrency.errors.maxConcurrency, 'Concurrency must be a whole integer between 1 and 32.');

    const resRuntime = validateRunConfig({ ...validBase, maxRuntimeSeconds: '300.9' });
    assert.equal(resRuntime.isValid, false);
    assert.equal(resRuntime.errors.maxRuntimeSeconds, 'Run duration limit must be a whole integer between 10 and 3600 seconds.');
  });

  it('rejects non-numeric string values for integer fields', () => {
    const res = validateRunConfig({ ...validBase, maxQueries: 'ten' });
    assert.equal(res.isValid, false);
    assert.equal(res.errors.maxQueries, 'Query budget must be a whole integer between 1 and 500.');
  });

  it('enforces lower and upper numerical bounds', () => {
    const tooLow = validateRunConfig({
      ...validBase,
      maxQueries: '0',
      maxEvaluations: '0',
      maxConcurrency: '0',
      maxRuntimeSeconds: '5',
    });
    assert.equal(tooLow.isValid, false);
    assert.ok(tooLow.errors.maxQueries);
    assert.ok(tooLow.errors.maxEvaluations);
    assert.ok(tooLow.errors.maxConcurrency);
    assert.ok(tooLow.errors.maxRuntimeSeconds);

    const tooHigh = validateRunConfig({
      ...validBase,
      maxQueries: '501',
      maxEvaluations: '2001',
      maxConcurrency: '33',
      maxRuntimeSeconds: '3601',
    });
    assert.equal(tooHigh.isValid, false);
    assert.ok(tooHigh.errors.maxQueries);
    assert.ok(tooHigh.errors.maxEvaluations);
    assert.ok(tooHigh.errors.maxConcurrency);
    assert.ok(tooHigh.errors.maxRuntimeSeconds);
  });

  it('validates optional spend ceiling', () => {
    const validSpend = validateRunConfig({ ...validBase, spendCeiling: '10.50' });
    assert.equal(validSpend.isValid, true);

    const negativeSpend = validateRunConfig({ ...validBase, spendCeiling: '-5.00' });
    assert.equal(negativeSpend.isValid, false);
    assert.ok(negativeSpend.errors.spendCeiling);

    const invalidSpend = validateRunConfig({ ...validBase, spendCeiling: 'abc' });
    assert.equal(invalidSpend.isValid, false);
    assert.ok(invalidSpend.errors.spendCeiling);
  });
});
