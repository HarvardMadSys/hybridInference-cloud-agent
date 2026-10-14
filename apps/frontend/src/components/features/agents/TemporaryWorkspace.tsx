'use client';

import { useCallback, useEffect, useId, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
import { downloadAgentWorkspace } from '@/lib/api/agents';

// What a temporary workspace tells its owner, and the one way its files leave.
//
// A temporary workspace has no repository: nothing it does is published, and
// once its sandbox is gone so are its files. The owner is told once, before the
// first message of the first one they start, and is asked to say they read it;
// after that the conversation's own badge carries the same words and the
// download. The acknowledgement is remembered per account, in this browser,
// like Skip in getting-started: a new browser asks once more, which costs a
// click.

const acknowledgedKey = (userId: string) =>
  `agents.temporaryWorkspace.acknowledged.${encodeURIComponent(userId)}`;

/** Whether this account has been told, in this browser, what a temporary workspace keeps. */
export function useTemporaryWorkspaceAcknowledgement(userId?: string): {
  acknowledged: boolean;
  acknowledge: () => void;
} {
  const [acknowledged, setAcknowledged] = useState(false);

  useEffect(() => {
    if (!userId) return;
    try {
      setAcknowledged(window.localStorage.getItem(acknowledgedKey(userId)) === '1');
    } catch {
      setAcknowledged(false);
    }
  }, [userId]);

  const acknowledge = useCallback(() => {
    setAcknowledged(true);
    if (!userId) return;
    try {
      window.localStorage.setItem(acknowledgedKey(userId), '1');
    } catch {
      // Remembered for this visit only.
    }
  }, [userId]);

  return { acknowledged, acknowledge };
}

/** The first message's one stop: what a temporary workspace keeps, and a way back. */
export function TemporaryWorkspaceConfirm({
  onConfirm,
  onCancel,
  returnFocus,
}: {
  onConfirm: () => void;
  onCancel: () => void;
  /** Where the caret goes once the notice closes: the draft it interrupted. */
  returnFocus?: RefObject<HTMLElement>;
}) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const bodyId = useId();
  useModalDialog(panel, {
    onClose: onCancel,
    initialFocus: 'button[data-acknowledge]',
    returnFocus,
  });
  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-gray-900/25 p-4 dark:bg-black/50">
      <div
        ref={panel}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={bodyId}
        className="w-full max-w-md rounded-2xl border border-gray-200 bg-white p-6 shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <h2 id={titleId} className="text-base font-semibold text-gray-900 dark:text-gray-100">
          {t('temporaryWorkspace.confirmTitle')}
        </h2>
        <p id={bodyId} className="mt-2 text-sm leading-6 text-gray-600 dark:text-gray-300">
          {t('temporaryWorkspace.notice')}
        </p>
        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onCancel} className="ui-button px-3 text-xs">
            {t('temporaryWorkspace.cancel')}
          </button>
          <button
            type="button"
            data-acknowledge
            onClick={onConfirm}
            className="ui-button ui-button-primary px-3 text-xs"
          >
            {t('temporaryWorkspace.acknowledge')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** Hand a fetched file to the browser's own download. */
function saveFile(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Released once the browser has certainly taken the bytes, not before.
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

/** Download the whole workspace; a refusal is said beside the button. */
export function WorkspaceDownloadButton({
  jobId,
  className = '',
}: {
  jobId: string;
  className?: string;
}) {
  const t = useT();
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  async function download() {
    if (downloading) return;
    setDownloading(true);
    setError(null);
    try {
      const { blob, filename } = await downloadAgentWorkspace(jobId);
      saveFile(blob, filename);
    } catch (cause: unknown) {
      if (mounted.current) setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      if (mounted.current) setDownloading(false);
    }
  }

  return (
    <span className={`inline-flex min-w-0 flex-col items-start gap-1 ${className}`}>
      <button
        type="button"
        onClick={() => void download()}
        disabled={downloading}
        aria-busy={downloading || undefined}
        className="ui-focus-ring inline-flex min-h-8 items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 py-1 text-xs font-medium text-foreground transition-colors hover:bg-surface-hover disabled:cursor-wait disabled:opacity-70 [@media(pointer:coarse)]:min-h-11"
      >
        <svg
          aria-hidden="true"
          className="h-3.5 w-3.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 4v11m0 0-4-4m4 4 4-4M5 19h14" />
        </svg>
        {downloading ? t('temporaryWorkspace.downloading') : t('temporaryWorkspace.download')}
      </button>
      {error ? (
        <span role="alert" className="text-xs text-red-600 dark:text-red-400">
          {t('temporaryWorkspace.downloadFailed', { message: error })}
        </span>
      ) : null}
    </span>
  );
}

/**
 * The conversation's mark where a repository would be named: what a temporary
 * workspace keeps, said again whenever it is opened, beside the download.
 */
export function TemporaryWorkspaceBadge({ jobId }: { jobId: string }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const container = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    function onPointerDown(event: PointerEvent) {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') setOpen(false);
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  return (
    <span ref={container} className="relative inline-flex min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        title={t('temporaryWorkspace.details')}
        onClick={() => setOpen((value) => !value)}
        className="ui-focus-ring inline-flex min-w-0 items-center gap-1 rounded-md border border-amber-300/70 bg-amber-50 px-1.5 py-0.5 text-[11px] font-medium text-amber-800 transition-colors hover:bg-amber-100 dark:border-amber-400/30 dark:bg-amber-400/10 dark:text-amber-200 dark:hover:bg-amber-400/15"
      >
        <span className="truncate">{t('temporaryWorkspace.name')}</span>
      </button>
      {open ? (
        <div
          id={panelId}
          role="dialog"
          aria-label={t('temporaryWorkspace.details')}
          className="absolute left-0 top-full z-30 mt-1.5 w-72 max-w-[calc(100vw-2rem)] rounded-xl border border-gray-200 bg-white p-3 text-left shadow-card dark:border-white/10 dark:bg-gray-900"
        >
          <p className="text-xs leading-5 text-gray-600 dark:text-gray-300">
            {t('temporaryWorkspace.notice')}
          </p>
          <WorkspaceDownloadButton jobId={jobId} className="mt-2.5" />
        </div>
      ) : null}
    </span>
  );
}
