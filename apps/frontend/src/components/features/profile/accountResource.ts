'use client';

import { useCallback, useEffect, useSyncExternalStore } from 'react';

interface Snapshot<T> {
  data: T | null;
  error: boolean;
}

const clearers = new Set<() => void>();

/** Drop private account data and pending reads before a session changes. */
export function clearAccountResources() {
  clearers.forEach((clear) => clear());
}

/** Share successful reads across views, retaining them during revalidation. */
export function createAccountResource<T>(load: (signal: AbortSignal) => Promise<T>) {
  const empty: Snapshot<T> = { data: null, error: false };
  const entries = new Map<
    string,
    {
      snapshot: Snapshot<T>;
      listeners: Set<() => void>;
      controller: AbortController | null;
    }
  >();

  const entryFor = (userId: string) => {
    let entry = entries.get(userId);
    if (!entry) {
      entry = { snapshot: empty, listeners: new Set(), controller: null };
      entries.set(userId, entry);
    }
    return entry;
  };

  const publish = (entry: ReturnType<typeof entryFor>, snapshot: Snapshot<T>) => {
    entry.snapshot = snapshot;
    entry.listeners.forEach((listener) => listener());
  };

  const refresh = (userId: string) => {
    const entry = entryFor(userId);
    if (entry.controller) return;
    const controller = new AbortController();
    entry.controller = controller;
    if (entry.snapshot.error) publish(entry, { ...entry.snapshot, error: false });
    void load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) publish(entry, { data, error: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          publish(entry, { data: entry.snapshot.data, error: entry.snapshot.data === null });
        }
      })
      .finally(() => {
        if (entry.controller === controller) entry.controller = null;
      });
  };

  const write = (userId: string, data: T) => {
    const entry = entryFor(userId);
    // A save supersedes any older GET that is still in flight.
    entry.controller?.abort();
    entry.controller = null;
    publish(entry, { data, error: false });
  };

  clearers.add(() => {
    const previous = [...entries.values()];
    entries.clear();
    previous.forEach((entry) => {
      entry.controller?.abort();
      entry.listeners.forEach((listener) => listener());
    });
  });

  function useResource(userId?: string) {
    const subscribe = useCallback(
      (listener: () => void) => {
        if (!userId) return () => {};
        const entry = entryFor(userId);
        entry.listeners.add(listener);
        return () => {
          entry.listeners.delete(listener);
          if (!entry.listeners.size) {
            entry.controller?.abort();
            entry.controller = null;
          }
        };
      },
      [userId],
    );
    const read = useCallback(
      () => (userId ? (entries.get(userId)?.snapshot ?? empty) : empty),
      [userId],
    );
    const snapshot = useSyncExternalStore(subscribe, read, () => empty);
    useEffect(() => {
      if (userId) refresh(userId);
    }, [userId]);
    const retry = useCallback(() => {
      if (userId) refresh(userId);
    }, [userId]);
    return { ...snapshot, loading: !!userId && !snapshot.data && !snapshot.error, retry };
  }

  return { useResource, write };
}
