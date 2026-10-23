/**
 * Central HTTP access to the CLARITY Flask API.
 *
 * Every request goes through `apiRequest`, which owns:
 *   - the configurable API base URL,
 *   - JSON encoding/decoding,
 *   - request timeouts and caller-driven aborts,
 *   - normalisation of HTTP, network and malformed-response failures.
 *
 * Presentation components never call `fetch` directly.
 */

export const DEFAULT_API_BASE_URL = 'http://127.0.0.1:5000/api/v1';

// `import.meta.env` is provided by Vite; the fallbacks keep the module usable in tests.
const configuredBaseUrl =
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_BASE_URL) ||
  DEFAULT_API_BASE_URL;

export const API_BASE_URL = String(configuredBaseUrl).replace(/\/+$/, '');

const DEFAULT_TIMEOUT_MS = 15_000;

/**
 * Normalised API failure. `kind` is one of:
 *   - "http"      the backend answered with a non-2xx status
 *   - "network"   the backend could not be reached at all
 *   - "timeout"   the request exceeded its deadline
 *   - "aborted"   the caller cancelled the request (not a user-facing failure)
 *   - "malformed" the backend answered with a body the client cannot understand
 */
export class ApiError extends Error {
  constructor(message, { status = 0, code = null, details = null, kind = 'http', cause } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
    this.kind = kind;
    if (cause) this.cause = cause;
  }

  get isAborted() {
    return this.kind === 'aborted';
  }

  /** Field-level validation errors emitted by the Flask API. */
  get fieldErrors() {
    if (!Array.isArray(this.details)) return [];
    return this.details
      .filter((entry) => entry && typeof entry === 'object')
      .map((entry) => ({
        field: typeof entry.field === 'string' ? entry.field : null,
        message: typeof entry.message === 'string' ? entry.message : null,
        code: typeof entry.code === 'string' ? entry.code : null,
      }))
      .filter((entry) => entry.message);
  }
}

function buildUrl(path) {
  if (!path) return API_BASE_URL;
  return `${API_BASE_URL}${path.startsWith('/') ? path : `/${path}`}`;
}

function readTimeout(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_TIMEOUT_MS;
}

/**
 * Perform one API request.
 *
 * @param {string} path e.g. "/runs/run-1/status" (relative to the API base URL)
 * @param {object} [options]
 * @param {string} [options.method]
 * @param {object} [options.body] JSON-serialisable payload
 * @param {AbortSignal} [options.signal] caller cancellation
 * @param {number} [options.timeoutMs]
 * @returns {Promise<any>} decoded JSON body
 */
export async function apiRequest(path, options = {}) {
  const {
    method = 'GET',
    body,
    signal,
    timeoutMs = readTimeout(
      typeof import.meta !== 'undefined' ? import.meta.env?.VITE_API_TIMEOUT_MS : undefined,
    ),
  } = options;

  const controller = new AbortController();
  let timedOut = false;
  let callerAborted = false;

  const onCallerAbort = () => {
    callerAborted = true;
    controller.abort();
  };

  if (signal) {
    if (signal.aborted) {
      throw new ApiError('Request was cancelled.', { kind: 'aborted' });
    }
    signal.addEventListener('abort', onCallerAbort, { once: true });
  }

  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  let response;
  try {
    response = await fetch(buildUrl(path), {
      method,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (error) {
    if (timedOut) {
      throw new ApiError(
        `The CLARITY backend did not respond within ${Math.round(timeoutMs / 1000)} seconds.`,
        { kind: 'timeout', cause: error },
      );
    }
    if (callerAborted || error?.name === 'AbortError') {
      throw new ApiError('Request was cancelled.', { kind: 'aborted', cause: error });
    }
    throw new ApiError(
      `The CLARITY backend could not be reached at ${API_BASE_URL}. Check that Flask is running and that the API base URL is correct.`,
      { kind: 'network', cause: error },
    );
  } finally {
    clearTimeout(timer);
    if (signal) signal.removeEventListener('abort', onCallerAbort);
  }

  const rawBody = await response.text().catch(() => '');
  let payload = null;
  if (rawBody) {
    try {
      payload = JSON.parse(rawBody);
    } catch (error) {
      if (response.ok) {
        throw new ApiError(
          'The CLARITY backend returned a response that is not valid JSON.',
          { status: response.status, kind: 'malformed', cause: error },
        );
      }
    }
  }

  if (!response.ok) {
    const message =
      (payload && typeof payload.message === 'string' && payload.message) ||
      (payload && typeof payload.error === 'string' && payload.error) ||
      `The backend rejected the request (HTTP ${response.status}).`;
    throw new ApiError(message, {
      status: response.status,
      code: payload && typeof payload.error === 'string' ? payload.error : null,
      details: payload && payload.details !== undefined ? payload.details : null,
    });
  }

  return payload;
}

export const apiGet = (path, options = {}) => apiRequest(path, { ...options, method: 'GET' });

export const apiPost = (path, body = undefined, options = {}) =>
  apiRequest(path, { ...options, method: 'POST', body });
