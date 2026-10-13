'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { pinAgentJob, renameAgentJob, unpinAgentJob } from '@/lib/api/agents';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import type { AgentJob } from './types';

/** Conversation actions, with Escape reserved for dismissing their own overlays. */
export function ThreadActions({
  job,
  canFork,
  forking,
  onFork,
  onRename,
  onEnvironment,
}: {
  job: AgentJob;
  canFork: boolean;
  forking: boolean;
  onFork: () => void;
  onRename: (title: string) => void;
  onEnvironment?: () => void;
}) {
  const t = useT();
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const dialog = useRef<HTMLFormElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [title, setTitle] = useState(job.title);
  const [pinned, setPinned] = useState(Boolean(job.pinnedAt));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mutation = useRef(false);

  useEffect(() => setPinned(Boolean(job.pinnedAt)), [job.pinnedAt]);
  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        if (!change.threadIds.includes(job.threadId ?? job.id)) return;
        if (change.pinnedAt !== undefined) setPinned(Boolean(change.pinnedAt));
      }),
    [job.threadId, job.id],
  );

  function close() {
    setOpen(false);
    setRenaming(false);
    setError(null);
    trigger.current?.focus();
  }

  useEffect(() => {
    if (!open && !renaming) return;
    if (renaming) {
      input.current?.focus();
      input.current?.select();
    } else
      menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]:not(:disabled)')?.focus();
    const outside = (event: PointerEvent) => {
      if (
        open &&
        !menu.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // Run cancellation also listens on window. Consume this before its bubble listener.
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!mutation.current) close();
    };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('keydown', escape, true);
    };
  }, [open, renaming]);

  async function saveTitle() {
    const nextTitle = title.trim();
    if (!nextTitle || mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await renameAgentJob(job.id, nextTitle);
      onRename(result.title);
      close();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('taskActions.renameFailed'));
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }

  async function togglePin() {
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await (pinned ? unpinAgentJob(job.id) : pinAgentJob(job.id));
      setPinned(result.pinned);
      close();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t('taskActions.pinFailed'));
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }

  const itemClass =
    'ui-button ui-button-ghost w-full justify-start text-left text-foreground focus:bg-surface-hover';
  return (
    <div className="relative shrink-0">
      <button
        ref={trigger}
        type="button"
        aria-label={t('taskActions.options')}
        title={t('taskActions.options')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => {
          setError(null);
          setOpen(!open);
        }}
        className="ui-icon-button ui-button-ghost"
      >
        <svg aria-hidden="true" width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="5" cy="12" r="1.75" />
          <circle cx="12" cy="12" r="1.75" />
          <circle cx="19" cy="12" r="1.75" />
        </svg>
      </button>
      {open && (
        <div
          id={id}
          ref={menu}
          role="menu"
          aria-label={t('taskActions.options')}
          className="ui-card absolute right-0 top-full z-50 mt-1 w-48 p-1.5 shadow-xl"
          onKeyDown={(event) => {
            if (event.key === 'Tab') {
              setOpen(false);
              return;
            }
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const items = [
              ...menu.current!.querySelectorAll<HTMLButtonElement>(
                '[role="menuitem"]:not(:disabled)',
              ),
            ];
            const current = items.indexOf(document.activeElement as HTMLButtonElement);
            const next =
              event.key === 'Home'
                ? 0
                : event.key === 'End'
                  ? items.length - 1
                  : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
            items[next]?.focus();
          }}
        >
          <button
            role="menuitem"
            tabIndex={-1}
            disabled={busy}
            className={itemClass}
            onClick={() => {
              setTitle(job.title);
              setError(null);
              setOpen(false);
              setRenaming(true);
            }}
          >
            {t('taskActions.rename')}
          </button>
          {onEnvironment && (
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              className={itemClass}
              onClick={() => {
                setOpen(false);
                onEnvironment();
              }}
            >
              {t('environment.editSaved')}
            </button>
          )}
          <button
            role="menuitem"
            tabIndex={-1}
            disabled={busy}
            className={itemClass}
            onClick={() => void togglePin()}
          >
            {t(pinned ? 'taskActions.unpin' : 'taskActions.pin')}
          </button>
          <button
            role="menuitem"
            tabIndex={-1}
            disabled={!canFork || forking || busy}
            className={itemClass}
            onClick={() => {
              close();
              onFork();
            }}
          >
            {t('taskActions.fork')}
          </button>
          {error && (
            <p role="alert" className="px-3 py-2 text-xs text-status-error">
              {error}
            </p>
          )}
        </div>
      )}
      {renaming &&
        createPortal(
          <div
            className="fixed inset-0 z-[80] flex items-center justify-center bg-black/30 p-6"
            onPointerDown={(event) => {
              if (event.target === event.currentTarget && !busy) close();
            }}
          >
            <form
              ref={dialog}
              role="dialog"
              aria-modal="true"
              aria-labelledby={`${id}-title`}
              aria-busy={busy}
              className="ui-card w-full max-w-md p-6 shadow-xl"
              onSubmit={(event) => {
                event.preventDefault();
                void saveTitle();
              }}
              onKeyDown={(event) => {
                if (event.key !== 'Tab') return;
                const items = [
                  ...dialog.current!.querySelectorAll<HTMLElement>(
                    'input:not(:disabled), button:not(:disabled)',
                  ),
                ];
                const first = items[0],
                  last = items[items.length - 1];
                if (event.shiftKey && document.activeElement === first) {
                  event.preventDefault();
                  last?.focus();
                } else if (!event.shiftKey && document.activeElement === last) {
                  event.preventDefault();
                  first?.focus();
                }
              }}
            >
              <h2 id={`${id}-title`} className="mb-4 text-base font-semibold text-foreground">
                {t('taskActions.renameTitle')}
              </h2>
              <label htmlFor={`${id}-input`} className="mb-2 block text-sm text-muted">
                {t('taskActions.title')}
              </label>
              <input
                ref={input}
                id={`${id}-input`}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                maxLength={160}
                required
                readOnly={busy}
                className="ui-input w-full"
              />
              {error && (
                <p role="alert" className="mt-3 text-sm text-status-error">
                  {error}
                </p>
              )}
              <div className="mt-5 flex justify-end gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={close}
                  className="ui-button ui-button-ghost"
                >
                  {t('taskActions.cancel')}
                </button>
                <button
                  type="submit"
                  disabled={busy || !title.trim()}
                  className="ui-button ui-button-primary"
                >
                  {t(busy ? 'taskActions.saving' : 'taskActions.save')}
                </button>
              </div>
            </form>
          </div>,
          document.body,
        )}
    </div>
  );
}
