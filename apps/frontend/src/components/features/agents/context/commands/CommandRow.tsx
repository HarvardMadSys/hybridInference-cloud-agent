'use client';

// One command execution: its status, command text, timing, turn/step and
// background mark, and — when expanded — the recorded cwd, exit code, output
// and the navigation back to the step or turn it came from. Long text stays
// in a <pre> of React text nodes; nothing is ever set as HTML.

import { useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import { getEventContent, type CommandExecutionApi } from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import type { OpenPoint, OpenTurn } from '../ContextSections';
import { OUTPUT_CLIP, clip, durationLabel, statusTone } from './commandsPresent';

const STATUS_LABEL: Record<CommandExecutionApi['status'], TranslationKey> = {
  running: 'executions.status.running',
  completed: 'executions.status.completed',
  failed: 'executions.status.failed',
  unknown: 'executions.status.unknown',
};

export function CommandRow({
  execution,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  execution: CommandExecutionApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const [open, setOpen] = useState(false);
  const tone = statusTone(execution.status);
  const duration = durationLabel(execution.duration_ms);
  const durationTip =
    execution.duration_basis === 'events' ? t('executions.duration.eventsTip') : undefined;
  return (
    <li className="min-w-0">
      <button
        type="button"
        aria-expanded={open}
        aria-label={t(open ? 'executions.collapse' : 'executions.expand', {
          command: clip(execution.command, 60),
        })}
        onClick={() => setOpen((value) => !value)}
        className="ui-focus-ring flex w-full min-w-0 items-start gap-2 rounded-md px-2 py-1.5 text-left text-xs hover:bg-surface-hover"
      >
        <span aria-hidden="true" className={`mt-0.5 shrink-0 font-mono ${tone.className}`}>
          {tone.glyph}
        </span>
        <span className="min-w-0 flex-1">
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className="truncate font-mono text-foreground">
              {clip(execution.command, 160)}
            </span>
            {execution.background ? (
              <span className="shrink-0 rounded bg-teal-500/15 px-1 py-0.5 text-[10px] text-teal-700 dark:text-teal-300">
                {t('executions.background')}
              </span>
            ) : null}
            {execution.command_truncated ? (
              <span className="shrink-0 text-[10px] text-muted">
                {t('executions.details.commandTruncated')}
              </span>
            ) : null}
          </span>
          <span className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-muted">
            <span>{t(STATUS_LABEL[execution.status])}</span>
            {execution.exit_code != null ? (
              <span className="tabular-nums">
                {t('executions.exit', { code: execution.exit_code })}
              </span>
            ) : null}
            {execution.tool_error ? <span>{t('executions.toolError')}</span> : null}
            {duration ? (
              <span
                className="tabular-nums text-foreground"
                title={durationTip}
                aria-label={durationTip}
              >
                {duration}
              </span>
            ) : (
              <span>{t('executions.duration.none')}</span>
            )}
            <span
              title={t('executions.turnAttemptTip', {
                turn: execution.turn_no,
                attempt: execution.attempt_no,
              })}
            >
              {t('executions.turnAttempt', {
                turn: execution.turn_no,
                attempt: execution.attempt_no,
              })}
            </span>
            {execution.at ? (
              <time dateTime={execution.at}>
                {new Date(execution.at).toLocaleTimeString(locale, {
                  hour: '2-digit',
                  minute: '2-digit',
                  second: '2-digit',
                })}
              </time>
            ) : null}
            {execution.superseded ? <span>{t('executions.superseded')}</span> : null}
          </span>
        </span>
        <span className="shrink-0 font-mono text-[11px] text-muted">{execution.program}</span>
      </button>
      {open ? (
        <CommandDetails
          key={`${jobId}:${execution.id}:${execution.result_event_id ?? 'none'}`}
          execution={execution}
          jobId={jobId}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
        />
      ) : null}
    </li>
  );
}

function CommandDetails({
  execution,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  execution: CommandExecutionApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  // Detail state belongs to one result event. When a refresh gives this same
  // execution a new result, the old text and offset are dropped rather than
  // shown against the new outcome or sent as its continuation.
  const resultKey = `${jobId}:${execution.id}:${execution.result_event_id ?? 'none'}`;
  const [full, setFull] = useState<string | null>(null);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const fetchTicket = useRef(0);
  const command = execution.command;
  const output = full ?? clip(execution.output_preview, OUTPUT_CLIP);
  // Whether more of the stored result exists beyond what is shown, by the
  // event's own total when a page has been read, or the preview flag before.
  const canLoad =
    execution.result_event_id != null &&
    (nextOffset !== null || (full === null && execution.output_truncated));

  const load = (offset: number) => {
    if (execution.result_event_id == null) return;
    const ticket = ++fetchTicket.current;
    const key = resultKey;
    setLoading(true);
    setFailed(false);
    getEventContent(jobId, execution.result_event_id, offset)
      .then((content) => applyPage(content))
      .catch(() => {
        if (ticket === fetchTicket.current && key === resultKey) setFailed(true);
      })
      .finally(() => {
        if (ticket === fetchTicket.current && key === resultKey) setLoading(false);
      });

    function applyPage(content: Awaited<ReturnType<typeof getEventContent>>) {
      // A different result, or a newer fetch, owns the panel now.
      if (ticket !== fetchTicket.current || key !== resultKey) return;
      if (!content) {
        setFailed(true);
        return;
      }
      setFull((previous) =>
        offset === 0 || previous === null ? content.text : previous + content.text,
      );
      setNextOffset(content.next_offset);
    }
  };

  return (
    <div
      role="group"
      aria-label={t('executions.details.fields')}
      className="mb-1 ml-5 rounded-lg border border-border bg-surface-subtle p-2 text-xs"
    >
      <dl className="grid gap-1.5">
        <div className="min-w-0">
          <dt className="text-[11px] text-muted">{t('executions.details.command')}</dt>
          <dd className="mt-0.5">
            <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-foreground">
              {command}
            </pre>
          </dd>
        </div>
        {execution.cwd ? (
          <div className="min-w-0">
            <dt className="text-[11px] text-muted">{t('executions.details.cwd')}</dt>
            <dd className="mt-0.5 break-all font-mono text-[11px] text-foreground">
              {execution.cwd}
            </dd>
          </div>
        ) : null}
        <div className="min-w-0">
          <dt className="text-[11px] text-muted">{t('executions.details.result')}</dt>
          <dd className="mt-0.5">
            <span className="mr-2 text-[11px] text-muted">
              {execution.exit_code != null
                ? t('executions.exit', { code: execution.exit_code })
                : t('executions.noExit')}
              {' · '}
              {t('executions.details.characters', {
                count: execution.output_chars.toLocaleString(locale),
              })}
            </span>
            {output ? (
              <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-foreground">
                {output}
              </pre>
            ) : (
              <p className="text-muted">{t('executions.details.noOutput')}</p>
            )}
            {execution.output_truncated && nextOffset === null ? (
              <p className="mt-1 text-[11px] text-muted">{t('executions.details.truncated')}</p>
            ) : null}
            {canLoad ? (
              <button
                type="button"
                onClick={() => load(nextOffset ?? 0)}
                disabled={loading}
                className="ui-button ui-button-ghost mt-1 min-h-8 px-2 text-[11px]"
              >
                {loading
                  ? t('executions.details.loading')
                  : nextOffset !== null
                    ? t('executions.details.loadMore')
                    : t('executions.details.loadFull')}
              </button>
            ) : null}
            {failed ? (
              <p role="alert" className="mt-1 text-[11px] text-rose-600 dark:text-rose-400">
                {t('executions.details.loadFailed')}
              </p>
            ) : null}
          </dd>
        </div>
      </dl>
      <div className="mt-2 flex flex-wrap gap-1">
        <button
          type="button"
          onClick={() =>
            onOpenPoint({
              jobId: execution.job_id,
              turnNo: execution.turn_no,
              eventId: execution.event_id,
            })
          }
          className="ui-button ui-button-secondary min-h-8 px-2 text-[11px]"
        >
          {t('executions.openStep')}
        </button>
        <button
          type="button"
          onClick={() => onOpenTurn(execution.job_id, execution.turn_no)}
          className="ui-button ui-button-ghost min-h-8 px-2 text-[11px]"
        >
          {t('executions.openTurn')}
        </button>
      </div>
    </div>
  );
}
