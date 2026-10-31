'use client';

// File Activity: what a conversation's own agent read, wrote, edited,
// searched and viewed, derived from the tool calls and results the platform
// already recorded across every turn and attempt. It is a new section of the
// main Context view, below its dashboard and events, and it reads on demand —
// never at send, settlement, cancellation or fork time.
//
// The wording keeps four kinds of number apart: a count of successfully
// recorded operations, cumulative recorded line churn (not the workspace's
// current Git diff), a bounded scan window (never all history), and a delta
// the record did not carry, which is a dash, never a zero.

import { useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  FILE_ACTIVITY_FILTERS,
  FILE_ACTIVITY_SORTS,
  getAgentFileActivity,
  getAgentFileOperations,
  type FileActivityApi,
  type FileActivityFilter,
  type FileActivityRowApi,
  type FileActivitySort,
  type FileOperationApi,
  type FileOperationsPageApi,
} from '@/lib/api/agents';
import { Select } from '@/components/ui/Select';
import type { TranslationKey } from '@/lib/i18n/translate';
import { compactCount, relativeTime } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from './ContextSections';
import {
  BASIS_LABEL,
  FILTER_LABEL,
  KIND_LABEL,
  KIND_MARK,
  SORT_LABEL,
  STATUS_LABEL,
  filterCount,
  lineChurn,
  operationChurn,
  operationKey,
  rowHasUnsettled,
  splitPath,
} from './fileActivityPresent';

/** Rows one page reads; the column scrolls, the totals stay the scan's own. */
const PAGE = 25;
/** A search is sent once typing has paused, as the other lists do. */
export const FILE_SEARCH_DEBOUNCE_MS = 250;

interface Query {
  sort: FileActivitySort;
  filter: FileActivityFilter;
  q: string;
}

const DEFAULT_QUERY: Query = { sort: 'active', filter: 'all', q: '' };

/** Read one page, and the pages after it as the reader asks. */
function useFileActivity(jobId: string, stateKey: string, query: Query) {
  const [loaded, setLoaded] = useState<{
    key: string;
    view: FileActivityApi | null;
    error: boolean;
  } | null>(null);
  const [more, setMore] = useState<FileActivityApi[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const current = loaded?.key === stateKey ? loaded : null;

  useEffect(() => {
    const controller = new AbortController();
    setMore([]);
    getAgentFileActivity(jobId, { ...query, limit: PAGE }, controller.signal).then(
      (view) => {
        if (!controller.signal.aborted) setLoaded({ key: stateKey, view, error: false });
      },
      () => {
        if (!controller.signal.aborted) setLoaded({ key: stateKey, view: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, stateKey, query.sort, query.filter, query.q, query]);

  const loadMore = async () => {
    const tail = more.length > 0 ? more[more.length - 1] : current?.view;
    if (!tail?.next_cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await getAgentFileActivity(jobId, {
        ...query,
        limit: PAGE,
        cursor: tail.next_cursor,
      });
      if (next) setMore((pages) => [...pages, next]);
    } finally {
      setLoadingMore(false);
    }
  };

  // Later pages repeat the same totals; the first page's are the scan's own.
  const pages = current?.view ? [current.view, ...more] : more;
  const rows = pages.flatMap((page) => page.files);
  return {
    view: current?.view ?? null,
    rows,
    error: current?.error ?? false,
    loading: current === null,
    hasMore: pages.length > 0 && Boolean(pages[pages.length - 1]?.next_cursor),
    loadMore,
    loadingMore,
  };
}

export function FileActivity({
  jobId,
  conversationKey,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  conversationKey: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const [query, setQuery] = useState<Query>(DEFAULT_QUERY);
  const [typed, setTyped] = useState('');
  useEffect(() => {
    if (typed === query.q) return undefined;
    const timer = window.setTimeout(
      () => setQuery((q) => ({ ...q, q: typed })),
      FILE_SEARCH_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [typed, query.q]);
  const stateKey = `${conversationKey}\u0000${query.sort}\u0000${query.filter}\u0000${query.q}`;
  const { view, rows, error, loading, hasMore, loadMore, loadingMore } = useFileActivity(
    jobId,
    stateKey,
    query,
  );
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const nowMs = view ? Date.parse(view.generated_at) : Date.now();

  const toggle = (path: string) =>
    setExpanded((open) => {
      const next = new Set(open);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });

  return (
    <section className="mt-6" aria-labelledby="file-activity-title">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 id="file-activity-title" className="text-sm font-semibold text-foreground">
          {t('files.title')}
        </h3>
        {view ? <Headline view={view} nowMs={nowMs} /> : null}
      </div>
      <p className="mt-1 text-xs text-muted">{t('files.note')}</p>
      <Controls
        query={query}
        typed={typed}
        onTyped={setTyped}
        onSort={(sort) => setQuery((q) => ({ ...q, sort }))}
        onFilter={(filter) => setQuery((q) => ({ ...q, filter }))}
        counts={view?.filter_counts ?? {}}
      />
      {error ? (
        <p className="mt-2 rounded-lg border border-border px-3 py-2 text-xs text-muted">
          {t('files.loadError')}
        </p>
      ) : loading ? (
        <p className="mt-2 px-3 py-2 text-xs text-muted">{t('common.loading')}</p>
      ) : rows.length === 0 ? (
        <p className="mt-2 rounded-lg border border-border px-3 py-2 text-xs text-muted">
          {query.q || query.filter !== 'all' ? t('files.noneMatching') : t('files.none')}
        </p>
      ) : (
        <ul
          aria-label={t('files.title')}
          className="mt-2 divide-y divide-border rounded-lg border border-border text-xs"
        >
          {rows.map((row) => (
            <FileRow
              key={row.path}
              row={row}
              jobId={jobId}
              conversationKey={conversationKey}
              open={expanded.has(row.path)}
              onToggle={() => toggle(row.path)}
              nowMs={nowMs}
              onOpenPoint={onOpenPoint}
              onOpenTurn={onOpenTurn}
            />
          ))}
        </ul>
      )}
      {hasMore ? (
        <button
          type="button"
          onClick={() => void loadMore()}
          disabled={loadingMore}
          className="ui-button ui-button-ghost mt-2 min-h-9 px-2.5 text-xs [@media(pointer:coarse)]:min-h-11"
        >
          {loadingMore ? t('files.loadingMore') : t('files.loadMore')}
        </button>
      ) : null}
      {view ? <Coverage view={view} nowMs={nowMs} /> : null}
    </section>
  );
}

/** The one-line summary: files, recorded churn, and what those numbers are. */
function Headline({ view, nowMs }: { view: FileActivityApi; nowMs: number }) {
  const { t, locale } = useLocale();
  const totals = view.totals;
  return (
    <p className="text-xs text-muted">
      <span className="tabular-nums text-foreground">
        {t('files.filesCount', { count: compactCount(totals.files) })}
      </span>
      {totals.added_unknown_files > 0 || totals.removed_unknown_files > 0 ? (
        <span title={t('files.churnUnknownTip')}>
          {' · '}
          <span className="tabular-nums text-emerald-600 dark:text-emerald-400">
            +{compactCount(totals.added)}
          </span>{' '}
          <span className="tabular-nums text-rose-600 dark:text-rose-400">
            −{compactCount(totals.removed)}
          </span>
          {' · '}
          {t('files.churnPartial')}
        </span>
      ) : (
        <span title={t('files.churnTip')}>
          {' · '}
          <span className="tabular-nums text-emerald-600 dark:text-emerald-400">
            +{compactCount(totals.added)}
          </span>{' '}
          <span className="tabular-nums text-rose-600 dark:text-rose-400">
            −{compactCount(totals.removed)}
          </span>
        </span>
      )}
      {totals.unknown_path_operations > 0 || totals.search_unknown_scope > 0 ? (
        <span>
          {' · '}
          {t('files.unattributed', {
            count: compactCount(totals.unknown_path_operations + totals.search_unknown_scope),
          })}
        </span>
      ) : null}
      <span className="sr-only">{relativeTime(view.generated_at, nowMs, locale)}</span>
    </p>
  );
}

/** Filters (with their counts), the path search and the sort. */
function Controls({
  query,
  typed,
  onTyped,
  onSort,
  onFilter,
  counts,
}: {
  query: Query;
  typed: string;
  onTyped: (value: string) => void;
  onSort: (sort: FileActivitySort) => void;
  onFilter: (filter: FileActivityFilter) => void;
  counts: Partial<Record<FileActivityFilter, number>>;
}) {
  const { t, locale } = useLocale();
  return (
    <div className="mt-2 grid gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
      <input
        type="search"
        value={typed}
        onChange={(event) => onTyped(event.target.value)}
        placeholder={t('files.searchPlaceholder')}
        aria-label={t('files.searchPlaceholder')}
        className="ui-focus-ring min-w-0 rounded-md border border-border bg-transparent px-2.5 py-1.5 text-xs text-foreground placeholder:text-muted"
      />
      <div className="flex flex-wrap items-center gap-1">
        <div role="group" aria-label={t('files.filter')} className="flex flex-wrap gap-1">
          {FILE_ACTIVITY_FILTERS.map((filter) => (
            <Chip
              key={filter}
              active={query.filter === filter}
              onClick={() => onFilter(filter)}
              label={t(FILTER_LABEL[filter])}
              count={filterCount(counts, filter)}
              testId={`file-filter-${filter}`}
            />
          ))}
        </div>
        <span className="ml-1 flex items-center gap-1 text-muted">
          <span className="whitespace-nowrap">{t('files.sort')}</span>
          <Select<FileActivitySort>
            value={query.sort}
            onValueChange={onSort}
            aria-label={t('files.sort')}
            className="min-w-[9rem]"
            options={FILE_ACTIVITY_SORTS.map((sort) => ({
              value: sort,
              label: t(SORT_LABEL[sort]),
            }))}
          />
        </span>
        <span className="sr-only" aria-live="polite">
          {t('files.filtered', { count: counts[query.filter] ?? 0, total: counts.all ?? 0 })}
        </span>
        <span className="sr-only">{locale}</span>
      </div>
    </div>
  );
}

function Chip({
  active,
  onClick,
  label,
  count,
  testId,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
  count: number;
  testId: string;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      data-testid={testId}
      className={`ui-focus-ring inline-flex items-center gap-1 whitespace-nowrap rounded-md border px-2 py-1 tabular-nums [@media(pointer:coarse)]:min-h-11 ${
        active
          ? 'border-accent bg-surface font-medium text-foreground'
          : 'border-border text-muted hover:bg-surface-hover'
      }`}
    >
      {label}
      <span className="text-[11px] opacity-80">{count.toLocaleString('en-US')}</span>
    </button>
  );
}

/** One file: type badge, path, its counts, recorded churn and last activity. */
function FileRow({
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
      <div className="flex min-w-0 flex-wrap items-start gap-2 px-3 py-2">
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
        <div className="min-w-0 flex-1 basis-64">
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
    <span className="ml-auto flex shrink-0 flex-wrap items-center gap-1.5 tabular-nums">
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
  const [more, setMore] = useState<FileOperationApi[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const key = `${conversationKey}\u0000${path}`;

  useEffect(() => {
    const controller = new AbortController();
    setMore([]);
    getAgentFileOperations(jobId, path, { limit: 40 }, controller.signal).then(
      (page) => {
        if (!controller.signal.aborted) setState({ key, page, error: false });
      },
      () => {
        if (!controller.signal.aborted) setState({ key, page: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, path, key]);

  const current = state?.key === key ? state : null;
  const operations = current?.page ? [...current.page.operations, ...more] : more;
  const hasEarlier =
    more.length > 0
      ? Boolean(more[more.length - 1] && current?.page?.has_earlier)
      : Boolean(current?.page?.has_earlier);
  const earlierCursor = more.length > 0 ? null : (current?.page?.next_cursor ?? null);

  const loadEarlier = async () => {
    if (!earlierCursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getAgentFileOperations(jobId, path, { limit: 40, cursor: earlierCursor });
      if (page) setMore((pages) => [...pages, ...page.operations]);
    } finally {
      setLoadingMore(false);
    }
  };

  if (current?.error) {
    return <p className="border-t border-border px-3 py-2 text-muted">{t('files.loadError')}</p>;
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

/** What the totals describe: the bounded window, or the honest cap note. */
function Coverage({ view, nowMs }: { view: FileActivityApi; nowMs: number }) {
  const { t, locale } = useLocale();
  const coverage = view.coverage;
  const first = coverage.window_first_at;
  const last = coverage.window_last_at;
  const windowArgs = {
    from: first ? relativeTime(first, nowMs, locale) : '',
    to: last ? relativeTime(last, nowMs, locale) : '',
  };
  const window = first && last ? t('files.coverage.window', windowArgs) : null;
  const scope = t('files.coverage.scope', {
    events: compactCount(coverage.events_scanned),
    turns: compactCount(coverage.turns),
    attempts: compactCount(coverage.attempts),
  });
  return (
    <p className="mt-1 text-[11px] text-muted">
      {coverage.events_capped ? t('files.coverage.capped') : null}
      {coverage.events_capped ? ' ' : null}
      {scope}
      {window ? ` ${window}` : null} {t('files.coverage.notGit')}
    </p>
  );
}

export type { TranslationKey };
