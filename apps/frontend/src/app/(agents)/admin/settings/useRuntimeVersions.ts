'use client';

import { useEffect, useRef, useState } from 'react';

import type { RuntimeVersions } from '@/lib/api/adminRuntimeUpgrades';
import { getRuntimeVersions } from '@/lib/api/adminRuntimeUpgrades';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

/**
 * Every npm release of each agent the page can update, read once per visit.
 *
 * Not polled: npm adds a release a day at most, and the control plane caches
 * each list for an hour. A failed read is kept as that agent's error, so the
 * menu still offers what the servers already have, and is asked again the
 * next time the tab opens.
 */
export function useRuntimeVersions({
  runtimes,
  enabled,
  onAccessDenied,
}: {
  runtimes: string[];
  enabled: boolean;
  onAccessDenied: (cause: unknown) => void;
}): Record<string, RuntimeVersions> {
  const [lists, setLists] = useState<Record<string, RuntimeVersions>>({});
  const read = useRef<Record<string, RuntimeVersions>>({});
  const denied = useRef(onAccessDenied);
  useEffect(() => {
    denied.current = onAccessDenied;
  });
  const wanted = runtimes.join('\n');

  useEffect(() => {
    if (!enabled || !wanted) return undefined;
    // Each run asks for whatever it does not have yet, and a run replaced
    // before its answers arrive gives way to the next, which asks again.
    const controller = new AbortController();
    const keep = (runtime: string, list: RuntimeVersions) => {
      read.current = { ...read.current, [runtime]: list };
      setLists(read.current);
    };
    for (const runtime of wanted.split('\n')) {
      const known = read.current[runtime];
      if (known && !known.error) continue;
      getRuntimeVersions(runtime, controller.signal)
        .then((list) => {
          if (!controller.signal.aborted) keep(runtime, list);
        })
        .catch((cause: unknown) => {
          if (controller.signal.aborted) return;
          if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
            denied.current(cause);
            return;
          }
          keep(runtime, { runtime, versions: [], deprecated: null, error: getErrorMessage(cause) });
        });
    }
    return () => controller.abort();
  }, [enabled, wanted]);

  return lists;
}
