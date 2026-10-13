'use client';

import { Fragment, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import Link from 'next/link';
import { useLocale } from '@/components/providers';
import type { UserMemoryEntry } from '@/lib/api/userMemory';
import { PencilIcon, TrashIcon } from './icons';

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

/** Entries saved before scopes existed apply to their source repository, if any. */
export function entryScope(memory: UserMemoryEntry): 'repo' | 'user' {
  return memory.scope ?? (memory.source_repo ? 'repo' : 'user');
}

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
  const [draftScope, setDraftScope] = useState(entryScope(memory));
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
  const scope = entryScope(memory);
  const errorKey = unavailable
    ? 'settings.memory.entryUnavailable'
    : error === 'conflict' && confirmRemove
      ? 'settings.memory.removeConflict'
      : error === 'error' && confirmRemove
        ? 'settings.memory.removeError'
        : error
          ? ERROR_KEYS[error]
          : null;
  const meta: ReactNode[] = [
    <span key="kind">
      {t(memory.kind === 'preference' ? 'settings.memory.preference' : 'settings.memory.task')}
    </span>,
    scope === 'user' ? (
      <span key="scope">{t('settings.memory.userScope')}</span>
    ) : memory.source_repo ? (
      <span key="scope" className="break-all">
        {memory.source_repo}
      </span>
    ) : null,
    memory.created_at && createdAt && !Number.isNaN(createdAt.getTime()) ? (
      <time key="date" dateTime={memory.created_at}>
        {createdAt.toLocaleDateString(locale, { dateStyle: 'medium' })}
      </time>
    ) : null,
    <Link
      key="source"
      href={`/${encodeURIComponent(memory.source_job_id)}`}
      className="inline-flex min-h-8 items-center font-medium text-accent hover:underline"
    >
      {t('settings.memory.source')}
    </Link>,
  ].filter(Boolean);

  return (
    <li aria-label={t('settings.memory.entryLabel', { number })} className="px-3 py-2">
      {editing ? (
        <p className="mt-1 text-xs font-medium text-muted">
          {t(unavailable ? 'settings.memory.lastSavedText' : 'settings.memory.savedText')}
        </p>
      ) : null}
      <div className="flex items-start gap-2">
        <p className="min-w-0 flex-1 whitespace-pre-wrap break-words pt-1.5 text-sm leading-relaxed text-foreground">
          {memory.content}
        </p>
        {!editing && !confirmRemove ? (
          <div className="-mr-1 flex shrink-0">
            <button
              ref={editButton}
              type="button"
              onClick={() => {
                setDraft(memory.content);
                setDraftScope(entryScope(memory));
                setEditing(true);
                onEditingChange(true);
                setError(null);
              }}
              disabled={pending}
              aria-label={t('settings.memory.editEntry')}
              title={t('settings.memory.editEntry')}
              className="ui-icon-button ui-button-ghost"
            >
              <PencilIcon />
            </button>
            <button
              ref={removeButton}
              type="button"
              onClick={() => {
                setConfirmRemove(true);
                setError(null);
              }}
              disabled={pending}
              aria-label={t('settings.memory.removeEntry')}
              title={t('settings.memory.removeEntry')}
              className="ui-icon-button ui-button-ghost hover:!text-status-error"
            >
              <TrashIcon />
            </button>
          </div>
        ) : null}
      </div>
      <div className="flex flex-wrap items-center gap-x-1.5 text-xs text-muted">
        {meta.map((item, index) => (
          <Fragment key={index}>
            {index ? <span aria-hidden="true">·</span> : null}
            {item}
          </Fragment>
        ))}
        {memory.user_edited || memory.edited ? (
          <span className="ml-1 rounded bg-accent-subtle px-1.5 py-0.5 text-accent">
            {t('settings.memory.edited')}
          </span>
        ) : null}
      </div>

      {errorKey ? (
        <div role="alert" id={`${editorId}-error`} className="mb-1 mt-2">
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
        <div className="mb-1 mt-2">
          <label htmlFor={editorId} className="block text-xs font-medium text-foreground">
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
            rows={3}
            disabled={pending}
            readOnly={unavailable}
            aria-invalid={error === 'invalid'}
            aria-describedby={`${editorId}-limit${errorKey ? ` ${editorId}-error` : ''}`}
            className="ui-input mt-1 w-full resize-y"
          />
          <p id={`${editorId}-limit`} className="mt-1 text-xs text-muted">
            {t('settings.memory.entryLimit', { count: draft.length })}
          </p>
          <div className="mt-2 flex flex-wrap items-end gap-2">
            <div className="min-w-0">
              <label
                htmlFor={`${editorId}-scope`}
                className="block text-xs font-medium text-foreground"
              >
                {t('settings.memory.scope')}
              </label>
              <select
                id={`${editorId}-scope`}
                value={draftScope}
                onChange={(event) => setDraftScope(event.target.value as 'repo' | 'user')}
                disabled={pending || unavailable}
                className="ui-input mt-1 block min-h-11 max-w-full rounded-lg"
              >
                <option value="repo" disabled={!memory.source_repo}>
                  {t('settings.memory.repoScope', { repo: memory.source_repo ?? '' })}
                </option>
                <option value="user">{t('settings.memory.userScope')}</option>
              </select>
            </div>
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
              className="ui-button ui-button-ghost min-h-11"
            >
              {t('settings.memory.cancel')}
            </button>
          </div>
        </div>
      ) : confirmRemove ? (
        <div
          role="group"
          aria-label={t('settings.memory.removeTitle')}
          className="mb-1 mt-2 flex flex-wrap items-center gap-2 rounded-lg bg-surface-muted p-2"
        >
          <p className="min-w-0 flex-1 text-xs leading-relaxed text-muted">
            <span className="font-medium text-foreground">{t('settings.memory.removeTitle')}</span>{' '}
            {t('settings.memory.removeDescription')}
          </p>
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
            className="ui-button ui-button-ghost min-h-11"
          >
            {t('settings.memory.cancel')}
          </button>
        </div>
      ) : null}
    </li>
  );
}
