import { useCallback, useEffect, useRef, useState } from 'react';

import { listRuns } from '../api/runs.js';

const INITIAL_STATE = {
  runs: null,
  isLoading: true,
  isRefreshing: false,
  error: null,
  loadedAt: null,
};

/**
 * Load persisted runs from `GET /api/v1/runs`.
 *
 * A background refresh keeps the previous rows visible instead of flashing the
 * loading state, and every request is aborted when a newer one starts.
 */
export function useRuns({ limit = 20 } = {}) {
  const [state, setState] = useState(INITIAL_STATE);
  const controllerRef = useRef(null);
  const mountedRef = useRef(true);

  const load = useCallback(
    async ({ background = false } = {}) => {
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      // The initial render already reports `isLoading`. Only a deliberate
      // refresh needs a synchronous state transition here.
      if (background) {
        setState((previous) => ({ ...previous, isRefreshing: true, error: null }));
      }

      try {
        const payload = await listRuns({ limit, signal: controller.signal });
        if (!mountedRef.current || controller.signal.aborted) return;
        setState({
          runs: Array.isArray(payload?.runs) ? payload.runs : [],
          isLoading: false,
          isRefreshing: false,
          error: null,
          loadedAt: new Date(),
        });
      } catch (error) {
        if (!mountedRef.current || controller.signal.aborted || error?.isAborted) return;
        setState((previous) => ({
          ...previous,
          isLoading: false,
          isRefreshing: false,
          error,
        }));
      }
    },
    [limit],
  );

  useEffect(() => {
    mountedRef.current = true;
    // Initial read from the backend. State is only updated after the response.
    // oxlint-disable-next-line react/set-state-in-effect
    load();
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, [load]);

  const reload = useCallback(() => load({ background: true }), [load]);

  return { ...state, reload };
}
