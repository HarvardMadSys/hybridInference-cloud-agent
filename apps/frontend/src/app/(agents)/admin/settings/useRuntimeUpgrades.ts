'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import type { RuntimeUpgradeItem } from '@/lib/api/adminRuntimeUpgrades';
import { getRuntimeUpgrades, requestRuntimeUpgrade } from '@/lib/api/adminRuntimeUpgrades';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

/** While a server is downloading, the bar is read this often. */
const DOWNLOADING_POLL_MS = 3_000;
const IDLE_POLL_MS = 60_000;

/**
 * The agents this page can update, kept current while an update runs.
 *
 * `onUpdated` is called when an update finishes, because finishing switches
 * the version new chats use, which the rest of the page reads from the pool.
 */
export function useRuntimeUpgrades({
  enabled,
  onAccessDenied,
  onUpdated,
}: {
  enabled: boolean;
  onAccessDenied: (cause: unknown) => void;
  onUpdated: () => void;
}) {
  const [items, setItems] = useState<RuntimeUpgradeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const mounted = useRef(true);
  const states = useRef(new Map<string, string>());
  const callbacks = useRef({ onAccessDenied, onUpdated });
  useEffect(() => {
    callbacks.current = { onAccessDenied, onUpdated };
  });

  const adopt = useCallback((next: RuntimeUpgradeItem[]) => {
    let finished = false;
    for (const item of next) {
      const state = item.upgrade?.state ?? '';
      if (states.current.get(item.runtime) === 'downloading' && state === 'updated')
        finished = true;
      states.current.set(item.runtime, state);
    }
    setItems(next);
    setError(null);
    if (finished) callbacks.current.onUpdated();
  }, []);

  const fail = useCallback((cause: unknown) => {
    if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
      callbacks.current.onAccessDenied(cause);
    } else {
      setError(getErrorMessage(cause));
    }
  }, []);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    try {
      const next = await getRuntimeUpgrades(controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      adopt(next.runtimes);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    }
  }, [adopt, fail]);

  const downloading = items?.some((item) => item.upgrade?.state === 'downloading') ?? false;

  useEffect(() => {
    mounted.current = true;
    if (enabled) void load();
  }, [enabled, load]);

  // Its own effect, so a change of pace does not read the list again at once.
  useEffect(() => {
    if (!enabled) return undefined;
    const timer = setInterval(
      () => {
        if (!document.hidden) void load();
      },
      downloading ? DOWNLOADING_POLL_MS : IDLE_POLL_MS,
    );
    return () => clearInterval(timer);
  }, [enabled, downloading, load]);

  useEffect(
    () => () => {
      mounted.current = false;
      request.current?.abort();
    },
    [],
  );

  const update = useCallback(
    async (runtime: string, version: string) => {
      setBusy(true);
      setError(null);
      // A poll started before the request must not paint the old state after it.
      request.current?.abort();
      sequence.current += 1;
      try {
        const next = await requestRuntimeUpgrade(runtime, version);
        if (mounted.current) adopt(next.runtimes);
      } catch (cause) {
        if (mounted.current) fail(cause);
      } finally {
        if (mounted.current) setBusy(false);
      }
    },
    [adopt, fail],
  );

  return { items, error, busy, update, reload: load };
}
