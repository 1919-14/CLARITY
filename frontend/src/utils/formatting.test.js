import { describe, expect, it } from 'vitest';

import {
  executionModeLabel,
  formatCostUsd,
  formatDateTime,
  formatDecimal,
  formatInteger,
  formatLimit,
  formatRemaining,
  formatSeconds,
  phaseLabel,
  shortId,
  statusLabel,
  statusTone,
  UNAVAILABLE,
} from './formatting.js';

describe('formatting', () => {
  it('never turns a missing cost into zero', () => {
    expect(formatCostUsd(null)).toBe(UNAVAILABLE);
    expect(formatCostUsd(undefined)).toBe(UNAVAILABLE);
    expect(formatCostUsd(Number.NaN)).toBe(UNAVAILABLE);
    expect(formatCostUsd(0)).toBe('$0.0000');
    expect(formatCostUsd(1.23456)).toBe('$1.2346');
  });

  it('distinguishes measured zero from unavailable for counts and decimals', () => {
    expect(formatInteger(0)).toBe('0');
    expect(formatInteger(null)).toBe(UNAVAILABLE);
    expect(formatDecimal(0)).toBe('0.0000');
    expect(formatDecimal(null)).toBe(UNAVAILABLE);
  });

  it('formats limits and remaining capacity without inventing a limit', () => {
    expect(formatLimit({ used: 2, limit: 10 })).toBe('2 / 10');
    expect(formatLimit({ used: 0, limit: 0 })).toBe('0 / 0');
    expect(formatLimit({ used: 3, limit: null })).toBe('3 · no limit');
    expect(formatLimit(null)).toBe(UNAVAILABLE);
    expect(formatRemaining({ limit: 10, remaining: 8 })).toBe('8');
    expect(formatRemaining({ limit: null, remaining: null, disabled: true })).toBe('No limit');
    expect(formatRemaining({ limit: 10, remaining: null })).toBe(UNAVAILABLE);
  });

  it('formats UTC timestamps deterministically and rejects invalid input', () => {
    expect(formatDateTime('2026-10-24T08:00:00+00:00')).toBe('24 Oct 2026, 08:00 UTC');
    expect(formatDateTime('not-a-date')).toBe(UNAVAILABLE);
    expect(formatDateTime(null)).toBe(UNAVAILABLE);
  });

  it('formats durations across seconds, minutes, hours and days', () => {
    expect(formatSeconds(0)).toBe('0 s');
    expect(formatSeconds(null)).toBe(UNAVAILABLE);
    expect(formatSeconds(42.5)).toBe('43 s');
    expect(formatSeconds(125)).toBe('2 m 05 s');
    expect(formatSeconds(3725)).toBe('1 h 02 m');
    expect(formatSeconds(90_000)).toBe('1 d 01 h');
  });

  it('uses the documented lifecycle vocabulary with a safe fallback', () => {
    expect(statusLabel('stopped_by_budget')).toBe('Stopped by budget');
    expect(statusLabel('future_state')).toBe('Future State');
    expect(statusLabel(null)).toBe(UNAVAILABLE);
    expect(statusTone('running')).toBe('active');
    expect(statusTone('completed')).toBe('success');
    expect(statusTone('unknown')).toBe('neutral');
    expect(phaseLabel('candidate_evaluation')).toBe('Candidate evaluation');
    expect(phaseLabel('some_new_phase')).toBe('Some New Phase');
    expect(executionModeLabel('nodus_remote')).toBe('Nodus remote');
    expect(executionModeLabel(null)).toBe(UNAVAILABLE);
  });

  it('abbreviates very long identifiers but keeps short ones whole', () => {
    expect(shortId('run-1')).toBe('run-1');
    expect(shortId('1234567890abcdefghijklmnop')).toBe('1234567890ab…klmnop');
    expect(shortId(null)).toBe(UNAVAILABLE);
  });
});
