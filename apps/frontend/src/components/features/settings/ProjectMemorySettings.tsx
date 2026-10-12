'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAuth, useLocale } from '@/components/providers';
import { getAgentConfig } from '@/lib/api/agents';
import {
  clearProjectMemory,
  consolidateProjectMemory,
  deleteProjectMemoryEntry,
  editProjectMemoryEntry,
  saveProjectMemories,
  getMemoryProjects,
  getProjectMemory,
  updateProjectMemory,
  type ProjectMemoryView,
} from '@/lib/api/projectMemory';
import type { UserMemoryEntry } from '@/lib/api/userMemory';
import { APIError } from '@/lib/utils/errors';
import { UserMemoryEntryCard, type UserMemoryRequestResult } from './UserMemoryEntryCard';
import { MemoryDetails } from './MemoryDetails';

type Operation = 'refresh' | 'toggle' | 'clear' | 'edit' | 'delete' | 'consolidate';

export function ProjectMemorySettings() {
  const { state } = useAuth();
  const userId = state.user?.id;
  return <ProjectMemorySelector key={userId ? `user:${userId}` : 'signed-out'} userId={userId} />;
}

function ProjectMemorySelector({ userId }: { userId: string | undefined }) {
  const { t } = useLocale();
  const [projects, setProjects] = useState<string[]>([]);
  const [repo, setRepo] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(false);

  const loadProjects = useCallback(async () => {
    if (!userId) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(false);
    try {
      const next = await getMemoryProjects(controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setProjects(next);
      setRepo((current) => (next.includes(current) ? current : (next[0] ?? '')));
    } catch {
      if (mounted.current && !controller.signal.aborted) setError(true);
    } finally {
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }, [userId]);

  useEffect(() => {
    mounted.current = true;
    void loadProjects();
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [loadProjects]);

  return (
    <section
      aria-labelledby="settings-project-memory"
      className="ui-card mt-4 bg-surface p-3 sm:p-4"
    >
      <h2 id="settings-project-memory" className="text-base font-semibold text-foreground">
        {t('settings.projectMemory.title')}
      </h2>
      <p className="mt-3 text-sm leading-relaxed text-muted">
        {t('settings.projectMemory.description')}
      </p>
      <p className="mt-2 text-xs leading-relaxed text-muted">
        {t('settings.memory.consolidateDescription')}
      </p>
      {!userId ? (
        <p className="mt-3 text-sm text-muted">{t('settings.projectMemory.signIn')}</p>
      ) : (
        <>
          <div className="mt-4 flex flex-wrap items-end gap-3">
            <label className="flex min-w-0 flex-1 flex-col gap-2 text-sm font-medium text-foreground">
              {t('settings.projectMemory.project')}
              <select
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                disabled={loading || projects.length === 0}
                className="min-h-11 rounded-lg border border-border bg-surface p-2 text-foreground"
              >
                {!projects.length ? (
                  <option value="">{t('settings.projectMemory.select')}</option>
                ) : null}
                {projects.map((project) => (
                  <option key={project} value={project}>
                    {project}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              onClick={() => void loadProjects()}
              disabled={loading}
              className="ui-button ui-button-secondary min-h-11"
            >
              {t('settings.projectMemory.refreshProjects')}
            </button>
          </div>
          {loading ? (
            <p role="status" className="mt-3 text-sm text-muted">
              {t('settings.projectMemory.loadingProjects')}
            </p>
          ) : error ? (
            <p role="alert" className="mt-3 text-sm text-status-error">
              {t('settings.projectMemory.projectsError')}
            </p>
          ) : projects.length === 0 ? (
            <p className="mt-3 text-sm text-muted">{t('settings.projectMemory.noProjects')}</p>
          ) : null}
          {repo ? <ProjectMemoryPanel key={repo} repo={repo} /> : null}
        </>
      )}
    </section>
  );
}

function ProjectMemoryPanel({ repo }: { repo: string }) {
  const { t, locale } = useLocale();
  const [view, setView] = useState<ProjectMemoryView | null>(null);
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
    if (confirmClear) confirmButton.current?.focus();
    else if (wasConfirming.current) clearButton.current?.focus();
    wasConfirming.current = confirmClear;
  }, [confirmClear]);

  const run = useCallback(
    async (
      operation: Operation,
      perform: (signal: AbortSignal) => Promise<ProjectMemoryView>,
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
        if (next.repo !== repo)
          throw new Error('Project memory response does not match the selected project');
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
    [repo],
  );

  useEffect(() => {
    mounted.current = true;
    void run('refresh', (signal) => getProjectMemory(repo, signal));
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [run, repo]);

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
    <div aria-busy={pending !== null}>
      {pending === 'refresh' && !view ? (
        <p role="status" className="mt-3 text-sm text-muted">
          {t('settings.projectMemory.loading')}
        </p>
      ) : null}
      {error ? (
        <div role="alert" className="mt-3 rounded-lg border border-border p-3">
          <p className="text-sm text-status-error">{t('settings.projectMemory.error')}</p>
          <button
            type="button"
            onClick={() => void run('refresh', (signal) => getProjectMemory(repo, signal))}
            disabled={pending !== null}
            className="ui-button ui-button-secondary mt-2 min-h-11"
          >
            {t('settings.memory.retry')}
          </button>
        </div>
      ) : null}
      {view ? (
        <>
          <label className="mt-4 flex min-h-11 items-center gap-3 text-sm font-medium text-foreground">
            <input
              type="checkbox"
              role="switch"
              checked={view.enabled}
              onChange={(event) => {
                const enabled = event.target.checked;
                void run('toggle', (signal) => updateProjectMemory(repo, enabled, signal));
              }}
              disabled={pending !== null}
              className="h-4 w-4 accent-crimson"
            />
            {t('settings.projectMemory.automatic')}
          </label>
          <p aria-live="polite" className="mt-1 text-xs leading-relaxed text-muted">
            {t(view.enabled ? 'settings.projectMemory.enabled' : 'settings.projectMemory.disabled')}
          </p>
          {updatedAt && !Number.isNaN(updatedAt.getTime()) ? (
            <p className="mt-2 text-xs text-muted">
              {t('settings.memory.updated', {
                date: updatedAt.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' }),
              })}
            </p>
          ) : null}
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void run('refresh', (signal) => getProjectMemory(repo, signal))}
              disabled={pending !== null}
              className="ui-button ui-button-secondary min-h-11"
            >
              {t(
                pending === 'refresh'
                  ? 'settings.memory.refreshing'
                  : 'settings.projectMemory.refresh',
              )}
            </button>
            <div className="flex flex-wrap items-end gap-2">
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
              <button
                type="button"
                onClick={() =>
                  void run('consolidate', (signal) =>
                    consolidateProjectMemory(repo, view.revision, consolidationModel, signal),
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
                    : 'settings.projectMemory.consolidate',
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
              {t('settings.projectMemory.clear')}
            </button>
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
              aria-label={t('settings.projectMemory.confirmTitle', { repo })}
              className="mt-3 rounded-lg border border-border bg-surface-muted p-3"
            >
              <p className="text-sm font-medium text-foreground">
                {t('settings.projectMemory.confirmTitle', { repo })}
              </p>
              <p className="mt-1 text-xs leading-relaxed text-muted">
                {t('settings.projectMemory.confirmDescription')}
              </p>
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  ref={confirmButton}
                  type="button"
                  onClick={() => void run('clear', (signal) => clearProjectMemory(repo, signal))}
                  disabled={pending !== null}
                  className="ui-button ui-button-secondary min-h-11 text-status-error"
                >
                  {t(
                    pending === 'clear'
                      ? 'settings.memory.clearing'
                      : 'settings.projectMemory.confirmClear',
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
            scope="project"
            memoriesContent={
              <div>
                <h3
                  ref={memoriesHeading}
                  tabIndex={-1}
                  className="text-sm font-semibold text-foreground outline-none"
                >
                  {t('settings.projectMemory.memories')}
                </h3>
                <p className="mt-2 text-xs leading-relaxed text-muted">
                  {t('settings.memory.entriesDescription')}{' '}
                  {t('settings.projectMemory.bounds', {
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
                                return editProjectMemoryEntry(
                                  repo,
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
                              return saveProjectMemories(repo, view.revision, memories, signal);
                            },
                            false,
                          )
                        }
                        onRemove={() =>
                          run(
                            'delete',
                            (signal) =>
                              deleteProjectMemoryEntry(repo, memory.id, view.revision, signal),
                            false,
                          )
                        }
                        onRefresh={() =>
                          run(
                            'refresh',
                            (signal) => getProjectMemory(repo, signal),
                            false,
                            memory.id,
                          )
                        }
                      />
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-sm text-muted">
                    {t('settings.projectMemory.emptyMemory')}
                  </p>
                )}
              </div>
            }
          />
        </>
      ) : null}
    </div>
  );
}
