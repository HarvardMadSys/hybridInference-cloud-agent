'use client';

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';

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
  const [position, setPosition] = useState({ top: 0, left: 0, maxHeight: 0 });

  useLayoutEffect(() => {
    if (!open || !trigger.current || !menu.current) return;
    const anchor = trigger.current.getBoundingClientRect();
    const bounds = menu.current.getBoundingClientRect();
    const top = Math.max(8, Math.min(anchor.bottom + 8, window.innerHeight - bounds.height - 8));
    setPosition({
      left: Math.max(
        8,
        Math.min(anchor.right - bounds.width, window.innerWidth - bounds.width - 8),
      ),
      top,
      maxHeight: window.innerHeight - top - 8,
    });
  }, [open]);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[aria-checked="true"]')?.focus();
    const outside = (event: PointerEvent | FocusEvent) => {
      if (
        !menu.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      // Escape dismisses this menu, not the running agent behind it.
      event.preventDefault();
      event.stopImmediatePropagation();
      setOpen(false);
      trigger.current?.focus();
    };
    const dismiss = () => setOpen(false);
    const scroll = (event: Event) => {
      if (!menu.current?.contains(event.target as Node)) dismiss();
    };
    document.addEventListener('pointerdown', outside);
    document.addEventListener('focusin', outside);
    window.addEventListener('keydown', escape, true);
    window.addEventListener('resize', dismiss);
    window.addEventListener('scroll', scroll, true);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('focusin', outside);
      window.removeEventListener('keydown', escape, true);
      window.removeEventListener('resize', dismiss);
      window.removeEventListener('scroll', scroll, true);
    };
  }, [open]);

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
        <span>{t('transcript.display', { mode: t(`transcript.${value}`) })}</span>
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
            style={{ ...position, maxHeight: position.maxHeight || undefined }}
            className="ui-card fixed z-50 w-72 max-w-[calc(100vw-1rem)] overflow-y-auto p-1.5 shadow-xl"
            onKeyDown={(event) => {
              if (event.key === 'Tab') {
                // Resume the page's native tab order at the trigger, not the portal.
                trigger.current?.focus();
                setOpen(false);
                return;
              }
              if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
              event.preventDefault();
              const items = [
                ...menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'),
              ];
              const current = items.indexOf(document.activeElement as HTMLButtonElement);
              const next =
                event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? items.length - 1
                    : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) %
                      items.length;
              items[next]?.focus();
            }}
          >
            <div id={`${id}-title`} className="px-2.5 py-1.5 text-xs text-muted">
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
                className="ui-focus-ring flex w-full items-start gap-2 rounded-md px-2.5 py-2 text-left text-foreground hover:bg-surface-hover focus:bg-surface-hover aria-checked:bg-surface-muted [@media(pointer:coarse)]:min-h-11"
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
              className="mt-1.5 border-t border-border px-2.5 pb-1 pt-2.5 text-xs text-muted"
            >
              {t('transcript.displayHint')}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
