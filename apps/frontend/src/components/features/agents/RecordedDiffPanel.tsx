'use client';

import { useT } from '@/components/providers';
import type { RecordedFileSelection } from './turnDeliverables';

const LINE_STYLE = {
  hunk: 'bg-surface-muted text-muted',
  ctx: 'text-foreground',
  add: 'bg-emerald-50 text-status-success dark:bg-emerald-500/10',
  del: 'bg-red-50 text-status-error dark:bg-red-500/10',
} as const;

/** Recorded result review never substitutes the shared workspace's current diff. */
export function RecordedDiffPanel({
  selection,
  turnNo,
  onClose,
}: {
  selection: RecordedFileSelection;
  turnNo: number;
  onClose: () => void;
}) {
  const t = useT();
  return (
    <section className="space-y-3" aria-label={t('turnResults.recordedTurn', { number: turnNo })}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-foreground">
          {t('turnResults.recordedTurn', { number: turnNo })}
        </h2>
        <button type="button" className="ui-button ui-button-secondary" onClick={onClose}>
          {t('turnResults.backToWorkspace')}
        </button>
      </div>
      <p className="text-xs text-muted">{t('turnResults.recordedHint')}</p>
      <div className="ui-card overflow-hidden" data-recorded-job-id={selection.jobId}>
        <div className="border-b border-border px-4 py-3 font-mono text-xs text-foreground break-all">
          {selection.file.path}
        </div>
        <pre className="max-h-[34rem] overflow-auto font-mono text-xs leading-relaxed" tabIndex={0}>
          <code>
            {selection.file.lines.map((line, index) => (
              <span key={index} className={`block min-w-max px-4 ${LINE_STYLE[line.marker]}`}>
                {line.text || ' '}
              </span>
            ))}
          </code>
        </pre>
      </div>
    </section>
  );
}
