'use client';

import { useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';

/** Own focus and Escape while an optional dialog's code is downloading. */
export function ComposerDialogLoading({ title, onClose }: { title: string; onClose: () => void }) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useModalDialog(panel, { onClose, initialFocus: 'button' });
  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-gray-900/25 p-4 dark:bg-black/50">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-xl rounded-2xl border border-gray-200 bg-white p-6 shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex items-center justify-between gap-4">
          <h2 id={titleId} className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            {title}
          </h2>
          <button type="button" onClick={onClose} className="ui-focus-ring rounded-md px-2 py-1">
            {t('skills.cancel')}
          </button>
        </div>
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          {t('common.loading')}
        </p>
      </div>
    </div>,
    document.body,
  );
}
