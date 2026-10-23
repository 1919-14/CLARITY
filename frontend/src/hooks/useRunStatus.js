import { useCallback, useEffect, useRef, useState } from 'react';

import { isTerminalRunState } from '../api/contracts.js';
import { getRun, getRunBudget, getRunStatus } from '../api/runs.js';

export const DEFAULT_POLL_INTERVAL_MS = 4000;

function initialState(runId) {
  return {
    runId: runId || null,
    run: null,
    progress: null,
    budget: null,
    isLoading: Boolean(runId),
    isRefreshing: false,
    isStale: false,
    error: null,
    refreshedAt: null,
  };
}

/**
 * Load one run's persisted state, lifecycle progress and budget, then poll at a
 * bounded interval until the backend reports a terminal state.
 *
 * Guarantees:
 *   - at most one request batch is in flight at a time;
 *   - the interval is torn down on unmount, run change, or terminal state;
 *   - a failed refresh keeps the last known values and marks them stale;
 *   - a 404 stops polling, because the run does not exist.
 */
export function useRunStatus(runId, { pollIntervalMs = DEFAULT_POLL_INTERVAL_MS } = {}) {
  const [state, setState] = useState(() => initialState(runId));
  const controllerRef = useRef(null);
  const inFlightRef = useRef(false);
  const requestIdRef = useRef(0);
  const mountedRef = useRef(true);

  // Switching runs resets the ledger during render, so the previous run's
  // values can never be mistaken for the new run's state.
  if (state.runId !== (runId || null)) {
    setState(initialState(runId));
  }

  const load = useCallback(
    async ({ background = false } = {}) => {
      if (!runId || inFlightRef.current) return;
      inFlightRef.current = true;

      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      const requestId = ++requestIdRef.current;

      if (background) {
        setState((previous) => ({ ...previous, isRefreshing: true }));
      }

      try {
        const [runPayload, progressPayload, budgetPayload] = await Promise.all([
          getRun(runId, { signal: controller.signal }),
          getRunStatus(runId, { signal: controller.signal }),
          getRunBudget(runId, { signal: controller.signal }),
        ]);
        if (!mountedRef.current || controller.signal.aborted || requestId !== requestIdRef.current) {
          return;
        }
        setState({
          runId,
          run: runPayload?.run ?? null,
          progress: progressPayload?.status ?? null,
          budget: budgetPayload?.budget ?? null,
          isLoading: false,
          isRefreshing: false,
          isStale: false,
          error: null,
          refreshedAt: new Date(),
        });
      } catch (error) {
        if (!mountedRef.current || controller.signal.aborted || error?.isAborted) return;
        setState((previous) => ({
          ...previous,
          isLoading: false,
          isRefreshing: false,
          isStale: previous.run !== null,
          error,
        }));
      } finally {
        // A newer request may already have taken over; only clear the flag for
        // the request that still owns it.
        if (requestId === requestIdRef.current) inFlightRef.current = false;
      }
    },
    [runId],
  );

  useEffect(() => {
    mountedRef.current = true;
    // Initial read from the backend. State is only updated after the response.
    // oxlint-disable-next-line react/set-state-in-effect
    if (runId) load();

    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
      controllerRef.current?.abort();
      inFlightRef.current = false;
    };
  }, [runId, load]);

  const currentStatus = state.run?.status || state.progress?.status;
  const isTerminal = isTerminalRunState(currentStatus);
  const isMissing = state.error?.status === 404;

  useEffect(() => {
    // Wait for the first response before scheduling refreshes: that avoids a
    // second request racing the initial load, and stops immediately when the
    // first response already reports a terminal state.
    if (!runId || isTerminal || isMissing || state.isLoading) return undefined;
    const timer = setInterval(() => {
      load({ background: true });
    }, pollIntervalMs);
    return () => clearInterval(timer);
  }, [runId, isTerminal, isMissing, state.isLoading, pollIntervalMs, load]);

  const refresh = useCallback(
    () => load({ background: state.run !== null }),
    [load, state.run],
  );

  return {
    ...state,
    status: currentStatus || null,
    isTerminal,
    isMissing,
    isPolling: Boolean(runId) && !isTerminal && !isMissing,
    refresh,
  };
}
