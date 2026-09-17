'use client';

import { useEffect, useId, useRef, type ReactNode } from 'react';
import { createPortal } from 'react-dom';

export const SKILL_FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-3 py-2 text-sm text-gray-900 dark:text-gray-100 disabled:opacity-60';
export const SKILL_BUTTON =
  'inline-flex items-center justify-center gap-1.5 rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50';
export const SKILL_PRIMARY =
  'inline-flex items-center justify-center gap-1.5 rounded-lg bg-gray-900 px-3 py-2 text-sm font-medium text-white hover:bg-gray-700 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white disabled:cursor-not-allowed disabled:opacity-50';

export function SkillIcon({ className = 'h-4 w-4' }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 5.5C9.5 3.5 6 3 3 4v15c3-1 6.5-.5 9 1.5C14.5 18.5 18 18 21 19V4c-3-1-6.5-.5-9 1.5Zm0 0v15M6 8h3m-3 4h3m6-4h3m-3 4h3"
      />
    </svg>
  );
}

export function SkillSwitch({
  label,
  checked,
  onChange,
  disabled = false,
}: {
  label: string;
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-label={label}
      aria-checked={checked}
      title={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`inline-flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-crimson disabled:opacity-50 ${checked ? 'bg-gray-900 dark:bg-gray-100' : 'bg-gray-200 dark:bg-gray-700'}`}
    >
      <span
        aria-hidden="true"
        className={`h-4 w-4 rounded-full bg-white shadow-sm transition-transform dark:bg-gray-900 ${checked ? 'translate-x-4' : ''}`}
      />
    </button>
  );
}

/** One accessible surface for skill editing, review, and conversation selection. */
export function SkillDialog({
  title,
  description,
  busy = false,
  onClose,
  children,
  footer,
}: {
  title: string;
  description?: string;
  busy?: boolean;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const id = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    (
      panel.current?.querySelector<HTMLElement>('input, textarea') ??
      panel.current?.querySelector<HTMLElement>('button')
    )?.focus();
    return () => {
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  if (typeof document === 'undefined') return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          if (!busy) onClose();
        }
        if (event.key === 'Tab') {
          const controls = Array.from(
            panel.current?.querySelectorAll<HTMLElement>(
              'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), a[href]',
            ) ?? [],
          ).filter((element) => !element.closest('[hidden]'));
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (first && last && event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (first && last && !event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }
      }}
    >
      <button
        type="button"
        tabIndex={-1}
        aria-label="Dismiss skill dialog"
        disabled={busy}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        className="relative flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 px-6 pt-5 pb-4">
          <div className="min-w-0">
            <h2
              id={id}
              className="break-words text-lg font-semibold text-gray-900 dark:text-gray-100"
            >
              {title}
            </h2>
            {description ? (
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">{description}</p>
            ) : null}
          </div>
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            aria-label="Close skill dialog"
            className="shrink-0 rounded-md px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5"
          >
            Close
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto px-6 pb-5">{children}</div>
        {footer ? (
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-gray-100 px-6 py-4 dark:border-white/5">
            {footer}
          </div>
        ) : null}
      </div>
    </div>,
    document.body,
  );
}

export function SkillDefault({
  checked,
  onChange,
  disabled = false,
}: {
  checked: boolean;
  onChange: (value: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="mt-5 flex items-start gap-2 text-sm text-gray-700 dark:text-gray-200">
      <input
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.target.checked)}
        disabled={disabled}
        className="mt-1 accent-gray-900 dark:accent-gray-100"
      />
      <span>
        Use by default in new chats
        <span className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">
          Choose different skills in each conversation.
        </span>
      </span>
    </label>
  );
}

export function SkillErrorMessage({ message }: { message: string | null }) {
  return message ? (
    <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
      {message}
    </p>
  ) : null;
}
