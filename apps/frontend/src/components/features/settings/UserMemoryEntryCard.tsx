'use client';

import { useEffect, useId, useRef, useState } from 'react';
import Link from 'next/link';
import { useLocale } from '@/components/providers';
import type { UserMemoryEntry } from '@/lib/api/userMemory';

export type UserMemoryRequestResult =
  | 'saved'
  | 'conflict'
  | 'invalid'
  | 'missing'
  | 'error'
  | 'aborted';
type EntryError = Exclude<UserMemoryRequestResult, 'saved' | 'aborted'>;

const ERROR_KEYS = {
  conflict: 'settings.memory.entryConflict',
  invalid: 'settings.memory.entryInvalid',
  missing: 'settings.memory.entryMissing',
  error: 'settings.memory.entryError',
} as const;

export function UserMemoryEntryCard({
  memory,
  number,
  pending,
  unavailable,
  onEditingChange,
  onSave,
  onRemove,
  onRefresh,
}: {
  memory: UserMemoryEntry;
  number: number;
  pending: boolean;
  unavailable: boolean;
  onEditingChange: (editing: boolean) => void;
  onSave: (content: string, scope: 'repo' | 'user') => Promise<UserMemoryRequestResult>;
  onRemove: () => Promise<UserMemoryRequestResult>;
  onRefresh: () => Promise<UserMemoryRequestResult>;
}) {
  const { t, locale } = useLocale();
  const [editing, setEditing] = useState(false);
  const [draftScope, setDraftScope] = useState<'repo' | 'user'>(
    memory.scope ?? (memory.source_repo ? 'repo' : 'user'),
  );
  const [draft, setDraft] = useState(memory.content);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const [error, setError] = useState<EntryError | null>(null);
  const editorId = useId();
  const editor = useRef<HTMLTextAreaElement>(null);
  const editButton = useRef<HTMLButtonElement>(null);
  const removeButton = useRef<HTMLButtonElement>(null);
  const confirmButton = useRef<HTMLButtonElement>(null);
  const previousMode = useRef<'view' | 'edit' | 'remove'>('view');
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (editing) editor.current?.focus();
    else if (confirmRemove) confirmButton.current?.focus();
    else if (previousMode.current === 'edit') editButton.current?.focus();
    else if (previousMode.current === 'remove') removeButton.current?.focus();
    previousMode.current = editing ? 'edit' : confirmRemove ? 'remove' : 'view';
  }, [editing, confirmRemove, error]);

  async function perform(action: 'save' | 'remove' | 'refresh') {
    if (unavailable && action !== 'refresh') return;
    const result = await (action === 'save'
      ? onSave(draft, draftScope)
      : action === 'remove'
        ? onRemove()
        : onRefresh());
    if (!mounted.current || result === 'aborted') return;
    if (result === 'saved') {
      setError(null);
      if (action === 'save') {
        setEditing(false);
        onEditingChange(false);
      }
    } else setError(result);
  }

  const createdAt = memory.created_at ? new Date(memory.created_at) : null;
  const date =
    createdAt && !Number.isNaN(createdAt.getTime())
      ? createdAt.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' })
      : null;
  const errorKey = unavailable
    ? 'settings.memory.entryUnavailable'
    : error === 'conflict' && confirmRemove
      ? 'settings.memory.removeConflict'
      : error === 'error' && confirmRemove
        ? 'settings.memory.removeError'
        : error
          ? ERROR_KEYS[error]
          : null;

  return (
    <li
      aria-label={t('settings.memory.entryLabel', { number })}
      className="rounded-lg border border-border p-3"
    >
      <div className="flex flex-wrap items-center gap-2 text-xs font-medium text-muted">
        <span>
          {t(memory.kind === 'preference' ? 'settings.memory.preference' : 'settings.memory.task')}
        </span>
        <span>
          {t(
            (memory.scope ?? (memory.source_repo ? 'repo' : 'user')) === 'repo'
              ? 'settings.memory.repoScope'
              : 'settings.memory.userScope',
            { repo: memory.source_repo ?? '' },
          )}
        </span>
        {memory.user_edited || memory.edited ? (
          <span className="rounded bg-accent-subtle px-2 py-0.5 text-accent">
            {t('settings.memory.edited')}
          </span>
        ) : null}
      </div>
      {editing ? (
        <p className="mt-2 text-xs font-medium text-muted">
          {t(unavailable ? 'settings.memory.lastSavedText' : 'settings.memory.savedText')}
        </p>
      ) : null}
      <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-foreground">
        {memory.content}
      </p>
      <p className="mt-2 break-words text-xs text-muted">
        {t('settings.memory.entryProject', {
          project: memory.source_repo ?? t('settings.memory.noProject'),
        })}
      </p>
      <p className="mt-1 text-xs text-muted">
        {date && memory.created_at ? (
          <time dateTime={memory.created_at}>{t('settings.memory.entryDate', { date })}</time>
        ) : (
          t('settings.memory.noDate')
        )}
      </p>
      <Link
        href={`/${encodeURIComponent(memory.source_job_id)}`}
        className="mt-2 inline-flex min-h-8 items-center text-xs font-medium text-accent hover:underline"
      >
        {t('settings.memory.source')}
      </Link>

      {errorKey ? (
        <div role="alert" id={`${editorId}-error`} className="mt-3">
          <p className="text-xs leading-relaxed text-status-error">{t(errorKey)}</p>
          {!unavailable && (error === 'conflict' || error === 'missing') ? (
            <button
              type="button"
              onClick={() => void perform('refresh')}
              disabled={pending}
              className="ui-button ui-button-secondary mt-2 min-h-11"
            >
              {t('settings.memory.refreshEntry')}
            </button>
          ) : null}
        </div>
      ) : null}

      {editing ? (
        <div className="mt-3">
          <label htmlFor={editorId} className="block text-sm font-medium text-foreground">
            {t('settings.memory.editContent')}
          </label>
          <textarea
            ref={editor}
            id={editorId}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value);
              setError(null);
            }}
            maxLength={240}
            rows={4}
            disabled={pending}
            readOnly={unavailable}
            aria-invalid={error === 'invalid'}
            aria-describedby={`${editorId}-limit${errorKey ? ` ${editorId}-error` : ''}`}
            className="ui-input mt-2 w-full resize-y"
          />
          <p id={`${editorId}-limit`} className="mt-1 text-xs text-muted">
            {t('settings.memory.entryLimit', { count: draft.length })}
          </p>
          <label
            htmlFor={`${editorId}-scope`}
            className="mt-2 block text-sm font-medium text-foreground"
          >
            {t('settings.memory.scope')}
          </label>
          <select
            id={`${editorId}-scope`}
            value={draftScope}
            onChange={(event) => setDraftScope(event.target.value as 'repo' | 'user')}
            disabled={pending || unavailable}
            className="ui-input mt-2 w-full"
          >
            <option value="repo" disabled={!memory.source_repo}>
              {t('settings.memory.repoScope', { repo: memory.source_repo ?? '' })}
            </option>
            <option value="user">{t('settings.memory.userScope')}</option>
          </select>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void perform('save')}
              disabled={pending || unavailable || !draft.trim() || draft.length > 240}
              className="ui-button ui-button-primary min-h-11"
            >
              {t('settings.memory.saveEntry')}
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                onEditingChange(false);
                setError(null);
              }}
              disabled={pending}
              className="ui-button ui-button-secondary min-h-11"
            >
              {t('settings.memory.cancel')}
            </button>
          </div>
        </div>
      ) : confirmRemove ? (
        <div
          role="group"
          aria-label={t('settings.memory.removeTitle')}
          className="mt-3 rounded-lg bg-surface-muted p-3"
        >
          <p className="text-sm font-medium text-foreground">{t('settings.memory.removeTitle')}</p>
          <p className="mt-1 text-xs leading-relaxed text-muted">
            {t('settings.memory.removeDescription')}
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <button
              ref={confirmButton}
              type="button"
              onClick={() => void perform('remove')}
              disabled={pending}
              className="ui-button ui-button-secondary min-h-11 text-status-error"
            >
              {t('settings.memory.confirmRemove')}
            </button>
            <button
              type="button"
              onClick={() => {
                setConfirmRemove(false);
                setError(null);
              }}
              disabled={pending}
              className="ui-button ui-button-secondary min-h-11"
            >
              {t('settings.memory.cancel')}
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex flex-wrap gap-2">
          <button
            ref={editButton}
            type="button"
            onClick={() => {
              setDraft(memory.content);
              setDraftScope(memory.scope ?? (memory.source_repo ? 'repo' : 'user'));
              setEditing(true);
              onEditingChange(true);
              setError(null);
            }}
            disabled={pending}
            className="ui-button ui-button-secondary min-h-11"
          >
            {t('settings.memory.editEntry')}
          </button>
          <button
            ref={removeButton}
            type="button"
            onClick={() => {
              setConfirmRemove(true);
              setError(null);
            }}
            disabled={pending}
            className="ui-button ui-button-secondary min-h-11"
          >
            {t('settings.memory.removeEntry')}
          </button>
        </div>
      )}
    </li>
  );
}
