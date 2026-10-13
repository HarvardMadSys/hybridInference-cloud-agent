'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import { getAgentConfig } from '@/lib/api/agents';
import type {
  UserMemoryEdit,
  UserMemoryEntry,
  UserMemorySuggestion,
  UserPortrait,
} from '@/lib/api/userMemory';
import { APIError } from '@/lib/utils/errors';
import { MemoryDetails } from './MemoryDetails';
import {
  UserMemoryEntryCard,
  entryScope,
  type UserMemoryRequestResult,
} from './UserMemoryEntryCard';

/** The fields user memory and project memory share. */
export interface MemoryView {
  enabled: boolean;
  revision: number;
  updated_at: string | null;
  portrait: UserPortrait;
  memories: UserMemoryEntry[];
  suggestions: UserMemorySuggestion[];
}

/** One memory's endpoints, already bound to its owner and, for a project, its repository. */
export interface MemoryApi<TView extends MemoryView = MemoryView> {
  read: (signal: AbortSignal) => Promise<TView>;
  toggle: (enabled: boolean, signal: AbortSignal) => Promise<TView>;
  clear: (signal: AbortSignal) => Promise<TView>;
  edit: (id: string, content: string, revision: number, signal: AbortSignal) => Promise<TView>;
  remove: (id: string, revision: number, signal: AbortSignal) => Promise<TView>;
  save: (revision: number, memories: UserMemoryEdit[], signal: AbortSignal) => Promise<TView>;
  consolidate: (revision: number, model: string, signal: AbortSignal) => Promise<TView>;
}

type Operation = 'refresh' | 'toggle' | 'clear' | 'edit' | 'delete' | 'consolidate';
type Dialog = 'clear' | 'consolidate' | null;

/** User memory when `repo` is absent, otherwise that repository's project memory. */
export function MemoryPanel<TView extends MemoryView>({
  api,
  repo,
  onViewChange,
}: {
  api: MemoryApi<TView>;
  repo?: string;
  onViewChange?: (view: TView | null) => void;
}) {
  const { t, locale } = useLocale();
  const [view, setView] = useState<TView | null>(null);
  const [pending, setPending] = useState<Operation | null>(null);
  const [error, setError] = useState(false);
  const [dialog, setDialog] = useState<Dialog>(null);
  const [editingEntries, setEditingEntries] = useState(() => new Map<string, UserMemoryEntry>());
  const editingEntriesRef = useRef(editingEntries);
  editingEntriesRef.current = editingEntries;
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const clearButton = useRef<HTMLButtonElement>(null);
  const consolidateButton = useRef<HTMLButtonElement>(null);
  const confirmClearButton = useRef<HTMLButtonElement>(null);
  const consolidateGroup = useRef<HTMLDivElement>(null);
  const previousDialog = useRef<Dialog>(null);
  const memoriesHeading = useRef<HTMLHeadingElement>(null);
  const [removedEntries, setRemovedEntries] = useState(0);
  const [models, setModels] = useState<string[]>([]);
  const [modelsStatus, setModelsStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  // The model catalog loads when consolidation is first opened; 0 means not yet.
  const [modelLoadAttempt, setModelLoadAttempt] = useState(0);
  const [modelChoice, setModelChoice] = useState('');
  const modelSelectId = useId();

  useEffect(() => {
    onViewChange?.(view);
  }, [view, onViewChange]);

  useEffect(() => {
    if (!modelLoadAttempt) return;
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
  }, [modelLoadAttempt]);

  useEffect(() => {
    if (removedEntries) memoriesHeading.current?.focus();
  }, [removedEntries]);

  useEffect(() => {
    if (dialog === 'clear') confirmClearButton.current?.focus();
    else if (dialog === 'consolidate') consolidateGroup.current?.focus();
    else if (previousDialog.current === 'clear') clearButton.current?.focus();
    else if (previousDialog.current === 'consolidate') consolidateButton.current?.focus();
    previousDialog.current = dialog;
  }, [dialog]);

  const run = useCallback(
    async (
      operation: Operation,
      perform: (signal: AbortSignal) => Promise<TView>,
      showGlobalError = true,
      refreshedEntryId?: string,
    ): Promise<UserMemoryRequestResult> => {
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
        if (operation === 'clear' || operation === 'consolidate') setDialog(null);
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
    [],
  );

  useEffect(() => {
    mounted.current = true;
    void run('refresh', api.read);
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [run, api]);

  function toggleDialog(next: Exclude<Dialog, null>) {
    if (next === 'consolidate' && !modelLoadAttempt) setModelLoadAttempt(1);
    setDialog((current) => (current === next ? null : next));
  }

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
  const clearTitle = repo
    ? t('settings.projectMemory.confirmTitle', { repo })
    : t('settings.memory.confirmTitle');

  return (
    <div aria-busy={pending !== null}>
      {pending === 'refresh' && !view ? (
        <p role="status" className="mt-4 text-sm text-muted">
          {t('settings.memory.loading')}
        </p>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-status-error"
        >
          <span>{t('settings.memory.error')}</span>
          <button
            type="button"
            onClick={() => void run('refresh', api.read)}
            disabled={pending !== null}
            className="ui-button ui-button-secondary min-h-11"
          >
            {t('settings.memory.retry')}
          </button>
        </div>
      ) : null}

      {view ? (
        <>
          <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1">
            <label className="flex min-h-11 items-center gap-3 text-sm font-medium text-foreground">
              <input
                type="checkbox"
                role="switch"
                checked={view.enabled}
                onChange={(event) => {
                  const enabled = event.target.checked;
                  void run('toggle', (signal) => api.toggle(enabled, signal));
                }}
                disabled={pending !== null}
                className="h-4 w-4 accent-crimson"
              />
              {t('settings.memory.automatic')}
            </label>
            {updatedAt && !Number.isNaN(updatedAt.getTime()) ? (
              <span className="text-xs text-muted">
                {t('settings.memory.updated', {
                  date: updatedAt.toLocaleString(locale, {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  }),
                })}
              </span>
            ) : null}
            <div className="flex flex-wrap gap-1 sm:ml-auto">
              <button
                type="button"
                onClick={() => void run('refresh', api.read)}
                disabled={pending !== null}
                className="ui-button ui-button-ghost min-h-11"
              >
                {t(
                  pending === 'refresh' ? 'settings.memory.refreshing' : 'settings.memory.refresh',
                )}
              </button>
              <button
                ref={consolidateButton}
                type="button"
                aria-expanded={dialog === 'consolidate'}
                onClick={() => toggleDialog('consolidate')}
                disabled={pending !== null || !view.memories.length}
                className="ui-button ui-button-ghost min-h-11 aria-expanded:bg-surface-hover aria-expanded:text-foreground"
              >
                {t('settings.memory.consolidate')}
              </button>
              <button
                ref={clearButton}
                type="button"
                aria-expanded={dialog === 'clear'}
                onClick={() => toggleDialog('clear')}
                disabled={pending !== null}
                className="ui-button ui-button-ghost min-h-11 aria-expanded:bg-surface-hover aria-expanded:text-foreground"
              >
                {t('settings.memory.clear')}
              </button>
            </div>
          </div>

          {dialog === 'consolidate' ? (
            <div
              ref={consolidateGroup}
              role="group"
              aria-label={t('settings.memory.consolidateTitle')}
              tabIndex={-1}
              className="mt-2 rounded-lg border border-border bg-surface-muted p-3 outline-none"
            >
              <p className="text-xs leading-relaxed text-muted">
                {t('settings.memory.consolidateDescription')}
              </p>
              {modelsStatus === 'error' ? (
                <p role="alert" className="mt-2 text-sm text-status-error">
                  {t('settings.memory.modelsError')}
                </p>
              ) : null}
              <div className="mt-3 flex flex-wrap items-end gap-2">
                <div className="min-w-0">
                  <label htmlFor={modelSelectId} className="text-xs font-medium text-foreground">
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
                {modelsStatus === 'error' || (modelsStatus === 'ready' && !models.length) ? (
                  <button
                    type="button"
                    onClick={() => setModelLoadAttempt((attempt) => attempt + 1)}
                    className="ui-button ui-button-secondary min-h-11"
                  >
                    {t('settings.memory.modelsRetry')}
                  </button>
                ) : null}
                <button
                  type="button"
                  onClick={() =>
                    void run('consolidate', (signal) =>
                      api.consolidate(view.revision, consolidationModel, signal),
                    )
                  }
                  disabled={
                    pending !== null ||
                    editingEntries.size > 0 ||
                    !view.memories.length ||
                    modelsStatus !== 'ready' ||
                    !consolidationModel
                  }
                  className="ui-button ui-button-primary min-h-11"
                >
                  {t(
                    pending === 'consolidate'
                      ? 'settings.memory.consolidating'
                      : 'settings.memory.consolidateNow',
                  )}
                </button>
                <button
                  type="button"
                  onClick={() => setDialog(null)}
                  disabled={pending !== null}
                  className="ui-button ui-button-ghost min-h-11"
                >
                  {t('settings.memory.cancel')}
                </button>
              </div>
            </div>
          ) : null}

          {dialog === 'clear' ? (
            <div
              role="group"
              aria-label={clearTitle}
              className="mt-2 rounded-lg border border-border bg-surface-muted p-3"
            >
              <p className="text-sm font-medium text-foreground">{clearTitle}</p>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                {t(
                  repo
                    ? 'settings.projectMemory.confirmDescription'
                    : 'settings.memory.confirmDescription',
                )}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  ref={confirmClearButton}
                  type="button"
                  onClick={() => void run('clear', api.clear)}
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
                  onClick={() => setDialog(null)}
                  disabled={pending !== null}
                  className="ui-button ui-button-ghost min-h-11"
                >
                  {t('settings.memory.cancel')}
                </button>
              </div>
            </div>
          ) : null}

          <div className="mt-4">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
              <h3
                ref={memoriesHeading}
                tabIndex={-1}
                className="text-sm font-semibold text-foreground outline-none"
              >
                {t('settings.memory.memories')}
              </h3>
              <span className="text-xs text-muted">
                {t(repo ? 'settings.projectMemory.bounds' : 'settings.memory.bounds', {
                  count: view.memories.length,
                  characters: view.memories.reduce(
                    (total, memory) => total + memory.content.length,
                    0,
                  ),
                })}
              </span>
            </div>
            {shownMemories.length ? (
              <ul className="mt-2 max-h-96 divide-y divide-border overflow-y-auto rounded-lg border border-border">
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
                          if (scope === entryScope(memory))
                            return api.edit(memory.id, content, view.revision, signal);
                          const memories = view.memories.map((entry) => ({
                            id: entry.id,
                            content: entry.id === memory.id ? content : entry.content,
                            scope: entry.id === memory.id ? scope : entryScope(entry),
                          }));
                          return api.save(view.revision, memories, signal);
                        },
                        false,
                      )
                    }
                    onRemove={() =>
                      run('delete', (signal) => api.remove(memory.id, view.revision, signal), false)
                    }
                    onRefresh={() => run('refresh', api.read, false, memory.id)}
                  />
                ))}
              </ul>
            ) : (
              <p className="mt-2 text-sm text-muted">{t('settings.memory.emptyMemory')}</p>
            )}
          </div>

          <MemoryDetails view={view} project={repo !== undefined} />
        </>
      ) : null}
    </div>
  );
}
