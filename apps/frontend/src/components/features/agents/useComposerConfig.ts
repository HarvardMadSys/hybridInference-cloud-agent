'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { MODEL_CONNECTIONS_CHANGED } from './modelConnectionEvents';
import { SOURCE_CONTROL_RECHECK_MS } from './sourceControlRecovery';

/** Re-read the server's choices after a connection changes or the owner returns. */
export function useComposerConfig(
  onLoaded: (config: AgentConfigApi) => void,
  onError: (cause: unknown) => void,
  retrySourceControl = false,
) {
  const handlers = useRef({ onLoaded, onError });
  handlers.current = { onLoaded, onError };
  const latest = useRef(0);
  // Only background polling waits. Owner-triggered reads can still supersede
  // a slow request, whose eventual completion must not clear this flag.
  const pendingLatest = useRef(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);

  const refresh = useCallback(async (options: { fresh?: boolean } = {}) => {
    const request = ++latest.current;
    pendingLatest.current = true;
    setRefreshing(true);
    try {
      const config = await getAgentConfig(options);
      if (request !== latest.current) return undefined;
      handlers.current.onLoaded(config);
      return config;
    } catch (cause) {
      if (request === latest.current) handlers.current.onError(cause);
      return undefined;
    } finally {
      if (request === latest.current) {
        pendingLatest.current = false;
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, []);

  useEffect(() => {
    const read = () => {
      if (document.visibilityState !== 'hidden') void refresh();
    };
    const changed = () => void refresh();
    const storage = (event: StorageEvent) => {
      if (event.key === MODEL_CONNECTIONS_CHANGED) changed();
    };
    void refresh();
    window.addEventListener(MODEL_CONNECTIONS_CHANGED, changed);
    window.addEventListener('storage', storage);
    window.addEventListener('focus', read);
    document.addEventListener('visibilitychange', read);
    return () => {
      latest.current += 1;
      window.removeEventListener(MODEL_CONNECTIONS_CHANGED, changed);
      window.removeEventListener('storage', storage);
      window.removeEventListener('focus', read);
      document.removeEventListener('visibilitychange', read);
    };
  }, [refresh]);

  useEffect(() => {
    if (!retrySourceControl) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'hidden' && !pendingLatest.current) void refresh();
    }, SOURCE_CONTROL_RECHECK_MS);
    return () => window.clearInterval(timer);
  }, [retrySourceControl, refresh]);

  return { loading, refreshing, refresh };
}
