'use client';

import { useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useModalDialog } from '@/components/ui/useModalDialog';

export const FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm text-gray-900 focus:border-crimson focus:outline-none disabled:opacity-60 dark:border-white/10 dark:text-gray-100';
export const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5';
export const PRIMARY =
  'rounded-lg bg-crimson px-4 py-2 text-sm font-semibold text-white hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-50';

/** The modal surface both SSH dialogs share. */
export function SshDialogFrame({
  title,
  closeLabel,
  busy,
  onClose,
  initialFocus,
  children,
}: {
  title: string;
  closeLabel: string;
  busy: boolean;
  onClose: () => void;
  initialFocus?: string;
  children: React.ReactNode;
}) {
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  useModalDialog(panel, { busy, onClose, initialFocus });
  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button
        type="button"
        tabIndex={-1}
        aria-label={closeLabel}
        disabled={busy}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-busy={busy}
        className="relative max-h-full w-full max-w-lg overflow-y-auto rounded-2xl border border-gray-200 bg-white p-6 shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex items-start justify-between gap-4">
          <h2 id={`${id}-title`} className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {title}
          </h2>
          <button
            type="button"
            disabled={busy}
            onClick={onClose}
            aria-label={closeLabel}
            className="rounded-md px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-white/5"
          >
            ×
          </button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}
