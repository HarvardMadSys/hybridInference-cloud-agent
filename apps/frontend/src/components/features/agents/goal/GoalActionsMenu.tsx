'use client';

import { useId, useRef, useState, type RefObject } from 'react';
import { useT } from '@/components/providers';
import {
  MENU_POSITIONED,
  MENU_SURFACE,
  MENU_ROW,
  MENU_ROW_DESTRUCTIVE,
} from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';

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
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const openAtEnd = useRef(false);
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef,
    menuRef: menu,
    placement: { gap: 4, preferredHeight: 140, maxHeight: 320 },
    initialFocus: openAtEnd.current ? 'last' : 'first',
  });

  function choose(action: () => void) {
    setOpen(false);
    triggerRef.current?.focus();
    action();
  }

  return (
    <div className="relative shrink-0">
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
        className="ui-focus-ring inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-foreground disabled:opacity-40 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
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
          style={popup.style}
          className={`${MENU_SURFACE} ${MENU_POSITIONED} w-44`}
          onKeyDown={popup.onKeyDown}
        >
          {canEdit ? (
            <button
              type="button"
              role="menuitem"
              tabIndex={-1}
              disabled={disabled}
              title={t('goal.editHint')}
              onClick={() => choose(onEdit)}
              className={MENU_ROW}
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4 shrink-0"
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
            className={MENU_ROW_DESTRUCTIVE}
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4 shrink-0"
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
