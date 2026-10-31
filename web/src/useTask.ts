import { useCallback, useEffect, useReducer, useRef } from 'react';
import { ApiError, errorText, getTask } from './api';
import type { ServerEvent, TaskSnapshot } from './types';

interface State {
  snapshot: TaskSnapshot | null;
  connection: 'loading' | 'online' | 'reconnecting';
  error: string;
  notice: string;
  stale: boolean;
}
type Action =
  | { type: 'snapshot'; snapshot: TaskSnapshot }
  | { type: 'error'; message: string }
  | { type: 'unavailable'; message: string }
  | { type: 'connection'; value: State['connection'] }
  | { type: 'stale' }
  | { type: 'notice'; message: string };
const initial: State = { snapshot: null, connection: 'loading', error: '', notice: '', stale: false };
function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'snapshot':
      return { ...state, snapshot: action.snapshot, error: '', stale: false };
    case 'error':
      return { ...state, error: action.message };
    case 'unavailable':
      return { ...state, snapshot: null, error: action.message };
    case 'connection':
      return { ...state, connection: action.value };
    case 'stale':
      return { ...state, stale: true, notice: '信息已更新，请查看当前步骤' };
    case 'notice':
      return { ...state, notice: action.message };
  }
}

/** Events invalidate the projection; only a persisted snapshot changes business state. */
export function useTask(id: string, onSessionExpired: () => void) {
  const [state, dispatch] = useReducer(reducer, initial);
  const refreshRef = useRef<() => Promise<void>>(async () => {});
  const expireRef = useRef(onSessionExpired);
  expireRef.current = onSessionExpired;
  useEffect(() => {
    let disposed = false;
    let source: EventSource | undefined;
    let subscribed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let cursor = 0;
    let refreshing: Promise<void> | null = null;
    let refreshAgain = false;
    const controller = new AbortController();
    const snapshot = async () => {
      if (refreshing) {
        refreshAgain = true;
        return refreshing;
      }
      refreshing = (async () => {
        do {
          refreshAgain = false;
          const data = await getTask(id, { signal: controller.signal });
          if (disposed) return;
          // A reset can deliberately move the cursor backwards after a restore.
          cursor = data.last_event_seq;
          dispatch({ type: 'snapshot', snapshot: data });
        } while (refreshAgain && !disposed);
      })();
      try {
        await refreshing;
      } finally {
        refreshing = null;
      }
    };
    const failed = (error: unknown) => {
      if (disposed) return;
      if (error instanceof ApiError && error.status === 401) {
        source?.close();
        subscribed = false;
        expireRef.current();
      } else if (error instanceof ApiError && error.status === 404) {
        source?.close();
        subscribed = false;
        dispatch({ type: 'unavailable', message: errorText(error) });
      } else dispatch({ type: 'error', message: errorText(error) });
    };
    const connect = () => {
      if (disposed) return;
      clearTimeout(timer);
      source?.close();
      subscribed = true;
      source = new EventSource(`/api/tasks/${encodeURIComponent(id)}/events?after=${cursor}`, {
        withCredentials: true,
      });
      source.onopen = () => {
        if (!disposed) dispatch({ type: 'connection', value: 'online' });
      };
      const receive = (raw: MessageEvent) => {
        let event: ServerEvent;
        try {
          event = JSON.parse(raw.data);
        } catch {
          void snapshot().catch(failed);
          return;
        }
        const seq = Number(event.seq || raw.lastEventId);
        if (!Number.isSafeInteger(seq) || seq <= 0) {
          void snapshot().catch(failed);
          return;
        }
        if (seq <= cursor) return;
        // A gap can include staff-only events. Re-read the authorized current projection.
        if (seq > cursor + 1) dispatch({ type: 'connection', value: 'reconnecting' });
        if (event.type === 'memory_changed') dispatch({ type: 'stale' });
        void snapshot()
          .then(() => {
            if (!disposed) dispatch({ type: 'connection', value: 'online' });
          })
          .catch(failed);
      };
      source.onmessage = receive;
      for (const name of [
        'progress',
        'product_card',
        'step_card',
        'escalation_card',
        'receipt',
        'memory_changed',
        'task_state',
      ])
        source.addEventListener(name, receive as EventListener);
      source.addEventListener('reset', () => {
        source?.close();
        subscribed = false;
        void snapshot().then(connect).catch(failed);
      });
      source.addEventListener('session_expired', () => {
        source?.close();
        subscribed = false;
        expireRef.current();
      });
      source.onerror = () => {
        source?.close();
        subscribed = false;
        if (disposed) return;
        dispatch({ type: 'connection', value: 'reconnecting' });
        timer = setTimeout(() => {
          void snapshot()
            .then(connect)
            .catch((error) => {
              failed(error);
              if (!(error instanceof ApiError && [401, 404].includes(error.status)))
                timer = setTimeout(connect, 3000);
            });
        }, 1500);
      };
    };
    refreshRef.current = async () => {
      await snapshot();
      if (!subscribed) connect();
    };
    void snapshot().then(connect).catch(failed);
    return () => {
      disposed = true;
      controller.abort();
      source?.close();
      clearTimeout(timer);
    };
  }, [id]);
  const refresh = useCallback(() => refreshRef.current(), []);
  const conflict = useCallback(async () => {
    dispatch({ type: 'stale' });
    await refreshRef.current();
  }, []);
  const setNotice = useCallback((message: string) => dispatch({ type: 'notice', message }), []);
  return { ...state, refresh, conflict, setNotice };
}
