import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Load one resource through the API client.
 *
 * `loader` receives `{ signal }` and must be memoised by the caller (for
 * example with `useCallback`) so the request only restarts when its inputs
 * change. Requests are aborted on unmount, on reload and when `loader` changes.
 */
export function useResource(loader, { enabled = true } = {}) {
  const [state, setState] = useState(() => ({
    source: loader,
    data: null,
    isLoading: enabled,
    isRefreshing: false,
    error: null,
  }));
  const controllerRef = useRef(null);
  const mountedRef = useRef(true);

  // A new loader means a different resource (for example another run). Reset
  // during render instead of in an effect so the old data never flashes under
  // the new selection.
  if (state.source !== loader) {
    setState({
      source: loader,
      data: null,
      isLoading: enabled,
      isRefreshing: false,
      error: null,
    });
  }

  const load = useCallback(
    async ({ background = false } = {}) => {
      if (!enabled) return;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;

      if (background) {
        setState((previous) => ({ ...previous, isRefreshing: true, error: null }));
      }

      try {
        const data = await loader({ signal: controller.signal });
        if (!mountedRef.current || controller.signal.aborted) return;
        setState({ source: loader, data, isLoading: false, isRefreshing: false, error: null });
      } catch (error) {
        if (!mountedRef.current || controller.signal.aborted || error?.isAborted) return;
        setState((previous) => ({ ...previous, isLoading: false, isRefreshing: false, error }));
      }
    },
    [loader, enabled],
  );

  useEffect(() => {
    mountedRef.current = true;
    // Initial read from the backend. The state update happens after the
    // response arrives, not synchronously during the effect.
    // oxlint-disable-next-line react/set-state-in-effect
    if (enabled) load();
    return () => {
      mountedRef.current = false;
      controllerRef.current?.abort();
    };
  }, [load, enabled]);

  const reload = useCallback(() => load({ background: true }), [load]);

  const { source: _source, ...visible } = state;
  return { ...visible, reload };
}
