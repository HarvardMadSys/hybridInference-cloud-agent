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

import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  FILE_ACTIVITY_FILTERS,
  FILE_ACTIVITY_SORTS,
  getAgentFileActivity,
  type FileActivityApi,
  type FileActivityFilter,
  type FileActivitySort,
} from '@/lib/api/agents';
import { Select } from '@/components/ui/Select';
import type { TranslationKey } from '@/lib/i18n/translate';
import { compactCount, relativeTime } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from './ContextSections';
import { FileRow } from './FileActivityRows';
import { FILTER_LABEL, SORT_LABEL, filterCount } from './fileActivityPresent';

/** Rows one page reads; the column scrolls, the totals stay the scan's own. */
const PAGE = 25;
/** A search is sent once typing has paused, as the other lists do. */
export const FILE_SEARCH_DEBOUNCE_MS = 250;
/** How often a shown section re-reads while its conversation has a running turn. */
export const ACTIVE_REFRESH_MS = 60_000;

interface Query {
  sort: FileActivitySort;
  filter: FileActivityFilter;
  q: string;
}

const DEFAULT_QUERY: Query = { sort: 'active', filter: 'all', q: '' };

/** Which read a state belongs to: the conversation, the filters, and a tick. */
interface LoadKey {
  key: string;
  generation: number;
}

/**
 * Read the first page, and later pages as the reader asks.
 *
 * Every request is fenced twice: an effect-local `AbortController` cancels it
 * when the query or conversation changes or the section unmounts, and a
 * generation token makes a late response a no-op even if the transport ignored
 * the abort. A failed page is surfaced with a retry rather than swallowed.
 * `refreshTick` is bumped by the parent when the conversation settles, so a
 * running turn's new operations and a pending call that later completed become
 * visible without polling anything.
 */
function useFileActivity(
  jobId: string,
  conversationKey: string,
  query: Query,
  refreshTick: number,
) {
  const [loadKey, setLoadKey] = useState<LoadKey>({ key: conversationKey, generation: 0 });
  const [loaded, setLoaded] = useState<{
    key: string;
    generation: number;
    view: FileActivityApi | null;
    error: boolean;
  } | null>(null);
  const [more, setMore] = useState<FileActivityApi[]>([]);
  const [pagesError, setPagesError] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const stateKey = `${conversationKey}\u0000${query.sort}\u0000${query.filter}\u0000${query.q}`;
  const current =
    loaded && loaded.key === stateKey && loaded.generation === loadKey.generation ? loaded : null;
  const abortPages = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    const generation = loadKey.generation;
    setMore([]);
    setPagesError(false);
    getAgentFileActivity(jobId, { ...query, limit: PAGE }, controller.signal).then(
      (view) => {
        if (controller.signal.aborted || generation !== loadKey.generation) return;
        setLoaded({ key: stateKey, generation, view, error: false });
      },
      () => {
        if (controller.signal.aborted || generation !== loadKey.generation) return;
        setLoaded({ key: stateKey, generation, view: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, stateKey, query, loadKey.generation, refreshTick]);

  // A refreshed first page replaces the pages read from the older scan.
  useEffect(() => {
    setMore([]);
    setPagesError(false);
    abortPages.current?.abort();
  }, [refreshTick]);

  const retry = useCallback(() => {
    setLoaded(null);
    setLoadKey((value) => ({ key: conversationKey, generation: value.generation + 1 }));
  }, [conversationKey]);

  const loadMore = useCallback(async () => {
    const tail = more.length > 0 ? more[more.length - 1] : current?.view;
    if (!tail?.next_cursor || loadingMore) return;
    const generation = loadKey.generation;
    const controller = new AbortController();
    abortPages.current?.abort();
    abortPages.current = controller;
    setLoadingMore(true);
    setPagesError(false);
    try {
      const next = await getAgentFileActivity(jobId, {
        ...query,
        limit: PAGE,
        cursor: tail.next_cursor,
      });
      // A page that resolved after the query changed belongs to the old list.
      if (controller.signal.aborted || generation !== loadKey.generation) return;
      if (!next) return;
      setMore((pages) => [...pages, next]);
    } catch {
      if (!controller.signal.aborted && generation === loadKey.generation) setPagesError(true);
    } finally {
      if (generation === loadKey.generation) setLoadingMore(false);
    }
  }, [jobId, more, current, query, loadKey.generation, loadingMore]);

  // Later pages repeat the same totals; the first page's are the scan's own.
  const pages = current?.view ? [current.view, ...more] : more;
  const rows = pages.flatMap((page) => page.files);
  const lastPage = pages[pages.length - 1];
  return {
    view: current?.view ?? null,
    rows,
    error: current?.error ?? false,
    loading: current === null,
    pagesError,
    retry,
    hasMore: Boolean(lastPage?.next_cursor),
    loadMore,
    loadingMore,
  };
}

export function FileActivity({
  jobId,
  conversationKey,
  onOpenPoint,
  onOpenTurn,
  refreshTick = 0,
  active = false,
}: {
  jobId: string;
  conversationKey: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
  /** Bumped by the page when the conversation's own view is read again. */
  refreshTick?: number;
  /** A turn is running: read again on a slow clock, never faster than a minute. */
  active?: boolean;
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
  // A running turn adds operations; a slow re-read keeps the section current
  // without polling, without touching the conversation, and only while shown.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => setTick((value) => value + 1), ACTIVE_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [active]);
  const { view, rows, error, loading, pagesError, retry, hasMore, loadMore, loadingMore } =
    useFileActivity(jobId, conversationKey, query, refreshTick + tick);
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
        <p className="mt-2 flex flex-wrap items-center gap-2 rounded-lg border border-border px-3 py-2 text-xs text-muted">
          {t('files.loadError')}
          <button
            type="button"
            onClick={retry}
            className="ui-button ui-button-ghost min-h-8 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
          >
            {t('common.retry')}
          </button>
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
      {pagesError ? (
        <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
          {t('files.moreError')}
          <button
            type="button"
            onClick={() => void loadMore()}
            className="ui-button ui-button-ghost min-h-8 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
          >
            {t('common.retry')}
          </button>
        </p>
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
