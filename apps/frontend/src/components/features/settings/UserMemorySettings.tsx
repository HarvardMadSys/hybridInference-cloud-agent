'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { useAuth, useLocale } from '@/components/providers';
import {
  clearUserMemory,
  getUserMemory,
  updateUserMemory,
  type UserMemoryView,
} from '@/lib/api/userMemory';

type Operation = 'refresh' | 'toggle' | 'clear';

export function UserMemorySettings() {
  const { state } = useAuth();
  const userId = state.user?.id;
  // A new account gets a fresh panel immediately, including before effects run.
  return <UserMemoryPanel key={userId ? `user:${userId}` : 'signed-out'} userId={userId} />;
}

function UserMemoryPanel({ userId }: { userId: string | undefined }) {
  const { t, locale } = useLocale();
  const [view, setView] = useState<UserMemoryView | null>(null);
  const [pending, setPending] = useState<Operation | null>(null);
  const [error, setError] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const clearButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);

  useEffect(() => {
    if (confirmClear) confirmButton.current?.focus();
    else if (wasConfirming.current) clearButton.current?.focus();
    wasConfirming.current = confirmClear;
  }, [confirmClear]);

  const run = useCallback(
    async (operation: Operation, enabled?: boolean) => {
      if (!userId) return;
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      setPending(operation);
      setError(false);
      try {
        const next =
          operation === 'toggle'
            ? await updateUserMemory(enabled!, controller.signal)
            : operation === 'clear'
              ? await clearUserMemory(controller.signal)
              : await getUserMemory(controller.signal);
        if (!mounted.current || controller.signal.aborted) return;
        setView(next);
        if (operation === 'clear') setConfirmClear(false);
      } catch {
        if (mounted.current && !controller.signal.aborted) setError(true);
      } finally {
        if (mounted.current && !controller.signal.aborted) setPending(null);
      }
    },
    [userId],
  );

  useEffect(() => {
    mounted.current = true;
    void run('refresh');
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [run]);

  const hasPortrait =
    view &&
    (view.portrait.summary ||
      view.portrait.projects.length ||
      view.portrait.focus_areas.length ||
      view.portrait.preferred_runtime ||
      view.portrait.preferred_model);
  const updatedAt = view?.updated_at ? new Date(view.updated_at) : null;

  return (
    <section
      aria-labelledby="settings-user-memory"
      className="ui-card mt-6 rounded-2xl bg-surface p-4 sm:p-6"
      aria-busy={pending !== null}
    >
      <h2 id="settings-user-memory" className="text-base font-semibold text-foreground">
        {t('settings.memory.title')}
      </h2>
      <p className="mt-3 text-sm leading-relaxed text-muted">{t('settings.memory.description')}</p>

      {!userId ? (
        <p className="mt-3 text-sm text-muted">{t('settings.memory.signIn')}</p>
      ) : (
        <>
          {pending === 'refresh' && !view ? (
            <p role="status" className="mt-3 text-sm text-muted">
              {t('settings.memory.loading')}
            </p>
          ) : null}
          {error ? (
            <div role="alert" className="mt-3 rounded-lg border border-border p-3">
              <p className="text-sm text-status-error">{t('settings.memory.error')}</p>
              <button
                type="button"
                onClick={() => void run('refresh')}
                disabled={pending !== null}
                className="ui-button ui-button-secondary mt-2 min-h-11"
              >
                {t('settings.memory.retry')}
              </button>
            </div>
          ) : null}

          {view ? (
            <>
              <div className="mt-4 flex items-center gap-4">
                {/* The API generates this illustration as a data URL for the account. */}
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={view.avatar.data_url}
                  alt={t('settings.memory.avatarAlt')}
                  width={80}
                  height={80}
                  className="h-20 w-20 shrink-0 rounded-full border border-border object-cover"
                />
                <div>
                  <h3 className="text-sm font-semibold text-foreground">
                    {t('settings.memory.avatar')}
                  </h3>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    {t('settings.memory.avatarDescription')}
                  </p>
                </div>
              </div>
              <label className="mt-4 flex min-h-11 items-center gap-3 text-sm font-medium text-foreground">
                <input
                  type="checkbox"
                  role="switch"
                  checked={view.enabled}
                  onChange={(event) => void run('toggle', event.target.checked)}
                  disabled={pending !== null}
                  className="h-4 w-4 accent-crimson"
                />
                {t('settings.memory.automatic')}
              </label>
              <p aria-live="polite" className="mt-1 text-xs leading-relaxed text-muted">
                {t(view.enabled ? 'settings.memory.enabled' : 'settings.memory.disabled')}
              </p>
              {updatedAt && !Number.isNaN(updatedAt.getTime()) ? (
                <p className="mt-2 text-xs text-muted">
                  {t('settings.memory.updated', {
                    date: updatedAt.toLocaleString(locale, {
                      dateStyle: 'medium',
                      timeStyle: 'short',
                    }),
                  })}
                </p>
              ) : null}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  type="button"
                  onClick={() => void run('refresh')}
                  disabled={pending !== null}
                  className="ui-button ui-button-secondary min-h-11"
                >
                  {t(
                    pending === 'refresh'
                      ? 'settings.memory.refreshing'
                      : 'settings.memory.refresh',
                  )}
                </button>
                <button
                  ref={clearButton}
                  type="button"
                  onClick={() => setConfirmClear(true)}
                  disabled={pending !== null || confirmClear}
                  className="ui-button ui-button-secondary min-h-11"
                >
                  {t('settings.memory.clear')}
                </button>
              </div>

              {confirmClear ? (
                <div
                  role="group"
                  aria-label={t('settings.memory.confirmTitle')}
                  className="mt-3 rounded-lg border border-border bg-surface-muted p-3"
                >
                  <p className="text-sm font-medium text-foreground">
                    {t('settings.memory.confirmTitle')}
                  </p>
                  <p className="mt-1 text-xs leading-relaxed text-muted">
                    {t('settings.memory.confirmDescription')}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button
                      ref={confirmButton}
                      type="button"
                      onClick={() => void run('clear')}
                      disabled={pending !== null}
                      className="ui-button ui-button-secondary min-h-11 text-status-error"
                    >
                      {t(
                        pending === 'clear'
                          ? 'settings.memory.clearing'
                          : 'settings.memory.confirmClear',
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() => setConfirmClear(false)}
                      disabled={pending !== null}
                      className="ui-button ui-button-secondary min-h-11"
                    >
                      {t('settings.memory.cancel')}
                    </button>
                  </div>
                </div>
              ) : null}

              <div className="mt-5 grid gap-5 lg:grid-cols-3">
                <div>
                  <h3 className="text-sm font-semibold text-foreground">
                    {t('settings.memory.portrait')}
                  </h3>
                  {hasPortrait ? (
                    <>
                      {view.portrait.summary ? (
                        <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-muted">
                          {view.portrait.summary}
                        </p>
                      ) : null}
                      <dl className="mt-2 space-y-2 text-sm">
                        {view.portrait.projects.length ? (
                          <div>
                            <dt className="font-medium text-foreground">
                              {t('settings.memory.projects')}
                            </dt>
                            <dd className="break-words text-muted">
                              {view.portrait.projects.join(', ')}
                            </dd>
                          </div>
                        ) : null}
                        {view.portrait.focus_areas.length ? (
                          <div>
                            <dt className="font-medium text-foreground">
                              {t('settings.memory.focusAreas')}
                            </dt>
                            <dd className="break-words text-muted">
                              {view.portrait.focus_areas.join(', ')}
                            </dd>
                          </div>
                        ) : null}
                        {view.portrait.preferred_runtime ? (
                          <div>
                            <dt className="font-medium text-foreground">
                              {t('settings.memory.runtime')}
                            </dt>
                            <dd className="break-words text-muted">
                              {view.portrait.preferred_runtime}
                            </dd>
                          </div>
                        ) : null}
                        {view.portrait.preferred_model ? (
                          <div>
                            <dt className="font-medium text-foreground">
                              {t('settings.memory.model')}
                            </dt>
                            <dd className="break-words text-muted">
                              {view.portrait.preferred_model}
                            </dd>
                          </div>
                        ) : null}
                      </dl>
                    </>
                  ) : (
                    <p className="mt-2 text-sm leading-relaxed text-muted">
                      {t('settings.memory.emptyPortrait')}
                    </p>
                  )}
                </div>

                <div>
                  <h3 className="text-sm font-semibold text-foreground">
                    {t('settings.memory.memories')}
                  </h3>
                  {view.memories.length ? (
                    <ul className="mt-2 space-y-2">
                      {view.memories.map((memory) => (
                        <li key={memory.id} className="rounded-lg border border-border p-3">
                          <span className="text-xs font-medium text-muted">
                            {t(
                              memory.kind === 'preference'
                                ? 'settings.memory.preference'
                                : 'settings.memory.task',
                            )}
                          </span>
                          <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                            {memory.content}
                          </p>
                          <Link
                            href={`/${encodeURIComponent(memory.source_job_id)}`}
                            className="mt-2 inline-flex min-h-8 items-center text-xs font-medium text-accent hover:underline"
                          >
                            {t('settings.memory.source')}
                          </Link>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 text-sm text-muted">{t('settings.memory.emptyMemory')}</p>
                  )}
                </div>

                <div>
                  <h3 className="text-sm font-semibold text-foreground">
                    {t('settings.memory.suggestions')}
                  </h3>
                  {view.suggestions.length ? (
                    <ul className="mt-2 space-y-2">
                      {view.suggestions.map((suggestion) => (
                        <li key={suggestion.id} className="rounded-lg bg-surface-muted p-3">
                          <p className="break-words text-sm font-medium text-foreground">
                            {suggestion.title}
                          </p>
                          <p className="mt-1 whitespace-pre-wrap break-words text-xs leading-relaxed text-muted">
                            {suggestion.reason}
                          </p>
                          <p className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
                            {suggestion.action}
                          </p>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-2 text-sm leading-relaxed text-muted">
                      {t('settings.memory.emptySuggestions')}
                    </p>
                  )}
                </div>
              </div>
            </>
          ) : null}
        </>
      )}
    </section>
  );
}
