'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  addMemory,
  clearMemories,
  deleteMemory,
  getMemories,
  restoreMemory,
  setMemoryEnabled,
  updateMemory,
  type AgentMemory,
  type AgentMemoryScope,
} from '@/lib/api/agentMemory';
import { APIError } from '@/lib/utils/errors';
import { ChevronIcon, PencilIcon, TrashIcon } from './icons';

const CONTENT_LIMIT = 240;

const PROBLEMS = {
  error: 'settings.memory.error',
  conflict: 'settings.memory.conflict',
  invalid: 'settings.memory.invalid',
  full: 'settings.memory.full',
} as const;
type Problem = keyof typeof PROBLEMS;

/** What went wrong, by the reason the API names, in words the owner can act on. */
function problemOf(cause: unknown): Problem {
  if (cause instanceof APIError) {
    if (cause.code === 'memory_scope_full') return 'full';
    if (cause.statusCode === 409) return 'conflict';
    if (cause.statusCode === 400 || cause.statusCode === 422) return 'invalid';
  }
  return 'error';
}

/** The memories of every project when `repo` is null, otherwise of that repository. */
export function MemoryScopePanel({ repo }: { repo: string | null }) {
  const { t, locale } = useLocale();
  const [scope, setScope] = useState<AgentMemoryScope | null>(null);
  const [pending, setPending] = useState(false);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [draft, setDraft] = useState('');
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null);
  const [removing, setRemoving] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const mounted = useRef(false);
  const request = useRef<AbortController | null>(null);
  const addId = useId();

  /** Run one change, then show the scope as stored; a failure keeps drafts. */
  const run = useCallback(
    async (change?: (signal: AbortSignal) => Promise<unknown>): Promise<boolean> => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      setPending(true);
      setProblem(null);
      let succeeded = true;
      try {
        if (change) await change(controller.signal);
      } catch (cause) {
        if (controller.signal.aborted || !mounted.current) return false;
        setProblem(problemOf(cause));
        succeeded = false;
      }
      try {
        const next = await getMemories(repo, controller.signal);
        if (controller.signal.aborted || !mounted.current) return false;
        setScope(next);
      } catch {
        if (controller.signal.aborted || !mounted.current) return false;
        setProblem((current) => current ?? 'error');
        succeeded = false;
      } finally {
        if (mounted.current && !controller.signal.aborted) setPending(false);
      }
      return succeeded;
    },
    [repo],
  );

  useEffect(() => {
    mounted.current = true;
    void run();
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [run]);

  const formatDate = (value: string) =>
    new Date(value).toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' });

  if (!scope) {
    return problem ? (
      <div
        role="alert"
        className="mt-4 flex flex-wrap items-center gap-3 text-sm text-status-error"
      >
        <span>{t('settings.memory.error')}</span>
        <button
          type="button"
          onClick={() => void run()}
          disabled={pending}
          className="ui-button ui-button-secondary min-h-11"
        >
          {t('settings.memory.retry')}
        </button>
      </div>
    ) : (
      <p role="status" className="mt-4 text-sm text-muted">
        {t('settings.memory.loading')}
      </p>
    );
  }

  const clearTitle = repo
    ? t('settings.projectMemory.confirmTitle', { repo })
    : t('settings.memory.confirmTitle');

  return (
    <div aria-busy={pending}>
      <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1">
        <label className="flex min-h-11 items-center gap-3 text-sm font-medium text-foreground">
          <input
            type="checkbox"
            role="switch"
            checked={scope.enabled}
            onChange={(event) => {
              const enabled = event.target.checked;
              void run((signal) => setMemoryEnabled(repo, enabled, signal));
            }}
            disabled={pending}
            className="h-4 w-4 accent-crimson"
          />
          {t('settings.memory.enabled')}
        </label>
        <button
          type="button"
          aria-expanded={confirmClear}
          onClick={() => setConfirmClear((current) => !current)}
          disabled={pending}
          className="ui-button ui-button-ghost min-h-11 aria-expanded:bg-surface-hover aria-expanded:text-foreground sm:ml-auto"
        >
          {t('settings.memory.clear')}
        </button>
      </div>
      {!scope.enabled ? (
        <p className="mt-1 text-xs leading-relaxed text-muted">{t('settings.memory.paused')}</p>
      ) : null}

      {confirmClear ? (
        <div
          role="group"
          aria-label={clearTitle}
          className="mt-2 rounded-lg border border-border bg-surface-muted p-3"
        >
          <p className="text-sm font-medium text-foreground">{clearTitle}</p>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            {t('settings.memory.confirmDescription')}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={async () => {
                if (await run((signal) => clearMemories(repo, signal))) setConfirmClear(false);
              }}
              disabled={pending}
              className="ui-button ui-button-secondary min-h-11 text-status-error"
            >
              {t('settings.memory.confirmClear')}
            </button>
            <button
              type="button"
              onClick={() => setConfirmClear(false)}
              disabled={pending}
              className="ui-button ui-button-ghost min-h-11"
            >
              {t('settings.memory.cancel')}
            </button>
          </div>
        </div>
      ) : null}

      {problem ? (
        <p role="alert" className="mt-3 text-sm text-status-error">
          {t(PROBLEMS[problem])}
        </p>
      ) : null}

      <form
        className="mt-4 flex flex-col gap-2 sm:flex-row sm:items-start"
        onSubmit={async (event) => {
          event.preventDefault();
          const content = draft.trim();
          if (!content) return;
          if (await run((signal) => addMemory(repo, content, signal))) setDraft('');
        }}
      >
        <label htmlFor={addId} className="sr-only">
          {t('settings.memory.addLabel')}
        </label>
        <input
          id={addId}
          value={draft}
          maxLength={CONTENT_LIMIT}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={t('settings.memory.addPlaceholder')}
          disabled={pending}
          className="ui-input min-h-11 min-w-0 flex-1 rounded-lg"
        />
        <button
          type="submit"
          disabled={pending || !draft.trim()}
          className="ui-button ui-button-primary min-h-11"
        >
          {t('settings.memory.add')}
        </button>
      </form>

      <div className="mt-4 flex items-baseline justify-between gap-3">
        <h3 className="text-sm font-semibold text-foreground">{t('settings.memory.memories')}</h3>
        <span className="text-xs text-muted">
          {t('settings.memory.count', { count: scope.memories.length })}
        </span>
      </div>
      {scope.memories.length ? (
        <ul className="mt-2 max-h-96 divide-y divide-border overflow-y-auto rounded-lg border border-border">
          {scope.memories.map((memory) => (
            <MemoryRow
              key={memory.id}
              memory={memory}
              pending={pending}
              editing={editing?.id === memory.id ? editing.text : null}
              removing={removing === memory.id}
              formatDate={formatDate}
              onEdit={(text) => setEditing(text === null ? null : { id: memory.id, text })}
              onSave={async () => {
                if (!editing) return;
                const text = editing.text.trim();
                if (await run((signal) => updateMemory(memory, text, memory.repo, signal)))
                  setEditing(null);
              }}
              onRemove={(confirming) => setRemoving(confirming ? memory.id : null)}
              onConfirmRemove={async () => {
                if (await run((signal) => deleteMemory(memory, signal))) setRemoving(null);
              }}
            />
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-sm leading-relaxed text-muted">{t('settings.memory.empty')}</p>
      )}

      {scope.archived.length ? (
        <details className="group mt-4 rounded-lg border border-border">
          <summary className="ui-focus-ring flex min-h-11 cursor-pointer list-none items-center gap-2 rounded-lg px-3 text-sm font-medium text-foreground hover:bg-surface-hover [&::-webkit-details-marker]:hidden">
            <ChevronIcon className="h-4 w-4 shrink-0 text-muted transition-transform group-open:rotate-90" />
            {t('settings.memory.archived', { count: scope.archived.length })}
          </summary>
          <div className="border-t border-border p-3">
            <p className="text-xs leading-relaxed text-muted">
              {t('settings.memory.archivedHint')}
            </p>
            <ul className="mt-2 space-y-2">
              {scope.archived.map((memory) => (
                <li key={memory.id} className="flex items-start gap-3">
                  <p className="min-w-0 flex-1 whitespace-pre-wrap break-words text-sm text-muted">
                    {memory.content}
                  </p>
                  <button
                    type="button"
                    onClick={() => void run((signal) => restoreMemory(memory, signal))}
                    disabled={pending}
                    className="ui-button ui-button-secondary min-h-11"
                  >
                    {t('settings.memory.restore')}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </details>
      ) : null}
    </div>
  );
}

function MemoryRow({
  memory,
  pending,
  editing,
  removing,
  formatDate,
  onEdit,
  onSave,
  onRemove,
  onConfirmRemove,
}: {
  memory: AgentMemory;
  pending: boolean;
  editing: string | null;
  removing: boolean;
  formatDate: (value: string) => string;
  onEdit: (text: string | null) => void;
  onSave: () => void;
  onRemove: (confirming: boolean) => void;
  onConfirmRemove: () => void;
}) {
  const { t } = useLocale();
  const editorId = useId();

  return (
    <li className="p-3">
      {editing !== null ? (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            onSave();
          }}
        >
          <label htmlFor={editorId} className="text-xs font-medium text-foreground">
            {t('settings.memory.editLabel')}
          </label>
          <textarea
            id={editorId}
            value={editing}
            maxLength={CONTENT_LIMIT}
            rows={2}
            autoFocus
            onChange={(event) => onEdit(event.target.value)}
            disabled={pending}
            className="ui-input mt-1 block w-full rounded-lg"
          />
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <button
              type="submit"
              disabled={pending || !editing.trim()}
              className="ui-button ui-button-primary min-h-11"
            >
              {t('settings.memory.save')}
            </button>
            <button
              type="button"
              onClick={() => onEdit(null)}
              disabled={pending}
              className="ui-button ui-button-ghost min-h-11"
            >
              {t('settings.memory.cancel')}
            </button>
            <span className="ml-auto text-xs text-muted">
              {t('settings.memory.limit', { count: editing.length })}
            </span>
          </div>
        </form>
      ) : (
        <div className="flex items-start gap-2">
          <div className="min-w-0 flex-1">
            <p className="whitespace-pre-wrap break-words text-sm text-foreground">
              {memory.content}
            </p>
            <p className="mt-1 text-xs text-muted">
              {t(
                memory.origin === 'agent'
                  ? 'settings.memory.originAgent'
                  : 'settings.memory.originManual',
              )}
              {memory.last_used_at
                ? ` · ${t('settings.memory.lastUsed', { date: formatDate(memory.last_used_at) })}`
                : ''}
            </p>
          </div>
          <button
            type="button"
            onClick={() => onEdit(memory.content)}
            disabled={pending}
            aria-label={t('settings.memory.edit')}
            title={t('settings.memory.edit')}
            className="ui-icon-button ui-button-ghost min-h-11 min-w-11"
          >
            <PencilIcon />
          </button>
          <button
            type="button"
            onClick={() => onRemove(!removing)}
            disabled={pending}
            aria-expanded={removing}
            aria-label={t('settings.memory.remove')}
            title={t('settings.memory.remove')}
            className="ui-icon-button ui-button-ghost min-h-11 min-w-11"
          >
            <TrashIcon />
          </button>
        </div>
      )}
      {removing && editing === null ? (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted">{t('settings.memory.removeTitle')}</span>
          <button
            type="button"
            onClick={onConfirmRemove}
            disabled={pending}
            className="ui-button ui-button-secondary min-h-11 text-status-error"
          >
            {t('settings.memory.confirmRemove')}
          </button>
          <button
            type="button"
            onClick={() => onRemove(false)}
            disabled={pending}
            className="ui-button ui-button-ghost min-h-11"
          >
            {t('settings.memory.cancel')}
          </button>
        </div>
      ) : null}
    </li>
  );
}
