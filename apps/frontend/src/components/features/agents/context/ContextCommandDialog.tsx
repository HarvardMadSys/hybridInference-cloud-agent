'use client';

// What `/context` opens before a conversation exists: there is no context yet,
// and the place that compares the owner's earlier conversations. Nothing is
// sent and the draft is left as it was. In a conversation, `/context` opens
// its main Context tab (beside Chat and Trajectory) instead.

import Link from 'next/link';
import { useCallback, useId, useRef, useState, type ReactNode } from 'react';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';

export const CONTEXT_INSIGHTS_HREF = '/settings/usage#settings-context-insights';

export function ContextCommandDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const bodyId = useId();
  useModalDialog(panel, { onClose, initialFocus: 'button[data-close]' });
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-gray-900/25 p-4 dark:bg-black/50">
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="ui-card w-full max-w-md p-5"
      >
        <h2 id={titleId} className="text-base font-semibold text-foreground">
          {t('context.command.title')}
        </h2>
        <div id={bodyId} className="mt-2 space-y-2 text-sm text-muted">
          <p>{t('context.command.noConversation')}</p>
          <p>{t('context.command.insights')}</p>
        </div>
        <div className="mt-4 flex flex-wrap justify-end gap-2">
          <Link
            href={CONTEXT_INSIGHTS_HREF}
            className="ui-button ui-button-secondary min-h-11"
            onClick={onClose}
          >
            {t('context.command.openInsights')}
          </Link>
          <button
            type="button"
            data-close
            onClick={onClose}
            className="ui-button ui-button-primary min-h-11"
          >
            {t('context.command.close')}
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * `/context` for a composer with no conversation yet: `open` shows the dialog,
 * `dialog` is what to draw, and closing hands focus back to the box.
 */
export function useContextCommandDialog(refocus: () => void): {
  open: () => void;
  dialog: ReactNode;
} {
  const [shown, setShown] = useState(false);
  const open = useCallback(() => setShown(true), []);
  const close = useCallback(() => {
    setShown(false);
    refocus();
  }, [refocus]);
  return { open, dialog: shown ? <ContextCommandDialog onClose={close} /> : null };
}
