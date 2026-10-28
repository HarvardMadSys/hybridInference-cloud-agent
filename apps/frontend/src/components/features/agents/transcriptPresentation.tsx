'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import {
  MENU_HEADING,
  MENU_POSITIONED,
  MENU_ROW_SELECTED,
  MENU_ACTIONS,
  menuRowClass,
} from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';

export type TranscriptPresentation = 'compact' | 'standard' | 'detailed';

export const transcriptPresentationKey = (userId: string, threadId: string) =>
  `agents.transcript.presentation:${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;

function parsePresentation(value: string | null): TranscriptPresentation {
  return value === 'compact' || value === 'detailed' ? value : 'standard';
}

/** An account's preference belongs to this conversation, including all its turns. */
export function useTranscriptPresentation(userId: string | undefined, threadId: string) {
  const key = userId ? transcriptPresentationKey(userId, threadId) : null;
  const owner = useRef(key);
  owner.current = key;
  const [saved, setSaved] = useState<{ key: string | null; mode: TranscriptPresentation }>({
    key: null,
    mode: 'standard',
  });

  useEffect(() => {
    const read = () => {
      let mode: TranscriptPresentation = 'standard';
      try {
        if (key) mode = parsePresentation(localStorage.getItem(key));
      } catch {
        // Storage is optional; the current conversation remains usable.
      }
      setSaved({ key, mode });
    };
    read();
    const onStorage = (event: StorageEvent) => {
      if (owner.current === key && key && (event.key === key || event.key === null)) read();
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [key]);

  const update = useCallback(
    (mode: TranscriptPresentation) => {
      if (owner.current !== key) return;
      if (key) {
        try {
          localStorage.setItem(key, mode);
        } catch {
          // A blocked write must not block the user's display choice.
        }
      }
      setSaved({ key, mode });
    },
    [key],
  );

  return [saved.key === key ? saved.mode : 'standard', update] as const;
}

export function TranscriptPresentationControl({
  value,
  onChange,
}: {
  value: TranscriptPresentation;
  onChange: (value: TranscriptPresentation) => void;
}) {
  const t = useT();
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef: trigger,
    menuRef: menu,
    placement: {
      gap: 8,
      width: 288,
      maxWidth: 320,
      preferredHeight: 240,
      maxHeight: 400,
    },
    initialFocus: 'selected',
  });

  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
            event.preventDefault();
            setOpen(true);
          }
        }}
        className="ui-focus-ring inline-flex min-h-9 items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted transition-colors hover:bg-surface-hover hover:text-foreground aria-expanded:bg-surface-muted aria-expanded:text-foreground [@media(pointer:coarse)]:min-h-11"
      >
        <svg
          aria-hidden="true"
          className="h-3.5 w-3.5 shrink-0"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.8}
        >
          <path d="M3 6h7m4 0h7M3 12h12m4 0h2M3 18h3m4 0h11" />
          <circle cx="12" cy="6" r="2" />
          <circle cx="17" cy="12" r="2" />
          <circle cx="8" cy="18" r="2" />
        </svg>
        {/* On a phone the label pushed the run controls onto a third header
            row; the icon stays, and the label remains the button's name. */}
        <span className="max-sm:sr-only">
          {t('transcript.display', { mode: t(`transcript.${value}`) })}
        </span>
        <svg
          aria-hidden="true"
          className="h-3 w-3 shrink-0"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
        >
          <path d="m5 7.5 5 5 5-5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open &&
        createPortal(
          <div
            id={id}
            ref={menu}
            role="menu"
            aria-labelledby={`${id}-title`}
            aria-describedby={`${id}-hint`}
            style={popup.style}
            className={`${MENU_ACTIONS} ${MENU_POSITIONED} w-72 max-w-[calc(100vw-1rem)]`}
            onKeyDown={popup.onKeyDown}
          >
            <div id={`${id}-title`} className={MENU_HEADING}>
              {t('transcript.displayTitle')}
            </div>
            {(['compact', 'standard', 'detailed'] as const).map((mode) => (
              <button
                key={mode}
                type="button"
                role="menuitemradio"
                aria-checked={value === mode}
                aria-labelledby={`${id}-${mode}-label`}
                aria-describedby={`${id}-${mode}-description`}
                tabIndex={-1}
                onClick={() => {
                  onChange(mode);
                  setOpen(false);
                  trigger.current?.focus();
                }}
                className={`${value === mode ? MENU_ROW_SELECTED : menuRowClass()} items-start`}
              >
                <span aria-hidden="true" className="w-4 shrink-0 text-sm">
                  {value === mode ? '✓' : ''}
                </span>
                <span className="min-w-0">
                  <span id={`${id}-${mode}-label`} className="block text-sm">
                    {t(`transcript.${mode}`)}
                  </span>
                  <span
                    id={`${id}-${mode}-description`}
                    className="mt-0.5 block text-xs text-muted"
                  >
                    {t(`transcript.${mode}Description`)}
                  </span>
                </span>
              </button>
            ))}
            <div
              id={`${id}-hint`}
              className="mt-1.5 border-t border-border px-2.5 pb-2 pt-2.5 text-xs text-muted"
            >
              {t('transcript.displayHint')}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
