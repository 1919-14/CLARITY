import { describe, it, expect, vi, beforeEach } from 'vitest';
import { api, ApiError } from './client';

describe('CLARITY API Client', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('handles successful API requests', async () => {
    const mockData = { status: 'healthy', service: 'clarity-backend' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: {
        get: (h) => (h === 'content-type' ? 'application/json' : null),
      },
      json: async () => mockData,
    });

    const result = await api.getHealth();
    expect(result).toEqual(mockData);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/health'),
      expect.objectContaining({
        headers: expect.objectContaining({ Accept: 'application/json' }),
      })
    );
  });

  it('throws structured ApiError on HTTP failures', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 400,
      statusText: 'Bad Request',
      headers: {
        get: (h) => (h === 'content-type' ? 'application/json' : null),
      },
      json: async () => ({
        code: 'INVALID_CONFIG',
        message: 'Oracle query budget exceeds ceiling',
      }),
    });

    await expect(api.createRun({ max_oracle_queries: 9999 })).rejects.toThrow(
      'Oracle query budget exceeds ceiling'
    );
  });

  it('submits valid POST body on run creation', async () => {
    const mockCreatedRun = { run_id: 'run-test-01', status: 'pending' };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      headers: {
        get: (h) => (h === 'content-type' ? 'application/json' : null),
      },
      json: async () => mockCreatedRun,
    });

    const payload = {
      name: 'Test Run',
      domain: '1d_polynomial',
      execution_mode: 'local',
      max_oracle_queries: 20,
    };

    const res = await api.createRun(payload);
    expect(res).toEqual(mockCreatedRun);
    expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/runs'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(payload),
      })
    );
  });
});
