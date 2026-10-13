'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { AGENT_SESSION_SELECTION_LIMIT } from '@/lib/api/agents';

export const ARCHIVE_FOCUS =
  'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/40 focus-visible:ring-offset-2 dark:focus-visible:ring-offset-gray-900';

/** Why the server refused to archive or delete a selected session. */
export const SESSION_ERROR_KEYS = {
  fork_in_progress: 'archive.forkInProgress',
  publication_pending: 'archive.publicationPending',
  not_found: 'archive.sessionNotFound',
} as const;

export function ArchiveIcon({
  name,
  className = 'h-4 w-4',
}: {
  name:
    | 'archive'
    | 'search'
    | 'folder'
    | 'filter'
    | 'chevron'
    | 'more'
    | 'delete'
    | 'check'
    | 'close';
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.65}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {name === 'archive' ? (
        <>
          <rect x="3" y="3" width="18" height="4" rx="1" />
          <path d="M5 7v13h14V7M10 11h4" />
        </>
      ) : null}
      {name === 'search' ? (
        <>
          <circle cx="10.5" cy="10.5" r="6.5" />
          <path d="m16 16 4.5 4.5" />
        </>
      ) : null}
      {name === 'folder' ? (
        <path d="M3 7V5a1 1 0 0 1 1-1h5l3 3h8a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Zm0 2h18" />
      ) : null}
      {name === 'filter' ? <path d="M4 6h16M7 12h10M10 18h4" /> : null}
      {name === 'chevron' ? <path d="m9 5 7 7-7 7" /> : null}
      {name === 'more' ? (
        <>
          <circle cx="5" cy="12" r="1" />
          <circle cx="12" cy="12" r="1" />
          <circle cx="19" cy="12" r="1" />
        </>
      ) : null}
      {name === 'delete' ? (
        <>
          <path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7" />
        </>
      ) : null}
      {name === 'check' ? (
        <>
          <circle cx="12" cy="12" r="9" />
          <path d="m8 12 3 3 5-6" />
        </>
      ) : null}
      {name === 'close' ? <path d="m6 6 12 12M6 18 18 6" /> : null}
    </svg>
  );
}

export function ArchiveProjectMenu({
  project,
  count,
  disabled,
  onUnarchive,
  onDelete,
}: {
  project: string;
  count: number;
  disabled: boolean;
  onUnarchive: () => void;
  onDelete: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector('button')?.focus();
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  return (
    <div
      ref={root}
      className="relative shrink-0"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (!open && ['ArrowDown', 'ArrowUp'].includes(event.key)) {
          event.preventDefault();
          setOpen(true);
          return;
        }
        if (event.key === 'Escape' && open) {
          event.preventDefault();
          event.stopPropagation();
          setOpen(false);
          trigger.current?.focus();
        }
        if (!open || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const items = [...(menu.current?.querySelectorAll<HTMLButtonElement>('button') ?? [])];
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        const next =
          event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? items.length - 1
              : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length;
        items[next]?.focus();
      }}
    >
      <button
        ref={trigger}
        type="button"
        aria-label={t('archive.projectActions', { project })}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        className={`flex h-9 w-9 items-center justify-center rounded-md text-gray-400 hover:bg-gray-100 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:bg-white/5 dark:hover:text-gray-200 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 ${ARCHIVE_FOCUS}`}
        onClick={() => setOpen(!open)}
      >
        <ArchiveIcon name="more" />
      </button>
      {open ? (
        <div
          ref={menu}
          id={menuId}
          role="menu"
          aria-label={t('archive.projectActions', { project })}
          className="absolute right-0 top-10 z-20 min-w-56 rounded-lg border border-gray-200 bg-white p-1 shadow-lg dark:border-white/10 dark:bg-gray-900"
        >
          <button
            type="button"
            role="menuitem"
            className={`flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs text-gray-700 hover:bg-gray-100 dark:text-gray-200 dark:hover:bg-white/5 ${ARCHIVE_FOCUS}`}
            onClick={() => {
              setOpen(false);
              trigger.current?.focus();
              onUnarchive();
            }}
          >
            <ArchiveIcon name="archive" />
            {t('archive.unarchiveGroup', { count })}
          </button>
          <button
            type="button"
            role="menuitem"
            className={`flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-xs text-red-600 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-500/10 ${ARCHIVE_FOCUS}`}
            onClick={() => {
              setOpen(false);
              trigger.current?.focus();
              onDelete();
            }}
          >
            <ArchiveIcon name="delete" />
            {t('archive.deleteGroup', { count })}
          </button>
        </div>
      ) : null}
    </div>
  );
}

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function ArchiveDeleteDialog({
  count,
  skipped,
  scope,
  titles,
  busy,
  limitExceeded,
  error,
  onCancel,
  onConfirm,
}: {
  count: number;
  skipped: number;
  scope: string;
  titles: string[];
  busy: boolean;
  limitExceeded: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    cancel.current?.focus();
    return () => {
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center overflow-y-auto p-4"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          if (!busy) onCancel();
        }
        if (event.key !== 'Tab') return;
        const items = [...(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
        if (!items.length) {
          event.preventDefault();
          return;
        }
        if (event.shiftKey && document.activeElement === items[0]) {
          event.preventDefault();
          items[items.length - 1]?.focus();
        } else if (!event.shiftKey && document.activeElement === items[items.length - 1]) {
          event.preventDefault();
          items[0]?.focus();
        }
      }}
    >
      <button
        type="button"
        aria-label={t('archive.close')}
        tabIndex={-1}
        disabled={busy}
        onClick={onCancel}
        className="absolute inset-0 cursor-default bg-gray-900/25 dark:bg-black/60"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy}
        className="relative max-h-full w-full max-w-md overflow-y-auto rounded-xl border border-gray-200 bg-white p-6 shadow-xl dark:border-white/10 dark:bg-gray-900"
      >
        <div className="mb-4 flex items-center justify-between">
          <span className="rounded-lg bg-red-50 p-2.5 text-red-600 dark:bg-red-500/10 dark:text-red-300">
            <ArchiveIcon name="delete" className="h-5 w-5" />
          </span>
          <button
            type="button"
            aria-label={t('archive.close')}
            disabled={busy}
            onClick={onCancel}
            className={`rounded-md p-2 text-gray-400 hover:bg-gray-100 dark:text-gray-500 dark:hover:bg-white/5 ${ARCHIVE_FOCUS}`}
          >
            <ArchiveIcon name="close" />
          </button>
        </div>
        <h2 id={titleId} className="text-lg font-medium text-gray-900 dark:text-gray-100">
          {t(
            count === 0
              ? 'archive.noDeletableTitle'
              : count === 1
                ? 'archive.deleteTitleOne'
                : 'archive.deleteTitle',
            { count },
          )}
        </h2>
        <div
          id={descriptionId}
          className="mt-3 space-y-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400"
        >
          <p>{count ? t('archive.deleteBody') : t('archive.noDeletable')}</p>
          <p className="break-words font-medium text-gray-700 dark:text-gray-200">{scope}</p>
          {skipped > 0 ? <p>{t('archive.deleteSkipped', { count: skipped })}</p> : null}
          {limitExceeded ? (
            <p className="text-red-600 dark:text-red-300">
              {t('archive.bulkLimit', { count: AGENT_SESSION_SELECTION_LIMIT })}
            </p>
          ) : null}
        </div>
        {titles.length > 0 ? (
          <ul className="mt-4 space-y-1.5 rounded-lg bg-gray-50 p-3 text-xs text-gray-700 dark:bg-white/5 dark:text-gray-200">
            {titles.map((title, index) => (
              <li key={index} className="break-words">
                {title}
              </li>
            ))}
            {count > titles.length ? (
              <li className="text-gray-500 dark:text-gray-400">
                {t('archive.andMore', { count: count - titles.length })}
              </li>
            ) : null}
          </ul>
        ) : null}
        {error ? (
          <p className="mt-4 text-xs text-red-600 dark:text-red-300" role="alert">
            {error}
          </p>
        ) : null}
        <div className="mt-6 flex flex-wrap justify-end gap-2">
          <button
            ref={cancel}
            type="button"
            onClick={onCancel}
            disabled={busy}
            className={`rounded-lg border border-gray-200 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5 ${ARCHIVE_FOCUS}`}
          >
            {t('archive.cancel')}
          </button>
          {count > 0 ? (
            <button
              type="button"
              onClick={onConfirm}
              disabled={busy || limitExceeded}
              className={`rounded-lg bg-red-600 px-3 py-2 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-60 dark:bg-red-400 dark:text-gray-950 dark:hover:bg-red-300 ${ARCHIVE_FOCUS}`}
            >
              {busy
                ? t('archive.deleting')
                : t(count === 1 ? 'archive.deleteCountOne' : 'archive.deleteCount', { count })}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
