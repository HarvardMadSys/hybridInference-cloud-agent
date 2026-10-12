'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAuth, useLocale } from '@/components/providers';
import { getAgentConfig } from '@/lib/api/agents';
import {
  clearUserMemory,
  consolidateUserMemory,
  saveUserMemories,
  deleteUserMemoryEntry,
  editUserMemoryEntry,
  getUserMemory,
  updateUserMemory,
  type UserMemoryEntry,
  type UserMemoryView,
} from '@/lib/api/userMemory';
import { APIError } from '@/lib/utils/errors';
import { MemoryDetails } from './MemoryDetails';
import { UserMemoryEntryCard, type UserMemoryRequestResult } from './UserMemoryEntryCard';

type Operation = 'refresh' | 'toggle' | 'clear' | 'edit' | 'delete' | 'consolidate';

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
  const [editingEntries, setEditingEntries] = useState(() => new Map<string, UserMemoryEntry>());
  const editingEntriesRef = useRef(editingEntries);
  editingEntriesRef.current = editingEntries;
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const clearButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const wasConfirming = useRef(false);
  const memoriesHeading = useRef<HTMLHeadingElement>(null);
  const [removedEntries, setRemovedEntries] = useState(0);
  const [models, setModels] = useState<string[]>([]);
  const [modelsStatus, setModelsStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [modelLoadAttempt, setModelLoadAttempt] = useState(0);
  const [modelChoice, setModelChoice] = useState('');
  const modelSelectId = useId();

  useEffect(() => {
    if (!userId) return;
    let cancelled = false;
    setModelsStatus('loading');
    getAgentConfig()
      .then((config) => {
        if (cancelled) return;
        setModels(config.models);
        setModelsStatus('ready');
      })
      .catch(() => {
        if (!cancelled) setModelsStatus('error');
      });
    return () => {
      cancelled = true;
    };
  }, [userId, modelLoadAttempt]);

  useEffect(() => {
    if (removedEntries) memoriesHeading.current?.focus();
  }, [removedEntries]);

  useEffect(() => {
    if (confirmClear) confirmButton.current?.focus();
    else if (wasConfirming.current) clearButton.current?.focus();
    wasConfirming.current = confirmClear;
  }, [confirmClear]);

  const run = useCallback(
    async (
      operation: Operation,
      perform: (signal: AbortSignal) => Promise<UserMemoryView>,
      showGlobalError = true,
      refreshedEntryId?: string,
    ): Promise<UserMemoryRequestResult> => {
      if (!userId) return 'aborted';
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      setPending(operation);
      setError(false);
      try {
        const next = await perform(controller.signal);
        if (!mounted.current || controller.signal.aborted) return 'aborted';
        setView(next);
        setEditingEntries((current) => {
          if (operation === 'clear') return new Map();
          return new Map(
            [...current].map(([id, snapshot]) => [
              id,
              next.memories.find((memory) => memory.id === id) ?? snapshot,
            ]),
          );
        });
        if (operation === 'clear') setConfirmClear(false);
        if (
          operation === 'delete' ||
          (refreshedEntryId &&
            !next.memories.some((memory) => memory.id === refreshedEntryId) &&
            !editingEntriesRef.current.has(refreshedEntryId))
        )
          setRemovedEntries((count) => count + 1);
        return 'saved';
      } catch (cause) {
        if (!mounted.current || controller.signal.aborted) return 'aborted';
        if (showGlobalError) setError(true);
        if (cause instanceof APIError) {
          if (cause.statusCode === 409) return 'conflict';
          if (cause.statusCode === 400 || cause.statusCode === 422) return 'invalid';
          if (cause.statusCode === 404) return 'missing';
        }
        return 'error';
      } finally {
        if (mounted.current && !controller.signal.aborted) setPending(null);
      }
    },
    [userId],
  );

  useEffect(() => {
    mounted.current = true;
    void run('refresh', getUserMemory);
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [run]);

  const updatedAt = view?.updated_at ? new Date(view.updated_at) : null;
  const savedIds = new Set(view?.memories.map((memory) => memory.id));
  // Keep keyed editor instances, and their local drafts, when a refresh removes
  // their source. These owner-scoped snapshots never become saved memories.
  const shownMemories = [
    ...(view?.memories ?? []),
    ...[...editingEntries.values()].filter((memory) => !savedIds.has(memory.id)),
  ];
  const preferredModel = view?.portrait.preferred_model;
  const consolidationModel = models.includes(modelChoice)
    ? modelChoice
    : preferredModel && models.includes(preferredModel)
      ? preferredModel
      : (models[0] ?? '');

  return (
    <section
      aria-labelledby="settings-user-memory"
      className="ui-card mt-4 bg-surface p-3 sm:p-4"
      aria-busy={pending !== null}
    >
      <h2 id="settings-user-memory" className="text-base font-semibold text-foreground">
        {t('settings.memory.title')}
      </h2>
      <p className="mt-3 text-sm leading-relaxed text-muted">{t('settings.memory.description')}</p>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        {t('settings.memory.consolidateDescription')}
      </p>

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
                onClick={() => void run('refresh', getUserMemory)}
                disabled={pending !== null}
                className="ui-button ui-button-secondary mt-2 min-h-11"
              >
                {t('settings.memory.retry')}
              </button>
            </div>
          ) : null}

          {view ? (
            <>
              <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-4">
                <div className="min-w-0">
                  <label className="flex min-h-11 items-center gap-3 text-sm font-medium text-foreground">
                    <input
                      type="checkbox"
                      role="switch"
                      checked={view.enabled}
                      onChange={(event) => {
                        const enabled = event.target.checked;
                        void run('toggle', (signal) => updateUserMemory(enabled, signal));
                      }}
                      disabled={pending !== null}
                      className="h-4 w-4 accent-crimson"
                    />
                    {t('settings.memory.automatic')}
                  </label>
                  <p aria-live="polite" className="text-xs leading-relaxed text-muted">
                    {t(view.enabled ? 'settings.memory.enabled' : 'settings.memory.disabled')}
                  </p>
                  {updatedAt && !Number.isNaN(updatedAt.getTime()) ? (
                    <p className="text-xs text-muted">
                      {t('settings.memory.updated', {
                        date: updatedAt.toLocaleString(locale, {
                          dateStyle: 'medium',
                          timeStyle: 'short',
                        }),
                      })}
                    </p>
                  ) : null}
                </div>
                <div className="flex flex-wrap items-end gap-2 lg:ml-auto">
                  <button
                    type="button"
                    onClick={() => void run('refresh', getUserMemory)}
                    disabled={pending !== null}
                    className="ui-button ui-button-secondary min-h-11"
                  >
                    {t(
                      pending === 'refresh'
                        ? 'settings.memory.refreshing'
                        : 'settings.memory.refresh',
                    )}
                  </button>
                  <div className="flex flex-wrap items-end gap-2">
                    <div className="min-w-0">
                      <label
                        htmlFor={modelSelectId}
                        className="text-xs font-medium text-foreground"
                      >
                        {t('settings.memory.consolidationModel')}
                      </label>
                      <select
                        id={modelSelectId}
                        value={consolidationModel}
                        onChange={(event) => setModelChoice(event.target.value)}
                        disabled={pending !== null || modelsStatus !== 'ready' || !models.length}
                        className="ui-input mt-1 block min-h-11 max-w-full rounded-lg"
                      >
                        {!models.length ? (
                          <option value="">
                            {t(
                              modelsStatus === 'loading'
                                ? 'settings.memory.modelsLoading'
                                : 'settings.memory.noModels',
                            )}
                          </option>
                        ) : null}
                        {models.map((model) => (
                          <option key={model} value={model}>
                            {model}
                          </option>
                        ))}
                      </select>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        void run('consolidate', (signal) =>
                          consolidateUserMemory(view.revision, consolidationModel, signal),
                        )
                      }
                      disabled={
                        pending !== null ||
                        editingEntries.size > 0 ||
                        !view.memories.length ||
                        modelsStatus !== 'ready' ||
                        !consolidationModel
                      }
                      className="ui-button ui-button-secondary min-h-11"
                    >
                      {t(
                        pending === 'consolidate'
                          ? 'settings.memory.consolidating'
                          : 'settings.memory.consolidate',
                      )}
                    </button>
                  </div>
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
              </div>

              {modelsStatus === 'loading' ? (
                <p role="status" className="mt-3 text-xs text-muted">
                  {t('settings.memory.modelsLoading')}
                </p>
              ) : modelsStatus === 'error' ? (
                <div role="alert" className="mt-3 text-sm text-status-error">
                  <p>{t('settings.memory.modelsError')}</p>
                  <button
                    type="button"
                    onClick={() => setModelLoadAttempt((attempt) => attempt + 1)}
                    className="ui-button ui-button-secondary mt-2 min-h-11"
                  >
                    {t('settings.memory.modelsRetry')}
                  </button>
                </div>
              ) : !models.length ? (
                <div className="mt-3">
                  <p role="status" className="text-xs text-muted">
                    {t('settings.memory.noModels')}
                  </p>
                  <button
                    type="button"
                    onClick={() => setModelLoadAttempt((attempt) => attempt + 1)}
                    className="ui-button ui-button-secondary mt-2 min-h-11"
                  >
                    {t('settings.memory.modelsRetry')}
                  </button>
                </div>
              ) : null}

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
                      onClick={() => void run('clear', clearUserMemory)}
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

              <MemoryDetails
                view={view}
                scope="user"
                memoriesContent={
                  <div>
                    <h3
                      ref={memoriesHeading}
                      tabIndex={-1}
                      className="text-sm font-semibold text-foreground outline-none"
                    >
                      {t('settings.memory.memories')}
                    </h3>
                    <p className="mt-2 text-xs leading-relaxed text-muted">
                      {t('settings.memory.entriesDescription')}{' '}
                      {t('settings.memory.bounds', {
                        count: view.memories.length,
                        characters: view.memories.reduce(
                          (total, memory) => total + memory.content.length,
                          0,
                        ),
                      })}
                    </p>
                    {shownMemories.length ? (
                      <ul className="mt-2 max-h-96 space-y-2 overflow-y-auto">
                        {shownMemories.map((memory, index) => (
                          <UserMemoryEntryCard
                            key={memory.id}
                            memory={memory}
                            number={index + 1}
                            pending={pending !== null}
                            unavailable={!savedIds.has(memory.id)}
                            onEditingChange={(editing) => {
                              setEditingEntries((current) => {
                                const next = new Map(current);
                                if (editing) next.set(memory.id, memory);
                                else next.delete(memory.id);
                                return next;
                              });
                              if (!editing && !savedIds.has(memory.id))
                                setRemovedEntries((count) => count + 1);
                            }}
                            onSave={(content, scope) =>
                              run(
                                'edit',
                                (signal) => {
                                  const savedScope =
                                    memory.scope ?? (memory.source_repo ? 'repo' : 'user');
                                  if (scope === savedScope)
                                    return editUserMemoryEntry(
                                      memory.id,
                                      content,
                                      view.revision,
                                      signal,
                                    );
                                  const memories = view.memories.map((entry) => ({
                                    id: entry.id,
                                    content: entry.id === memory.id ? content : entry.content,
                                    scope:
                                      entry.id === memory.id
                                        ? scope
                                        : (entry.scope ?? (entry.source_repo ? 'repo' : 'user')),
                                  }));
                                  return saveUserMemories(view.revision, memories, signal);
                                },
                                false,
                              )
                            }
                            onRemove={() =>
                              run(
                                'delete',
                                (signal) => deleteUserMemoryEntry(memory.id, view.revision, signal),
                                false,
                              )
                            }
                            onRefresh={() => run('refresh', getUserMemory, false, memory.id)}
                          />
                        ))}
                      </ul>
                    ) : (
                      <p className="mt-2 text-sm text-muted">{t('settings.memory.emptyMemory')}</p>
                    )}
                  </div>
                }
              />
            </>
          ) : null}
        </>
      )}
    </section>
  );
}
