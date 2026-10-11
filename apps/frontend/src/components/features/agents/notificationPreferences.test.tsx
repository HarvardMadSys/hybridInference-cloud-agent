// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_NOTIFICATION_PREFERENCES,
  notificationPreferencesStorageKey,
  useNotificationPreferences,
} from './notificationPreferences';

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('notification preferences', () => {
  it('starts disabled and synchronizes settings with an already mounted monitor', () => {
    const settings = renderHook(() => useNotificationPreferences('sync-user'));
    const monitor = renderHook(() => useNotificationPreferences('sync-user'));
    expect(settings.result.current[0]).toEqual(DEFAULT_NOTIFICATION_PREFERENCES);

    act(() => settings.result.current[1]({ enabled: true, turns: false }));

    expect(monitor.result.current[0]).toEqual({ enabled: true, questions: true, turns: false });
    expect(
      JSON.parse(localStorage.getItem(notificationPreferencesStorageKey('sync-user'))!),
    ).toEqual(monitor.result.current[0]);
    settings.unmount();
    const restored = renderHook(() => useNotificationPreferences('sync-user'));
    expect(restored.result.current[0]).toEqual(monitor.result.current[0]);
  });

  it('isolates accounts, signed-out users, and a delayed callback from an old account', () => {
    const hook = renderHook(({ id }) => useNotificationPreferences(id), {
      initialProps: { id: 'account-a' as string | undefined },
    });
    act(() => hook.result.current[1]({ enabled: true }));
    const previousUpdate = hook.result.current[1];
    hook.rerender({ id: 'account-b' });
    expect(hook.result.current[0].enabled).toBe(false);
    act(() => previousUpdate({ turns: false }));
    expect(localStorage.getItem(notificationPreferencesStorageKey('account-b'))).toBeNull();
    expect(
      JSON.parse(localStorage.getItem(notificationPreferencesStorageKey('account-a'))!).turns,
    ).toBe(true);
    hook.rerender({ id: undefined });
    act(() => hook.result.current[1]({ enabled: true }));
    expect(hook.result.current[0]).toEqual(DEFAULT_NOTIFICATION_PREFERENCES);
  });

  it('receives another tab’s account-specific changes and clears', () => {
    const hook = renderHook(() => useNotificationPreferences('cross-tab'));
    const key = notificationPreferencesStorageKey('cross-tab');
    const next = JSON.stringify({ enabled: true, questions: false, turns: true });
    act(() => window.dispatchEvent(new StorageEvent('storage', { key, newValue: next })));
    expect(hook.result.current[0]).toEqual({ enabled: true, questions: false, turns: true });
    act(() =>
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: notificationPreferencesStorageKey('another-user'),
          newValue: null,
        }),
      ),
    );
    expect(hook.result.current[0].enabled).toBe(true);
    act(() => window.dispatchEvent(new StorageEvent('storage', { key, newValue: null })));
    expect(hook.result.current[0]).toEqual(DEFAULT_NOTIFICATION_PREFERENCES);
  });

  it('keeps choices in the session if persistent storage cannot be written', () => {
    const setItem = Storage.prototype.setItem;
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (this === localStorage) throw new Error('Persistent storage blocked');
      setItem.call(this, key, value);
    });
    const hook = renderHook(() => useNotificationPreferences('session-fallback'));
    act(() => hook.result.current[1]({ enabled: true, questions: false }));
    expect(
      sessionStorage.getItem(notificationPreferencesStorageKey('session-fallback')),
    ).not.toBeNull();
    hook.unmount();
    const restored = renderHook(() => useNotificationPreferences('session-fallback'));
    expect(restored.result.current[0]).toEqual({ enabled: true, questions: false, turns: true });
  });

  it('shares choices for the current visit if all browser storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('Storage blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('Storage blocked');
    });
    const hook = renderHook(() => useNotificationPreferences('memory-fallback'));
    const observer = renderHook(() => useNotificationPreferences('memory-fallback'));
    act(() => hook.result.current[1]({ enabled: true }));
    expect(observer.result.current[0].enabled).toBe(true);
    hook.unmount();
    const restored = renderHook(() => useNotificationPreferences('memory-fallback'));
    expect(restored.result.current[0].enabled).toBe(true);
  });

  it('does not opt in on malformed storage', () => {
    localStorage.setItem(notificationPreferencesStorageKey('invalid-user'), '{broken');
    const hook = renderHook(() => useNotificationPreferences('invalid-user'));
    expect(hook.result.current[0]).toEqual(DEFAULT_NOTIFICATION_PREFERENCES);
  });
});
