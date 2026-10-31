'use client';

// One file's row, its counts and recorded churn, and the operations it expands
// into. Split from the section so each module stays within the file budget and
// the row's own layout can be read on its own.

import { useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  getAgentFileOperations,
  type FileActivityRowApi,
  type FileOperationApi,
  type FileOperationsPageApi,
} from '@/lib/api/agents';
import { relativeTime } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from './ContextSections';
import {
  BASIS_LABEL,
  KIND_LABEL,
  KIND_MARK,
  STATUS_LABEL,
  lineChurn,
  operationChurn,
  operationKey,
  rowHasUnsettled,
  splitPath,
} from './fileActivityPresent';
import { OPERATIONS_PAGE } from './fileActivityLimits';

export function FileRow({
  row,
  jobId,
  conversationKey,
  open,
  onToggle,
  nowMs,
  onOpenPoint,
  onOpenTurn,
}: {
  row: FileActivityRowApi;
  jobId: string;
  conversationKey: string;
  open: boolean;
  onToggle: () => void;
  nowMs: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const parts = splitPath(row.path);
  return (
    <li className="min-w-0">
      <div className="flex min-w-0 flex-wrap items-start gap-x-2 gap-y-1 px-3 py-2">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={open}
          className="ui-focus-ring mt-0.5 shrink-0 rounded px-1 text-muted"
          aria-label={t(open ? 'files.collapse' : 'files.expand', { name: row.name })}
        >
          <span
            aria-hidden="true"
            className={`inline-block transition-transform ${open ? 'rotate-90' : ''}`}
          >
            ›
          </span>
        </button>
        <span
          className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold tabular-nums ${
            KIND_MARK[row.file_kind] ?? KIND_MARK.other
          }`}
          title={t('files.typeLabelTip')}
        >
          {row.type_label}
        </span>
        <div className="min-w-0 flex-1">
          <p className="break-words font-medium text-foreground" title={row.path}>
            {parts.directory ? <span className="text-muted">{parts.directory}</span> : null}
            <span>{parts.name}</span>
          </p>
          <Unsettled row={row} />
        </div>
        <RowSummary row={row} nowMs={nowMs} locale={locale} />
      </div>
      {open ? (
        <Operations
          jobId={jobId}
          conversationKey={conversationKey}
          path={row.path}
          nowMs={nowMs}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
        />
      ) : null}
    </li>
  );
}

/** The failed and unanswered operations a file carries, when it has any. */
function Unsettled({ row }: { row: FileActivityRowApi }) {
  const { t } = useLocale();
  if (!rowHasUnsettled(row)) return null;
  const unsettled = row.pending + row.not_recorded;
  return (
    <p className="mt-0.5 text-muted">
      {row.failed > 0 ? t('files.row.failed', { count: row.failed }) : null}
      {row.failed > 0 && unsettled > 0 ? ' · ' : null}
      {unsettled > 0 ? t('files.row.unsettled', { count: unsettled }) : null}
    </p>
  );
}

/** A file's counts, recorded churn and last activity, in one row. */
function RowSummary({
  row,
  nowMs,
  locale,
}: {
  row: FileActivityRowApi;
  nowMs: number;
  locale: string;
}) {
  const { t } = useLocale();
  // Only a file something wrote to has line churn; a read-only file has none,
  // and a pair of zeros would read as a measured result rather than absence.
  const churn = row.writes > 0 ? lineChurn(row) : null;
  return (
    <span className="flex w-full min-w-0 flex-wrap items-center gap-1.5 pl-7 tabular-nums sm:ml-auto sm:w-auto sm:shrink-0 sm:pl-0">
      <Count icon="●" value={row.reads} label={t('files.filter.read')} tone="read" />
      <Count icon="●" value={row.writes} label={t('files.filter.written')} tone="write" />
      {row.searches > 0 ? (
        <Count icon="⌕" value={row.searches} label={t('files.filter.searched')} tone="search" />
      ) : null}
      {row.images > 0 ? (
        <Count icon="▣" value={row.images} label={t('files.filter.images')} tone="image" />
      ) : null}
      {churn ? (
        <span
          className="whitespace-nowrap"
          title={churn.unknown ? t('files.churnUnknownTip') : t('files.churnTip')}
        >
          <span className="text-emerald-600 dark:text-emerald-400">{churn.added}</span>{' '}
          <span className="text-rose-600 dark:text-rose-400">{churn.removed}</span>
        </span>
      ) : null}
      <time dateTime={row.last_at ?? undefined} className="whitespace-nowrap text-muted">
        {row.last_at ? relativeTime(row.last_at, nowMs, locale) : t('files.noTime')}
      </time>
    </span>
  );
}

function Count({
  icon,
  value,
  label,
  tone,
}: {
  icon: string;
  value: number;
  label: string;
  tone: 'read' | 'write' | 'search' | 'image';
}) {
  if (value <= 0) return null;
  const colour = {
    read: 'bg-slate-500/15 text-slate-600 dark:bg-slate-400/20 dark:text-slate-300',
    write: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-300',
    search: 'bg-amber-500/15 text-amber-600 dark:text-amber-300',
    image: 'bg-pink-500/15 text-pink-600 dark:text-pink-300',
  }[tone];
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] ${colour}`}
      title={label}
    >
      <span aria-hidden="true">{icon}</span>
      {value.toLocaleString('en-US')}
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** One file's recorded operations, newest first, with their source steps. */
function Operations({
  jobId,
  conversationKey,
  path,
  nowMs,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  conversationKey: string;
  path: string;
  nowMs: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const [state, setState] = useState<{
    key: string;
    page: FileOperationsPageApi | null;
    error: boolean;
  } | null>(null);
  // Each later page is kept whole: its own cursor and ``has_earlier`` are what
  // reach page three. Flattening them into operations loses both.
  const [earlier, setEarlier] = useState<FileOperationsPageApi[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState(false);
  const [generation, setGeneration] = useState(0);
  const abortMore = useRef<AbortController | null>(null);
  const key = `${conversationKey}\u0000${path}`;

  useEffect(() => {
    const controller = new AbortController();
    setEarlier([]);
    setMoreError(false);
    getAgentFileOperations(jobId, path, { limit: OPERATIONS_PAGE }, controller.signal).then(
      (page) => {
        if (!controller.signal.aborted) setState({ key, page, error: false });
      },
      () => {
        if (!controller.signal.aborted) setState({ key, page: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, path, key, generation]);

  useEffect(() => {
    setEarlier([]);
    setMoreError(false);
    abortMore.current?.abort();
  }, [key, generation]);

  const current = state?.key === key ? state : null;
  const operations = [
    ...(current?.page?.operations ?? []),
    ...earlier.flatMap((page) => page.operations),
  ];
  const lastPage = earlier.length > 0 ? earlier[earlier.length - 1] : current?.page;
  const earlierCursor = lastPage?.next_cursor ?? null;
  const hasEarlier = Boolean(lastPage?.has_earlier);

  const retry = () => {
    setState(null);
    setGeneration((value) => value + 1);
  };

  const loadEarlier = async () => {
    if (!earlierCursor || loadingMore) return;
    const controller = new AbortController();
    abortMore.current?.abort();
    abortMore.current = controller;
    setLoadingMore(true);
    setMoreError(false);
    try {
      const page = await getAgentFileOperations(jobId, path, {
        limit: OPERATIONS_PAGE,
        cursor: earlierCursor,
      });
      if (controller.signal.aborted) return;
      if (page) setEarlier((pages) => [...pages, page]);
    } catch {
      if (!controller.signal.aborted) setMoreError(true);
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
    }
  };

  if (current?.error) {
    return (
      <p className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-muted">
        {t('files.loadError')}
        <button
          type="button"
          onClick={retry}
          className="ui-button ui-button-ghost min-h-8 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
        >
          {t('common.retry')}
        </button>
      </p>
    );
  }
  if (!current) {
    return <p className="border-t border-border px-3 py-2 text-muted">{t('common.loading')}</p>;
  }
  return (
    <div className="border-t border-border bg-surface/40 px-3 py-2">
      {operations.length === 0 ? (
        <p className="text-muted">{t('files.ops.none')}</p>
      ) : (
        <ol aria-label={t('files.ops.label', { name: path })} className="grid gap-1.5">
          {operations.map((operation) => (
            <OperationRow
              key={operationKey(operation)}
              operation={operation}
              nowMs={nowMs}
              onOpenPoint={onOpenPoint}
              onOpenTurn={onOpenTurn}
            />
          ))}
        </ol>
      )}
      {hasEarlier && earlierCursor ? (
        <button
          type="button"
          onClick={() => void loadEarlier()}
          disabled={loadingMore}
          className="ui-button ui-button-ghost mt-1 min-h-9 px-2.5 text-[11px] [@media(pointer:coarse)]:min-h-11"
        >
          {loadingMore ? t('files.loadingMore') : t('files.ops.earlier')}
        </button>
      ) : null}
      {moreError ? (
        <p className="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
          {t('files.moreError')}
          <button
            type="button"
            onClick={() => void loadEarlier()}
            className="ui-button ui-button-ghost min-h-8 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
          >
            {t('common.retry')}
          </button>
        </p>
      ) : null}
      {current.page ? (
        <p className="mt-1 text-muted">
          {t('files.ops.recorded', {
            shown: operations.length.toLocaleString(locale),
            total: current.page.total.toLocaleString(locale),
          })}
        </p>
      ) : null}
    </div>
  );
}

function OperationRow({
  operation,
  nowMs,
  onOpenPoint,
  onOpenTurn,
}: {
  operation: FileOperationApi;
  nowMs: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const churn = operationChurn(operation);
  const at = operation.at ? relativeTime(operation.at, nowMs, locale) : t('files.noTime');
  const step = operation.result_event_id ?? operation.event_id;
  return (
    <li
      className={`flex min-w-0 flex-wrap items-start gap-x-2 gap-y-0.5 rounded px-1 py-0.5 ${
        operation.superseded ? 'opacity-70' : ''
      }`}
    >
      <span className="w-14 shrink-0 whitespace-nowrap text-muted" title={operation.action}>
        {t(KIND_LABEL[operation.kind])}
      </span>
      <span className="min-w-0 flex-1 basis-48 break-words">
        <span className="text-foreground">{operation.tool || operation.action}</span>
        <span className="text-muted">
          {' · '}
          {t('files.ops.turn', { turn: operation.turn_no })}
          {operation.attempt_no > 1
            ? ` · ${t('files.ops.attempt', { n: operation.attempt_no })}`
            : ''}
          {' · '}
          {at}
        </span>
        {operation.patch_index !== null && operation.patch_files !== null ? (
          <span className="text-muted">
            {' · '}
            {t('files.ops.patchPart', {
              index: operation.patch_index,
              total: operation.patch_files,
            })}
          </span>
        ) : null}
        {operation.renamed_to ? (
          <span className="break-all text-muted">
            {' · '}
            {t('files.ops.renamedTo', { path: operation.renamed_to })}
          </span>
        ) : null}
        {operation.status !== 'succeeded' ? (
          <span className="text-amber-600 dark:text-amber-400">
            {' · '}
            {t(STATUS_LABEL[operation.status])}
          </span>
        ) : null}
        {operation.clipped ? (
          <span className="text-muted">
            {' · '}
            {t('files.ops.clipped')}
          </span>
        ) : null}
        {operation.subagent ? (
          <span className="text-muted">
            {' · '}
            {t('files.ops.subagent')}
          </span>
        ) : null}
        {operation.superseded ? (
          <span className="text-muted">
            {' · '}
            {t('files.ops.superseded')}
          </span>
        ) : null}
        {operation.path === null ? (
          <span className="text-muted">
            {' · '}
            {t('files.ops.unknownScope')} ({t(BASIS_LABEL[operation.path_basis])})
          </span>
        ) : null}
      </span>
      {churn ? (
        <span className="shrink-0 whitespace-nowrap tabular-nums">
          <span className="text-emerald-600 dark:text-emerald-400">{churn.added}</span>{' '}
          <span className="text-rose-600 dark:text-rose-400">{churn.removed}</span>
        </span>
      ) : null}
      <span className="ml-auto flex shrink-0 gap-1">
        <button
          type="button"
          onClick={() =>
            onOpenPoint({ jobId: operation.job_id, turnNo: operation.turn_no, eventId: step })
          }
          className="ui-button ui-button-ghost min-h-8 px-1.5 text-[11px] [@media(pointer:coarse)]:min-h-11"
        >
          {t('files.ops.step')}
        </button>
        <button
          type="button"
          onClick={() => onOpenTurn(operation.job_id, operation.turn_no)}
          className="ui-button ui-button-ghost min-h-8 px-1.5 text-[11px] [@media(pointer:coarse)]:min-h-11"
        >
          {t('files.ops.turnLink')}
        </button>
      </span>
    </li>
  );
}
