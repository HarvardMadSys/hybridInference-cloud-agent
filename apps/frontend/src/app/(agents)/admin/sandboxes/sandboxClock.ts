'use client';

import { useSyncExternalStore } from 'react';

const listeners = new Set<() => void>();
let nowMs = 0;
let timer: ReturnType<typeof setInterval> | undefined;

function tick() {
  nowMs = performance.now();
  for (const listener of listeners) listener();
}

function visibilityChanged() {
  clearInterval(timer);
  timer = undefined;
  if (document.visibilityState === 'hidden') return;
  tick();
  timer = setInterval(tick, 1_000);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  if (listeners.size === 1) {
    nowMs = performance.now();
    document.addEventListener('visibilitychange', visibilityChanged);
    visibilityChanged();
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      clearInterval(timer);
      timer = undefined;
      document.removeEventListener('visibilitychange', visibilityChanged);
    }
  };
}

const identity = (value: number) => value;

/** One visible-tab timer; React updates only consumers whose selected value changed. */
export function useSandboxClock(): number;
export function useSandboxClock<T>(select: (now: number) => T): T;
export function useSandboxClock(select: (now: number) => unknown = identity) {
  return useSyncExternalStore(
    subscribe,
    () => select(nowMs),
    () => select(0),
  );
}
