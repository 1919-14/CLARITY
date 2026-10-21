import { describe, it, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { api, ApiError } from './client.js';

describe('CLARITY API Client', () => {
  beforeEach(() => {
    delete globalThis.fetch;
  });

  it('handles successful API requests', async () => {
    const mockData = { status: 'healthy', service: 'clarity-backend' };
    let capturedUrl = null;
    let capturedOptions = null;

    globalThis.fetch = async (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      return {
        ok: true,
        headers: {
          get: (h) => (h === 'content-type' ? 'application/json' : null),
        },
        json: async () => mockData,
      };
    };

    const result = await api.getHealth();
    assert.deepEqual(result, mockData);
    assert.ok(capturedUrl.includes('/api/health'));
    assert.equal(capturedOptions.headers.Accept, 'application/json');
  });

  it('throws structured ApiError on HTTP failures', async () => {
    globalThis.fetch = async () => ({
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

    await assert.rejects(
      async () => {
        await api.createRun({ max_oracle_queries: 9999 });
      },
      (err) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.status, 400);
        assert.equal(err.message, 'Oracle query budget exceeds ceiling');
        return true;
      }
    );
  });

  it('submits valid POST body on run creation', async () => {
    const mockCreatedRun = { run_id: 'run-test-01', status: 'pending' };
    let capturedUrl = null;
    let capturedOptions = null;

    globalThis.fetch = async (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      return {
        ok: true,
        headers: {
          get: (h) => (h === 'content-type' ? 'application/json' : null),
        },
        json: async () => mockCreatedRun,
      };
    };

    const payload = {
      name: 'Test Run',
      domain: '1d_polynomial',
      execution_mode: 'local',
      max_oracle_queries: 20,
    };

    const res = await api.createRun(payload);
    assert.deepEqual(res, mockCreatedRun);
    assert.ok(capturedUrl.includes('/api/runs'));
    assert.equal(capturedOptions.method, 'POST');
    assert.equal(capturedOptions.body, JSON.stringify(payload));
  });

  it('handles run cancellation endpoint', async () => {
    const mockCancelled = { run_id: 'run-cancel-01', status: 'cancelled' };
    let capturedUrl = null;
    let capturedOptions = null;

    globalThis.fetch = async (url, options) => {
      capturedUrl = url;
      capturedOptions = options;
      return {
        ok: true,
        headers: {
          get: (h) => (h === 'content-type' ? 'application/json' : null),
        },
        json: async () => mockCancelled,
      };
    };

    const res = await api.cancelRun('run-cancel-01');
    assert.deepEqual(res, mockCancelled);
    assert.ok(capturedUrl.includes('/api/runs/run-cancel-01/cancel'));
    assert.equal(capturedOptions.method, 'POST');
  });
});
