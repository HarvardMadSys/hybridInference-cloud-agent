'use client';

import { useId, useState } from 'react';
import { useT } from '@/components/providers';
import type { AgentJobApi } from '@/lib/api/agents';
import type { AgentJobState } from './types';
import {
  hasSettledTurn,
  recordedResultUrl,
  type RecordedFileSelection,
  type RecordedTurnOutputs,
} from './turnDeliverables';

export interface TurnDeliverablesProps {
  job: { id: string; state: AgentJobState | AgentJobApi['state'] | undefined };
  outputs?: RecordedTurnOutputs | null;
  onOpenFile: (selection: RecordedFileSelection) => void;
}

/** Compact actions for a settled turn's explicitly recorded outputs. */
export function TurnDeliverables({ job, outputs, onOpenFile }: TurnDeliverablesProps) {
  const t = useT();
  const detailsId = useId();
  const [expandedJobId, setExpandedJobId] = useState<string | null>(null);
  if (!hasSettledTurn(job.state) || !outputs || outputs.jobId !== job.id) return null;
  const prUrl = recordedResultUrl(outputs.prUrl);
  if (!outputs.files.length && !prUrl) return null;
  const expanded = expandedJobId === job.id;

  return (
    <section
      aria-label={t('turnResults.region')}
      data-recorded-job-id={job.id}
      className="mt-4 min-w-0 overflow-hidden rounded-xl border border-border bg-surface-raised"
    >
      {outputs.files.length ? (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-2.5">
            <span className="text-[13px] font-medium text-foreground">
              {t('turnResults.title')}
            </span>
            <span className="text-xs text-muted">
              {outputs.files.length === 1
                ? t('turnResults.file')
                : t('turnResults.files', { count: outputs.files.length })}
            </span>
          </div>
          <ul id={detailsId} className="divide-y divide-border">
            {outputs.files.map((file, index) => (
              <li key={file.path} hidden={!expanded && index >= 4}>
                <button
                  type="button"
                  aria-label={t('turnResults.openFile', { path: file.path })}
                  onClick={() => onOpenFile({ jobId: outputs.jobId, file })}
                  className="flex w-full min-w-0 items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-surface-hover focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-focus"
                >
                  <svg
                    aria-hidden="true"
                    className="h-4 w-4 shrink-0 text-muted"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.5}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8m-5-5v5h5m-5-5 5 5"
                    />
                  </svg>
                  <span className="min-w-0 flex-1 break-all font-mono text-xs text-foreground">
                    {file.path}
                  </span>
                  {file.add !== undefined || file.del !== undefined ? (
                    <span className="flex shrink-0 items-center gap-2 font-mono text-xs">
                      {file.add !== undefined ? (
                        <span
                          aria-label={t('turnResults.additions', { count: file.add })}
                          className="text-status-success"
                        >
                          +{file.add}
                        </span>
                      ) : null}
                      {file.del !== undefined ? (
                        <span
                          aria-label={t('turnResults.deletions', { count: file.del })}
                          className="text-status-error"
                        >
                          −{file.del}
                        </span>
                      ) : null}
                    </span>
                  ) : (
                    <span className="text-[11px] text-muted">
                      {t('turnResults.patchUnavailable')}
                    </span>
                  )}
                </button>
              </li>
            ))}
          </ul>
        </>
      ) : null}
      {outputs.files.length > 4 || prUrl ? (
        <div className="flex flex-wrap items-center gap-3 border-t border-border px-3 py-2.5">
          {outputs.files.length > 4 ? (
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={detailsId}
              onClick={() => setExpandedJobId(expanded ? null : job.id)}
              className="rounded-md px-1 py-0.5 text-xs text-muted hover:text-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
            >
              {expanded
                ? t('turnResults.showLess')
                : t('turnResults.showAll', { count: outputs.files.length })}
            </button>
          ) : null}
          {prUrl ? (
            <a
              href={prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-auto rounded-md px-1 py-0.5 text-xs font-medium text-accent hover:underline focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus"
            >
              {t('turnResults.pullRequest')}
            </a>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
