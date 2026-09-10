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
}: {
  job: AgentJob;
  canFork: boolean;
  forking: boolean;
  onFork: () => void;
  onRename: (title: string) => void;
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
    'flex w-full rounded-md px-3 py-2 text-left text-sm text-gray-800 hover:bg-gray-100 focus:bg-gray-100 focus:outline-none disabled:opacity-40 dark:text-gray-100 dark:hover:bg-white/10 dark:focus:bg-white/10';
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
        className="rounded-lg p-2 text-gray-500 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 dark:text-gray-400 dark:hover:bg-white/10"
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
          className="absolute right-0 top-full z-50 mt-1 w-48 rounded-xl border border-gray-200 bg-white p-1.5 shadow-xl dark:border-white/10 dark:bg-gray-800"
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
            <p role="alert" className="px-3 py-2 text-xs text-red-600 dark:text-red-400">
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
              className="w-full max-w-md rounded-xl border border-gray-200 bg-white p-6 shadow-xl dark:border-white/10 dark:bg-gray-900"
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
              <h2
                id={`${id}-title`}
                className="mb-4 text-base font-semibold text-gray-900 dark:text-gray-100"
              >
                {t('taskActions.renameTitle')}
              </h2>
              <label
                htmlFor={`${id}-input`}
                className="mb-2 block text-sm text-gray-600 dark:text-gray-300"
              >
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
                className="w-full rounded-lg border border-gray-300 bg-transparent px-3 py-2 text-sm text-gray-900 focus:border-crimson focus:outline-none dark:border-white/20 dark:text-gray-100"
              />
              {error && (
                <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
                  {error}
                </p>
              )}
              <div className="mt-5 flex justify-end gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={close}
                  className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/10"
                >
                  {t('taskActions.cancel')}
                </button>
                <button
                  type="submit"
                  disabled={busy || !title.trim()}
                  className="rounded-lg bg-gray-900 px-3 py-2 text-sm text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
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
