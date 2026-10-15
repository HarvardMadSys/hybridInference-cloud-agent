'use client';

import { useCallback, useRef, useSyncExternalStore } from 'react';
import {
  DEFAULT_KEYBOARD_SHORTCUTS,
  parseKeyboardShortcuts,
  type KeyboardShortcuts,
} from '@/lib/keyboardShortcuts';

export function keyboardShortcutsStorageKey(userId: string): string {
  return `agents.keyboard-shortcuts.${encodeURIComponent(userId)}`;
}

const snapshots = new Map<string, KeyboardShortcuts>();
const memoryOnly = new Set<string>();
const listeners = new Map<string, Set<() => void>>();

function read(key: string): KeyboardShortcuts {
  if (memoryOnly.has(key)) return snapshots.get(key) ?? DEFAULT_KEYBOARD_SHORTCUTS;
  for (const storageName of ['sessionStorage', 'localStorage'] as const) {
    try {
      const value = window[storageName].getItem(key);
      if (value !== null) return parseKeyboardShortcuts(value);
    } catch {
      // Restricted storage still leaves the current visit usable.
    }
  }
  return DEFAULT_KEYBOARD_SHORTCUTS;
}

function publish(key: string, next: KeyboardShortcuts): void {
  if (JSON.stringify(snapshots.get(key)) === JSON.stringify(next)) return;
  snapshots.set(key, next);
  for (const listener of listeners.get(key) ?? []) listener();
}

function handleStorage(event: StorageEvent): void {
  // Session fallback is local to a tab; only persistent changes are shared.
  if (event.storageArea && event.storageArea !== window.localStorage) return;
  for (const key of listeners.keys()) {
    if (event.key !== null && event.key !== key) continue;
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // The in-memory snapshot can still follow the other tab.
    }
    memoryOnly.delete(key);
    publish(key, parseKeyboardShortcuts(event.key === null ? null : event.newValue));
  }
}

function persist(key: string, next: KeyboardShortcuts): void {
  const value = JSON.stringify(next);
  try {
    window.localStorage.setItem(key, value);
    memoryOnly.delete(key);
    try {
      window.sessionStorage.removeItem(key);
    } catch {
      // The persistent preference was saved.
    }
  } catch {
    try {
      window.sessionStorage.setItem(key, value);
      memoryOnly.delete(key);
    } catch {
      memoryOnly.add(key);
    }
  }
}

/** Account-scoped browser preferences shared by Settings and active handlers. */
export function useKeyboardShortcuts(
  userId?: string,
): [KeyboardShortcuts, (next: KeyboardShortcuts) => void] {
  const key = userId ? keyboardShortcutsStorageKey(userId) : null;
  const currentKey = useRef(key);
  currentKey.current = key;
  const getSnapshot = useCallback(() => {
    if (!key || typeof window === 'undefined') return DEFAULT_KEYBOARD_SHORTCUTS;
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
    () => DEFAULT_KEYBOARD_SHORTCUTS,
  );
  const update = useCallback(
    (next: KeyboardShortcuts) => {
      if (!key || currentKey.current !== key) return;
      const validated = parseKeyboardShortcuts(JSON.stringify(next));
      persist(key, validated);
      publish(key, validated);
    },
    [key],
  );
  return [preferences, update];
}
