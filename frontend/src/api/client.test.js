import { afterEach, describe, expect, it, vi } from 'vitest';

import { API_BASE_URL, ApiError, apiGet, apiPost, apiRequest } from './client.js';

function response(body, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('api client', () => {
  it('targets the configured API base URL and parses JSON', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ runs: [], count: 0 }));
    vi.stubGlobal('fetch', fetchMock);

    await expect(apiGet('/runs?limit=5')).resolves.toEqual({ runs: [], count: 0 });

    expect(API_BASE_URL).toBe('http://127.0.0.1:5000/api/v1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe(`${API_BASE_URL}/runs?limit=5`);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ method: 'GET' });
  });

  it('sends JSON bodies for POST requests', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ run: { run_id: 'run-1' } }, { status: 201 }));
    vi.stubGlobal('fetch', fetchMock);

    await apiPost('/runs', { name: 'A run' });

    const [, options] = fetchMock.mock.calls[0];
    expect(options.method).toBe('POST');
    expect(options.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(options.body)).toEqual({ name: 'A run' });
  });

  it('normalises backend validation errors and their field details', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        response(
          {
            error: 'Bad Request',
            message: 'Invalid run budget configuration.',
            status_code: 400,
            details: [{ field: 'max_oracle_queries', code: 'OUT_OF_RANGE', message: 'must be >= 0' }],
          },
          { status: 400 },
        ),
      ),
    );

    const error = await apiPost('/runs', {}).catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.status).toBe(400);
    expect(error.message).toBe('Invalid run budget configuration.');
    expect(error.kind).toBe('http');
    expect(error.fieldErrors).toEqual([
      { field: 'max_oracle_queries', code: 'OUT_OF_RANGE', message: 'must be >= 0' },
    ]);
  });

  it('rejects a successful response whose body is not JSON', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response('<html>not json</html>')));

    const error = await apiGet('/runs').catch((caught) => caught);

    expect(error.kind).toBe('malformed');
    expect(error.message).toMatch(/not valid JSON/i);
  });

  it('reports an unreachable backend with actionable context', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    const error = await apiGet('/runs').catch((caught) => caught);

    expect(error.kind).toBe('network');
    expect(error.message).toContain(API_BASE_URL);
    expect(error.message).toMatch(/Flask is running/i);
  });

  it('times out a request that never answers', async () => {
    vi.useFakeTimers();
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((_url, { signal }) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          const abortError = new Error('Aborted');
          abortError.name = 'AbortError';
          reject(abortError);
        });
      })),
    );

    const pending = apiRequest('/runs', { timeoutMs: 1000 });
    const assertion = expect(pending).rejects.toMatchObject({
      kind: 'timeout',
      message: expect.stringMatching(/did not respond within 1 seconds/i),
    });
    await vi.advanceTimersByTimeAsync(1000);
    await assertion;
  });

  it('marks a caller cancellation as aborted rather than failed', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((_url, { signal }) => new Promise((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          const abortError = new Error('Aborted');
          abortError.name = 'AbortError';
          reject(abortError);
        });
      })),
    );

    const controller = new AbortController();
    const pending = apiRequest('/runs', { signal: controller.signal });
    controller.abort();

    await expect(pending).rejects.toMatchObject({ kind: 'aborted' });
  });

  it('accepts an empty body as null', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response('', { status: 204 })));
    await expect(apiGet('/runs')).resolves.toBeNull();
  });
});
