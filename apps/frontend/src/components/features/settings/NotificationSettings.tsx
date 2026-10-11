'use client';

import { useEffect, useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import {
  NOTIFICATION_PERMISSION_EVENT,
  useNotificationPreferences,
} from '@/components/features/agents/notificationPreferences';

function supported(): boolean {
  return typeof Notification === 'function' && typeof Notification.requestPermission === 'function';
}

export function NotificationSettings() {
  const { state } = useAuth();
  const userId = state.user?.id;
  const t = useT();
  const [preferences, updatePreferences] = useNotificationPreferences(userId);
  const [permission, setPermission] = useState<NotificationPermission | null>(null);
  const [requesting, setRequesting] = useState(false);
  const [error, setError] = useState(false);
  const currentUser = useRef(userId);
  currentUser.current = userId;
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    const refresh = () => setPermission(supported() ? Notification.permission : null);
    refresh();
    window.addEventListener('focus', refresh);
    return () => {
      mounted.current = false;
      window.removeEventListener('focus', refresh);
    };
  }, []);

  useEffect(() => {
    setError(false);
    setRequesting(false);
  }, [userId]);

  async function enable() {
    if (!supported() || !userId || requesting) return;
    setRequesting(true);
    setError(false);
    try {
      // Browsers require the permission prompt to originate in a user click.
      const next =
        Notification.permission === 'granted' ? 'granted' : await Notification.requestPermission();
      if (!mounted.current || currentUser.current !== userId) return;
      setPermission(next);
      updatePreferences({ enabled: next === 'granted' });
      window.dispatchEvent(new Event(NOTIFICATION_PERMISSION_EVENT));
    } catch {
      if (mounted.current && currentUser.current === userId) setError(true);
    } finally {
      if (mounted.current && currentUser.current === userId) setRequesting(false);
    }
  }

  const enabled = preferences.enabled && permission === 'granted';
  const guidance = error
    ? 'settings.notifications.error'
    : permission === null
      ? 'settings.notifications.unsupported'
      : permission === 'denied'
        ? 'settings.notifications.denied'
        : enabled
          ? 'settings.notifications.enabled'
          : 'settings.notifications.off';

  return (
    <section aria-labelledby="settings-notifications" className="mt-6">
      <h3
        id="settings-notifications"
        className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500"
      >
        {t('settings.notifications.title')}
      </h3>
      <p className="mt-3 text-[13px] leading-relaxed text-gray-600 dark:text-gray-400">
        {t('settings.notifications.description')}
      </p>
      <p aria-live="polite" className="mt-2 text-[12px] text-gray-500 dark:text-gray-400">
        {t(guidance)}
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        {preferences.enabled ? (
          <button
            type="button"
            onClick={() => updatePreferences({ enabled: false })}
            className="rounded-lg border border-gray-200 px-3 py-1.5 text-[13px] font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5"
          >
            {t('settings.notifications.disable')}
          </button>
        ) : null}
        {!enabled ? (
          <button
            type="button"
            onClick={() => void enable()}
            disabled={!userId || permission === null || permission === 'denied' || requesting}
            className="rounded-lg bg-crimson px-3 py-1.5 text-[13px] font-medium text-white transition-colors hover:bg-crimson-dark focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t(requesting ? 'settings.notifications.enabling' : 'settings.notifications.enable')}
          </button>
        ) : null}
      </div>
      <fieldset className="mt-3 space-y-2">
        <legend className="sr-only">{t('settings.notifications.events')}</legend>
        <label className="flex items-center gap-2 text-[13px] text-gray-800 dark:text-gray-200">
          <input
            type="checkbox"
            checked={preferences.questions}
            onChange={(event) => updatePreferences({ questions: event.target.checked })}
            disabled={!userId}
            className="h-4 w-4 accent-crimson"
          />
          {t('settings.notifications.questions')}
        </label>
        <label className="flex items-center gap-2 text-[13px] text-gray-800 dark:text-gray-200">
          <input
            type="checkbox"
            checked={preferences.turns}
            onChange={(event) => updatePreferences({ turns: event.target.checked })}
            disabled={!userId}
            className="h-4 w-4 accent-crimson"
          />
          {t('settings.notifications.turns')}
        </label>
      </fieldset>
    </section>
  );
}
