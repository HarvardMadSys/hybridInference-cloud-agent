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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  FILE_ACTIVITY_FILTERS,
  FILE_ACTIVITY_SORTS,
  getAgentFileActivity,
  type FileActivityApi,
  type FileActivityFilter,
  type FileActivityRowApi,
  type FileActivitySort,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import { compactCount } from '@/components/features/settings/usagePresent';
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

/**
 * Read the first page, and later pages as the reader asks.
 *
 * Every request is fenced by a generation ref that advances whenever the
 * conversation, the filters, a refresh or a retry replaces the list, and by an
 * `AbortController` per request that is aborted on that same change and on
 * unmount. A late response therefore cannot append an older list's page, and
 * an old `finally` cannot clear a newer request's loading state. Failures are
 * surfaced with a retry rather than swallowed; `refreshTick` re-reads when the
 * page's own Context view is read again.
 */
function useFileActivity(
  jobId: string,
  conversationKey: string,
  query: Query,
  refreshTick: number,
) {
  //: Advances on every replacement; compared by identity, never by closure.
  const generation = useRef(0);
  const listAbort = useRef<AbortController | null>(null);
  const pagesAbort = useRef<AbortController | null>(null);
  const [loaded, setLoaded] = useState<{
    key: string;
    generation: number;
    view: FileActivityApi | null;
    error: boolean;
  } | null>(null);
  const [more, setMore] = useState<{ generation: number; pages: FileActivityApi[] }>({
    generation: -1,
    pages: [],
  });
  const [pagesError, setPagesError] = useState(false);
  const [retryCount, setRetryCount] = useState(0);
  const [loadingMore, setLoadingMore] = useState<{ generation: number; on: boolean }>({
    generation: -1,
    on: false,
  });
  const stateKey = `${conversationKey}\u0000${query.sort}\u0000${query.filter}\u0000${query.q}`;
  const current = loaded && loaded.key === stateKey ? loaded : null;
  const pages = useMemo(() => (more.generation === generation.current ? more.pages : []), [more]);
  const loading = loadingMore.generation === generation.current && loadingMore.on;

  useEffect(() => {
    // A new query, conversation, refresh or retry replaces the whole list.
    generation.current += 1;
    const mine = generation.current;
    listAbort.current?.abort();
    pagesAbort.current?.abort();
    const controller = new AbortController();
    listAbort.current = controller;
    setMore({ generation: mine, pages: [] });
    setPagesError(false);
    setLoadingMore({ generation: mine, on: false });
    getAgentFileActivity(jobId, { ...query, limit: PAGE }, controller.signal).then(
      (view) => {
        if (controller.signal.aborted || generation.current !== mine) return;
        setLoaded({ key: stateKey, generation: mine, view, error: false });
      },
      () => {
        if (controller.signal.aborted || generation.current !== mine) return;
        setLoaded({ key: stateKey, generation: mine, view: null, error: true });
      },
    );
    return () => {
      controller.abort();
      // A next page in flight belongs to the list this effect replaced.
      pagesAbort.current?.abort();
      pagesAbort.current = null;
    };
  }, [jobId, stateKey, query, refreshTick, retryCount]);

  const retry = useCallback(() => {
    // A new generation replaces the failed list; the effect below re-reads.
    generation.current += 1;
    setLoaded(null);
    setPagesError(false);
    setRetryCount((value) => value + 1);
  }, []);

  const loadMore = useCallback(async () => {
    const tail = pages.length > 0 ? pages[pages.length - 1] : current?.view;
    if (!tail?.next_cursor || loading) return;
    const mine = generation.current;
    const controller = new AbortController();
    pagesAbort.current?.abort();
    pagesAbort.current = controller;
    setLoadingMore({ generation: mine, on: true });
    setPagesError(false);
    try {
      const next = await getAgentFileActivity(
        jobId,
        { ...query, limit: PAGE, cursor: tail.next_cursor },
        controller.signal,
      );
      // A page that resolved after the list changed belongs to the old one.
      if (controller.signal.aborted || generation.current !== mine) return;
      if (!next) return;
      setMore((value) => ({
        generation: mine,
        pages: (value.generation === mine ? value.pages : []).concat(next),
      }));
    } catch {
      if (!controller.signal.aborted && generation.current === mine) setPagesError(true);
    } finally {
      // Only this generation's own request may clear its loading state.
      if (generation.current === mine) setLoadingMore({ generation: mine, on: false });
    }
  }, [jobId, pages, current, query, loading]);

  const allPages = current?.view ? [current.view, ...pages] : pages;
  const rows = allPages.flatMap((page) => page.files);
  const lastPage = allPages[allPages.length - 1];
  return {
    view: current?.view ?? null,
    rows,
    error: current?.error ?? false,
    loading: current === null,
    pagesError,
    retry,
    hasMore: Boolean(lastPage?.next_cursor),
    loadMore,
    loadingMore: loading,
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
  // without polling the conversation. Hidden pages are skipped, and one read
  // is queued for when the page becomes visible again so the time spent hidden
  // is not lost. The timer is cleaned up when the turn ends.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    let pending = false;
    const bump = () => {
      if (document.visibilityState === 'visible') {
        pending = false;
        setTick((value) => value + 1);
      } else {
        pending = true;
      }
    };
    const timer = window.setInterval(bump, ACTIVE_REFRESH_MS);
    const onVisible = () => {
      if (document.visibilityState === 'visible' && pending) {
        pending = false;
        setTick((value) => value + 1);
      }
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
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

  // The list is one bounded panel: the header and controls stay put and only
  // the rows scroll, so a long conversation never grows the page without end.
  return (
    <section className="mt-6" aria-labelledby="file-activity-title">
      <div className="overflow-hidden rounded-xl border border-border bg-surface-raised">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 px-3 pt-2.5">
          <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 gap-y-1">
            <h3 id="file-activity-title" className="text-sm font-semibold text-foreground">
              {t('files.title')}
            </h3>
            {view ? <Headline view={view} /> : null}
          </div>
          <div
            role="group"
            aria-label={t('files.filter')}
            className="flex flex-wrap items-center gap-1"
          >
            {FILE_ACTIVITY_FILTERS.map((filter) => (
              <Chip
                key={filter}
                active={query.filter === filter}
                onClick={() => setQuery((q) => ({ ...q, filter }))}
                label={t(FILTER_LABEL[filter])}
                count={filterCount(view?.filter_counts ?? {}, filter)}
                testId={`file-filter-${filter}`}
              />
            ))}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 px-3 pt-2 pb-2">
          <input
            type="search"
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder={t('files.searchPlaceholder')}
            aria-label={t('files.searchPlaceholder')}
            className="ui-focus-ring h-7 min-w-0 flex-1 basis-[calc(100%-0.5rem)] rounded-md border border-border bg-surface px-2 text-[11px] text-foreground placeholder:text-muted sm:basis-0"
          />
          <div
            role="group"
            aria-label={t('files.sort')}
            className="flex max-w-full flex-wrap items-center rounded-md border border-border p-0.5"
          >
            {FILE_ACTIVITY_SORTS.map((sort) => (
              <button
                key={sort}
                type="button"
                aria-pressed={query.sort === sort}
                onClick={() => setQuery((q) => ({ ...q, sort }))}
                data-testid={`file-sort-${sort}`}
                className={`ui-focus-ring whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] ${
                  query.sort === sort
                    ? 'bg-surface font-medium text-foreground shadow-subtle'
                    : 'text-muted hover:bg-surface-hover'
                }`}
              >
                {t(SORT_LABEL[sort])}
              </button>
            ))}
          </div>
          <span className="sr-only" aria-live="polite">
            {t('files.filtered', {
              count: filterCount(view?.filter_counts ?? {}, query.filter),
              total: filterCount(view?.filter_counts ?? {}, 'all'),
            })}
          </span>
        </div>
        <FileList
          rows={rows}
          query={query}
          error={error}
          loading={loading}
          pagesError={pagesError}
          hasMore={hasMore}
          loadingMore={loadingMore}
          retry={retry}
          loadMore={loadMore}
          expanded={expanded}
          onToggle={toggle}
          jobId={jobId}
          conversationKey={conversationKey}
          nowMs={nowMs}
          revision={refreshTick + tick}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
        />
        {view ? <Coverage view={view} /> : null}
      </div>
    </section>
  );
}

/** The bounded, internally scrolling list, with its empty, error and more states. */
function FileList({
  rows,
  query,
  error,
  loading,
  pagesError,
  hasMore,
  loadingMore,
  retry,
  loadMore,
  expanded,
  onToggle,
  jobId,
  conversationKey,
  nowMs,
  revision,
  onOpenPoint,
  onOpenTurn,
}: {
  rows: FileActivityRowApi[];
  query: Query;
  error: boolean;
  loading: boolean;
  pagesError: boolean;
  hasMore: boolean;
  loadingMore: boolean;
  retry: () => void;
  loadMore: () => Promise<void>;
  expanded: Set<string>;
  onToggle: (path: string) => void;
  jobId: string;
  conversationKey: string;
  nowMs: number;
  revision: number;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  if (error) {
    return (
      <p className="flex flex-wrap items-center gap-2 border-t border-border px-3 py-2 text-xs text-muted">
        {t('files.loadError')}
        <button
          type="button"
          onClick={retry}
          className="ui-button ui-button-ghost min-h-7 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
        >
          {t('common.retry')}
        </button>
      </p>
    );
  }
  if (loading) {
    return (
      <p className="border-t border-border px-3 py-2 text-xs text-muted">{t('common.loading')}</p>
    );
  }
  if (rows.length === 0) {
    return (
      <p className="border-t border-border px-3 py-2 text-xs text-muted">
        {query.q || query.filter !== 'all' ? t('files.noneMatching') : t('files.none')}
      </p>
    );
  }
  return (
    <ul
      aria-label={t('files.title')}
      data-testid="file-list"
      className="flex max-h-80 flex-col gap-0.5 overflow-y-auto border-t border-border p-1 text-xs"
    >
      {rows.map((row) => (
        <FileRow
          key={row.path}
          row={row}
          jobId={jobId}
          conversationKey={conversationKey}
          open={expanded.has(row.path)}
          onToggle={() => onToggle(row.path)}
          nowMs={nowMs}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
          revision={revision}
        />
      ))}
      {hasMore || pagesError ? (
        <li className="sticky bottom-0 flex shrink-0 items-center justify-center gap-2 bg-surface-raised/95 px-2 py-1 backdrop-blur">
          {pagesError ? (
            <>
              <span className="text-[11px] text-muted">{t('files.moreError')}</span>
              {/* A failed page must stay retryable: only the in-flight read
                  disables the control, never the failure it reported. */}
              <button
                type="button"
                onClick={() => void loadMore()}
                disabled={loadingMore}
                data-testid="file-show-more-retry"
                className="ui-button ui-button-ghost min-h-7 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
              >
                {loadingMore ? t('files.loadingMore') : t('common.retry')}
              </button>
            </>
          ) : (
            <button
              type="button"
              onClick={() => void loadMore()}
              disabled={loadingMore}
              data-testid="file-show-more"
              className="ui-button ui-button-ghost min-h-7 px-2 text-[11px] [@media(pointer:coarse)]:min-h-11"
            >
              {loadingMore ? t('files.loadingMore') : t('files.loadMore')}
            </button>
          )}
        </li>
      ) : null}
    </ul>
  );
}

/** The compact summary: files, recorded churn, and anything unattributed. */
function Headline({ view }: { view: FileActivityApi }) {
  const { t } = useLocale();
  const totals = view.totals;
  const partial = totals.added_unknown_files > 0 || totals.removed_unknown_files > 0;
  const unattributed = totals.unknown_path_operations + totals.search_unknown_scope;
  return (
    <p className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-[11px] text-muted">
      <span className="tabular-nums">{t('files.filesCount', { count: totals.files })}</span>
      <span
        className="tabular-nums"
        title={partial ? t('files.churnUnknownTip') : t('files.churnTip')}
      >
        <span className="text-emerald-600 dark:text-emerald-400">
          +{compactCount(totals.added)}
        </span>{' '}
        <span className="text-rose-600 dark:text-rose-400">−{compactCount(totals.removed)}</span>
        {partial ? <span> ~</span> : null}
      </span>
      {unattributed > 0 ? (
        <span title={t('files.unattributedTip')}>
          {t('files.unattributed', { count: compactCount(unattributed) })}
        </span>
      ) : null}
    </p>
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

/** What the totals describe: one short line, the detail in its tooltip. */
function Coverage({ view }: { view: FileActivityApi }) {
  const { t } = useLocale();
  const coverage = view.coverage;
  const detail = t('files.coverage.detail', {
    events: compactCount(coverage.events_scanned),
    turns: compactCount(coverage.turns),
    attempts: compactCount(coverage.attempts),
  });
  return (
    <p className="flex flex-wrap items-center gap-x-2 border-t border-border px-3 py-1 text-[10px] text-muted">
      {coverage.events_capped ? (
        <span className="text-amber-600 dark:text-amber-400">
          {t('files.coverage.cappedShort')}
        </span>
      ) : null}
      <span title={`${detail} ${t('files.coverage.notGit')}`}>
        {t('files.coverage.short', { events: compactCount(coverage.events_scanned) })}
      </span>
    </p>
  );
}

export type { TranslationKey };
