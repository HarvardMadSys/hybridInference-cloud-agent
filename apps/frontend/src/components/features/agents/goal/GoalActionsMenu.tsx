'use client';

import { useEffect, useId, useRef, useState, type RefObject } from 'react';
import { useT } from '@/components/providers';

/** Less frequent goal actions, with Escape reserved for dismissing this menu. */
export function GoalActionsMenu({
  triggerRef,
  canEdit,
  disabled,
  onEdit,
  onClear,
}: {
  triggerRef: RefObject<HTMLButtonElement>;
  canEdit: boolean;
  disabled: boolean;
  onEdit: () => void;
  onClear: () => void;
}) {
  const t = useT();
  const id = useId();
  const container = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const openAtEnd = useRef(false);

  useEffect(() => {
    if (!open) return;
    const items = menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]');
    items?.[openAtEnd.current ? items.length - 1 : 0]?.focus();
    const outside = (event: PointerEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return;
      // The page's Stop shortcut listens on window's bubble phase.
      event.preventDefault();
      event.stopImmediatePropagation();
      setOpen(false);
      triggerRef.current?.focus();
    };
    document.addEventListener('pointerdown', outside);
    window.addEventListener('keydown', escape, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      window.removeEventListener('keydown', escape, true);
    };
  }, [open, triggerRef]);

  function choose(action: () => void) {
    setOpen(false);
    triggerRef.current?.focus();
    action();
  }

  const itemClass =
    'flex min-h-9 w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm transition-colors hover:bg-gray-100 focus:bg-gray-100 focus:outline-none disabled:opacity-40 dark:hover:bg-white/10 dark:focus:bg-white/10 [@media(pointer:coarse)]:min-h-11';

  return (
    <div
      ref={container}
      className="relative shrink-0"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false);
      }}
    >
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        aria-label={t('goal.options')}
        title={t('goal.options')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => {
          openAtEnd.current = false;
          setOpen((value) => !value);
        }}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          openAtEnd.current = event.key === 'ArrowUp';
          setOpen(true);
        }}
        className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-200/60 hover:text-gray-900 focus-visible:outline focus-visible:outline-2 focus-visible:outline-crimson disabled:opacity-40 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
      >
        <svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="5" cy="12" r="1.75" />
          <circle cx="12" cy="12" r="1.75" />
          <circle cx="19" cy="12" r="1.75" />
        </svg>
      </button>
      {open ? (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label={t('goal.options')}
          className="absolute right-0 top-full z-50 mt-1 w-44 rounded-xl border border-gray-200 bg-white p-1 shadow-lg dark:border-white/10 dark:bg-gray-800"
          onKeyDown={(event) => {
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
            event.preventDefault();
            const items = Array.from(
              menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)'),
            );
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
          {canEdit ? (
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={disabled}
              title={t('goal.editHint')}
              onClick={() => choose(onEdit)}
              className={`${itemClass} text-gray-700 dark:text-gray-200`}
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.7}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M4 20h4.2L19 9.2a2.3 2.3 0 0 0-3.2-3.2L5 16.8V20Zm9.6-13.6 3.2 3.2"
                />
              </svg>
              {t('goal.edit')}
            </button>
          ) : null}
          <button
            type="button"
            role="menuitem"
            tabIndex={-1}
            disabled={disabled}
            title={t('goal.clearHint')}
            onClick={() => choose(onClear)}
            className={`${itemClass} text-crimson dark:text-crimson-light`}
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.7}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M4.5 7h15m-10 4v6m5-6v6M6.5 7l.9 13h9.2l.9-13M9.5 7V4h5v3"
              />
            </svg>
            {t('goal.clear')}
          </button>
        </div>
      ) : null}
    </div>
  );
}
