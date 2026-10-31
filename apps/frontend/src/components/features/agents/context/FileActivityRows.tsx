'use client';

// One file's row, its counts and recorded churn, and the operations it expands
// into. Split from the section so each module stays within the file budget and
// the row's own layout can be read on its own.

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import type { Translator } from '@/lib/i18n/translate';
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
  revision,
}: {
  row: FileActivityRowApi;
  jobId: string;
  conversationKey: string;
  open: boolean;
  onToggle: () => void;
  nowMs: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
  /** The section's refresh revision: an expanded file re-reads with it. */
  revision?: number;
}) {
  const { t, locale } = useLocale();
  const parts = splitPath(row.path);
  return (
    <li
      // Alternating rows, like the reference list: the stripe is what lets the
      // eye follow one file across its counts without a rule on every row.
      // ``shrink-0`` is what makes the bounded list scroll: a flex column would
      // otherwise squeeze many loaded or expanded children to fit 320px.
      className={`min-w-0 shrink-0 rounded-md [&:nth-child(even)]:bg-surface-muted/60 ${
        open ? 'bg-surface-hover' : ''
      }`}
      data-open={open ? 'true' : 'false'}
    >
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        title={row.path}
        aria-label={t(open ? 'files.collapse' : 'files.expand', { name: row.name })}
        className="ui-focus-ring flex w-full min-w-0 items-center gap-1.5 rounded-md px-1.5 py-1 text-left hover:bg-surface-hover [@media(max-width:380px)]:flex-wrap"
      >
        <span
          aria-hidden="true"
          className={`w-3 shrink-0 text-[10px] text-muted transition-transform ${
            open ? 'rotate-90' : ''
          }`}
        >
          ›
        </span>
        <span
          aria-hidden="true"
          className={`inline-flex h-4 min-w-6 shrink-0 items-center justify-center rounded px-1 text-[9px] font-bold leading-none ${
            KIND_MARK[row.file_kind] ?? KIND_MARK.other
          }`}
        >
          {row.type_label}
        </span>
        {/* The directory yields space before the name does, so a narrow row
            still reads as a file name with a shortened parent, never as the
            front of a path with its name cut off. */}
        <span className="flex min-w-0 flex-1 items-baseline">
          {parts.directory ? (
            <span className="min-w-0 flex-1 truncate text-muted">{parts.directory}</span>
          ) : null}
          <span className="min-w-0 shrink-0 truncate font-medium text-foreground">
            {parts.name}
          </span>
        </span>
        <RowSummary row={row} nowMs={nowMs} locale={locale} />
      </button>
      {open ? (
        <Operations
          jobId={jobId}
          conversationKey={conversationKey}
          path={row.path}
          nowMs={nowMs}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
          revision={revision}
        />
      ) : null}
    </li>
  );
}

/** A file's counts, recorded churn and last activity, inline on its row. */
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
  const unsettled = row.pending + row.not_recorded;
  return (
    <span className="flex shrink-0 items-center gap-1.5 tabular-nums">
      {rowHasUnsettled(row) ? (
        <span
          className="text-amber-600 dark:text-amber-400"
          title={
            row.failed > 0
              ? t('files.row.failed', { count: row.failed })
              : t('files.row.unsettled', { count: unsettled })
          }
        >
          <span aria-hidden="true">!</span>
          <span className="sr-only">
            {row.failed > 0
              ? t('files.row.failed', { count: row.failed })
              : t('files.row.unsettled', { count: unsettled })}
          </span>
        </span>
      ) : null}
      <Count icon="●" value={row.reads} label={t('files.filter.read')} tone="read" />
      <Count icon="●" value={row.writes} label={t('files.filter.written')} tone="write" />
      {row.searches > 0 ? (
        <Count icon="●" value={row.searches} label={t('files.filter.searched')} tone="search" />
      ) : null}
      {row.images > 0 ? (
        <Count icon="●" value={row.images} label={t('files.filter.images')} tone="image" />
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
      <time
        dateTime={row.last_at ?? undefined}
        className="whitespace-nowrap text-[10px] text-muted"
      >
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
      className={`inline-flex h-4 min-w-4 items-center justify-center gap-0.5 rounded-full px-1 text-[10px] font-semibold ${colour}`}
      title={label}
    >
      <span aria-hidden="true" className="text-[7px]">
        {icon}
      </span>
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
  revision = 0,
}: {
  jobId: string;
  conversationKey: string;
  path: string;
  nowMs: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
  revision?: number;
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
  const [moreError, setMoreError] = useState(false);
  const [generation, setGeneration] = useState(0);
  //: The page-request generation and the controller that owns the live page
  //: read. Both are refs so a late request compares against the current value
  //: rather than the one its closure captured.
  const pageGeneration = useRef(0);
  const pageAbort = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState<{ generation: number; on: boolean }>({
    generation: -1,
    on: false,
  });
  const key = `${conversationKey}\u0000${path}`;

  /** Replace the list: stop the live page read and clear its loading state. */
  const replace = useCallback(() => {
    pageGeneration.current += 1;
    pageAbort.current?.abort();
    pageAbort.current = null;
    setEarlier([]);
    setMoreError(false);
    setLoading({ generation: pageGeneration.current, on: false });
    return pageGeneration.current;
  }, []);

  useEffect(() => {
    const mine = replace();
    const controller = new AbortController();
    const listController = controller;
    getAgentFileOperations(jobId, path, { limit: OPERATIONS_PAGE }, listController.signal).then(
      (page) => {
        if (listController.signal.aborted || pageGeneration.current !== mine) return;
        setState({ key, page, error: false });
      },
      () => {
        if (listController.signal.aborted || pageGeneration.current !== mine) return;
        setState({ key, page: null, error: true });
      },
    );
    return () => {
      listController.abort();
      // A page read in flight when this row collapses or unmounts is cancelled.
      pageAbort.current?.abort();
      pageAbort.current = null;
    };
  }, [jobId, path, key, generation, revision, replace]);

  const current = state?.key === key ? state : null;
  const operations = [
    ...(current?.page?.operations ?? []),
    ...earlier.flatMap((page) => page.operations),
  ];
  const lastPage = earlier.length > 0 ? earlier[earlier.length - 1] : current?.page;
  const earlierCursor = lastPage?.next_cursor ?? null;
  const hasEarlier = Boolean(lastPage?.has_earlier);
  const loadingMore = loading.generation === pageGeneration.current && loading.on;

  const retry = () => {
    setState(null);
    setGeneration((value) => value + 1);
  };

  const loadEarlier = async () => {
    if (!earlierCursor || loadingMore) return;
    const mine = pageGeneration.current;
    const controller = new AbortController();
    pageAbort.current?.abort();
    pageAbort.current = controller;
    setLoading({ generation: mine, on: true });
    setMoreError(false);
    try {
      const page = await getAgentFileOperations(
        jobId,
        path,
        { limit: OPERATIONS_PAGE, cursor: earlierCursor },
        controller.signal,
      );
      if (controller.signal.aborted || pageGeneration.current !== mine) return;
      if (page) setEarlier((pages) => [...pages, page]);
    } catch {
      if (!controller.signal.aborted && pageGeneration.current === mine) setMoreError(true);
    } finally {
      // Only the request that still owns this list may clear its loading flag.
      if (pageGeneration.current === mine) {
        setLoading({ generation: mine, on: false });
        if (pageAbort.current === controller) pageAbort.current = null;
      }
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
    <div className="mb-1 ml-4 mr-1 border-l-2 border-border py-1 pl-2">
      {operations.length === 0 ? (
        <p className="text-[11px] text-muted">{t('files.ops.none')}</p>
      ) : (
        <ol aria-label={t('files.ops.label', { name: path })} className="grid gap-px">
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
      <p className="flex flex-wrap items-center gap-2 text-[10px] text-muted">
        {current.page ? (
          <span>
            {t('files.ops.recorded', {
              shown: operations.length.toLocaleString(locale),
              total: current.page.total.toLocaleString(locale),
            })}
          </span>
        ) : null}
        {hasEarlier && earlierCursor ? (
          <button
            type="button"
            onClick={() => void loadEarlier()}
            disabled={loadingMore}
            className="ui-button ui-button-ghost min-h-6 px-1.5 text-[10px] [@media(pointer:coarse)]:min-h-11"
          >
            {loadingMore ? t('files.loadingMore') : t('files.ops.earlier')}
          </button>
        ) : null}
        {moreError ? (
          <>
            <span>{t('files.moreError')}</span>
            <button
              type="button"
              onClick={() => void loadEarlier()}
              className="ui-button ui-button-ghost min-h-6 px-1.5 text-[10px] [@media(pointer:coarse)]:min-h-11"
            >
              {t('common.retry')}
            </button>
          </>
        ) : null}
      </p>
    </div>
  );
}

/** What a compact operation row cannot show inline, for its tooltip. */
function operationDetail(operation: FileOperationApi, t: Translator): string {
  const parts: string[] = [];
  if (operation.attempt_no > 1) {
    parts.push(t('files.ops.attempt', { n: operation.attempt_no }));
  }
  if (operation.patch_index !== null && operation.patch_files !== null) {
    parts.push(
      t('files.ops.patchPart', {
        index: operation.patch_index,
        total: operation.patch_files,
      }),
    );
  }
  if (operation.renamed_to) {
    parts.push(t('files.ops.renamedTo', { path: operation.renamed_to }));
  }
  if (operation.clipped) parts.push(t('files.ops.clipped'));
  if (operation.subagent) parts.push(t('files.ops.subagent'));
  if (operation.superseded) parts.push(t('files.ops.superseded'));
  if (operation.path === null) {
    parts.push(`${t('files.ops.unknownScope')} (${t(BASIS_LABEL[operation.path_basis])})`);
  }
  return parts.join(' · ');
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
      className={`flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded px-1 py-0.5 text-[11px] hover:bg-surface-hover ${
        operation.superseded ? 'opacity-70' : ''
      }`}
      data-status={operation.status}
    >
      <span className="w-9 shrink-0 text-muted" title={operation.action}>
        {t(KIND_LABEL[operation.kind])}
      </span>
      <span
        className="max-w-[9rem] shrink-0 truncate rounded-full border border-border px-1.5 text-[10px] text-muted"
        title={operation.tool || operation.action}
      >
        {operation.tool || operation.action}
      </span>
      <span
        className="min-w-0 flex-1 truncate text-muted"
        title={operationDetail(operation, t) || undefined}
      >
        {t('files.ops.turn', { turn: operation.turn_no })}
        {operation.attempt_no > 1
          ? ` · ${t('files.ops.attempt', { n: operation.attempt_no })}`
          : ''}
        {' · '}
        {at}
        {operation.status !== 'succeeded' ? (
          <span className="text-amber-600 dark:text-amber-400">
            {' · '}
            {t(STATUS_LABEL[operation.status])}
          </span>
        ) : null}
      </span>
      {churn ? (
        <span className="shrink-0 whitespace-nowrap tabular-nums">
          <span className="text-emerald-600 dark:text-emerald-400">{churn.added}</span>{' '}
          <span className="text-rose-600 dark:text-rose-400">{churn.removed}</span>
        </span>
      ) : null}
      <span className="ml-auto flex shrink-0 gap-0.5">
        <button
          type="button"
          onClick={() =>
            onOpenPoint({ jobId: operation.job_id, turnNo: operation.turn_no, eventId: step })
          }
          className="ui-focus-ring rounded px-1 text-[10px] text-muted hover:bg-surface-raised hover:text-foreground [@media(pointer:coarse)]:min-h-11"
        >
          {t('files.ops.step')}
        </button>
        <button
          type="button"
          onClick={() => onOpenTurn(operation.job_id, operation.turn_no)}
          className="ui-focus-ring rounded px-1 text-[10px] text-muted hover:bg-surface-raised hover:text-foreground [@media(pointer:coarse)]:min-h-11"
        >
          {t('files.ops.turnLink')}
        </button>
      </span>
    </li>
  );
}
