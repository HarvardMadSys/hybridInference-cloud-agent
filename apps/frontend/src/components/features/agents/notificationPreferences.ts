'use client';

import { useCallback, useRef, useSyncExternalStore } from 'react';

export interface NotificationPreferences {
  enabled: boolean;
  questions: boolean;
  turns: boolean;
}

/** Browser permission can change without changing the owner's saved opt-in. */
export const NOTIFICATION_PERMISSION_EVENT = 'agent:notification-permission';

export const DEFAULT_NOTIFICATION_PREFERENCES: NotificationPreferences = Object.freeze({
  enabled: false,
  questions: true,
  turns: true,
});

export function notificationPreferencesStorageKey(userId: string): string {
  return `agents.notifications.${encodeURIComponent(userId)}`;
}

const snapshots = new Map<string, NotificationPreferences>();
const memoryOnly = new Set<string>();
const listeners = new Map<string, Set<() => void>>();

function parse(value: string | null): NotificationPreferences {
  try {
    const record: unknown = JSON.parse(value ?? 'null');
    if (!record || typeof record !== 'object') return DEFAULT_NOTIFICATION_PREFERENCES;
    const preferences = record as Partial<NotificationPreferences>;
    return {
      enabled: preferences.enabled === true,
      questions: typeof preferences.questions === 'boolean' ? preferences.questions : true,
      turns: typeof preferences.turns === 'boolean' ? preferences.turns : true,
    };
  } catch {
    return DEFAULT_NOTIFICATION_PREFERENCES;
  }
}

function read(key: string): NotificationPreferences {
  if (memoryOnly.has(key)) return snapshots.get(key) ?? DEFAULT_NOTIFICATION_PREFERENCES;
  // A failed persistent write falls back to this tab's session. A successful
  // persistent write removes the fallback, so it never shadows a newer choice.
  for (const storageName of ['sessionStorage', 'localStorage'] as const) {
    try {
      const value = window[storageName].getItem(key);
      if (value !== null) return parse(value);
    } catch {
      // Restricted browser storage still leaves the current visit usable.
    }
  }
  return DEFAULT_NOTIFICATION_PREFERENCES;
}

function equal(a: NotificationPreferences, b: NotificationPreferences): boolean {
  return a.enabled === b.enabled && a.questions === b.questions && a.turns === b.turns;
}

function publish(key: string, next: NotificationPreferences): void {
  if (equal(snapshots.get(key) ?? DEFAULT_NOTIFICATION_PREFERENCES, next)) return;
  snapshots.set(key, next);
  for (const listener of listeners.get(key) ?? []) listener();
}

function handleStorage(event: StorageEvent): void {
  for (const key of listeners.keys()) {
    if (event.key !== null && event.key !== key) continue;
    // A different tab's persistent choice supersedes our session fallback.
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // The shared in-memory snapshot still follows the event.
    }
    memoryOnly.delete(key);
    publish(key, parse(event.key === null ? null : event.newValue));
  }
}

function persist(key: string, next: NotificationPreferences): void {
  const value = JSON.stringify(next);
  try {
    window.localStorage.setItem(key, value);
    memoryOnly.delete(key);
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // Persistent storage already contains the choice.
    }
  } catch {
    try {
      window.sessionStorage.setItem(key, value);
      memoryOnly.delete(key);
    } catch {
      memoryOnly.add(key);
      // publish() retains the choice for this visit if both stores are blocked.
    }
  }
}

/** Account-scoped browser preferences, shared by Settings and the app monitor. */
export function useNotificationPreferences(
  userId?: string,
): [NotificationPreferences, (update: Partial<NotificationPreferences>) => void] {
  const key = userId ? notificationPreferencesStorageKey(userId) : null;
  const currentKey = useRef(key);
  currentKey.current = key;
  const getSnapshot = useCallback(() => {
    if (!key || typeof window === 'undefined') return DEFAULT_NOTIFICATION_PREFERENCES;
    if (!snapshots.has(key)) snapshots.set(key, read(key));
    return snapshots.get(key)!;
  }, [key]);
  const subscribe = useCallback(
    (listener: () => void) => {
      if (!key) return () => {};
      if (listeners.size === 0) window.addEventListener('storage', handleStorage);
      let subscribers = listeners.get(key);
      if (!subscribers) {
        subscribers = new Set();
        listeners.set(key, subscribers);
      }
      subscribers.add(listener);
      publish(key, read(key));
      return () => {
        subscribers.delete(listener);
        if (subscribers.size === 0) listeners.delete(key);
        if (listeners.size === 0) window.removeEventListener('storage', handleStorage);
      };
    },
    [key],
  );
  const preferences = useSyncExternalStore(
    subscribe,
    getSnapshot,
    () => DEFAULT_NOTIFICATION_PREFERENCES,
  );
  const updatePreferences = useCallback(
    (update: Partial<NotificationPreferences>) => {
      if (!key || currentKey.current !== key) return;
      const next = { ...read(key), ...update };
      persist(key, next);
      publish(key, next);
    },
    [key],
  );
  return [preferences, updatePreferences];
}
