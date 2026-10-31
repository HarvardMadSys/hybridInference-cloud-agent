'use client';

// Every conversation with turns in the selected range, compared by its context
// profile, each one opening its own Context view. A search, a project (or the
// conversations without one) and a day from the heatmap narrow the whole
// range on the server; the heatmap, the project groups and the composition
// are the first page's, and cover every matching conversation.
//
// The numbers are the server's. Sizes are single requests' input tokens as the
// agent CLIs reported them; cost and cache hit are the Gateway's records summed
// on the server. Nothing here adds amounts together, and a measure that was not
// recorded is a dash. While the server is still reading runs it has not read
// before, the page says so and asks again until the ranking is complete.

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import { Select } from '@/components/ui/Select';
import {
  CONTEXT_SORTS,
  getContextInsights,
  type ContextInsightsFilters,
  type ContextInsightsPage,
  type ContextSort,
  type ConversationContext,
  type UsageRangePreset,
} from '@/lib/api/ownerUsage';
import type { TranslationKey } from '@/lib/i18n/translate';
import { contextHref, contextBar } from '../agents/context/contextPresent';
import { ContextActivityHeatmap } from './ContextActivityHeatmap';
import { Bar, ContextCompositionChart } from './ContextCompositionChart';
import { ContextInsightsFilterBar, ContextProjectGroups } from './ContextInsightsFilters';
import { NO_FILTERS, hasFilters, recordedSlices } from './contextInsightsPresent';
import { Card, LoadError, RangeNote, Tile, errorText } from './UsageParts';
import { cachedShare, compactCount, formatCost, formatShare, relativeTime } from './usagePresent';

/** How soon to ask again while runs are still being read. */
export const PROFILING_RETRY_MS = 2_500;
const PAGE = 25;

const SORT_LABEL: Record<ContextSort, TranslationKey> = {
  recent: 'contextInsights.sort.recent',
  peak: 'contextInsights.sort.peak',
  compactions: 'contextInsights.sort.compactions',
  requests: 'contextInsights.sort.requests',
};

/** The first page for a range, sort and filters, later pages on request, and a retry. */
function useInsights(range: UsageRangePreset, sort: ContextSort, filters: ContextInsightsFilters) {
  const { t } = useLocale();
  const [page, setPage] = useState<ContextInsightsPage | null>(null);
  const [rows, setRows] = useState<ConversationContext[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const latest = useRef(0);
  const fail = useCallback((cause: unknown) => setError(errorText(cause, t)), [t]);

  useEffect(() => {
    const controller = new AbortController();
    const request = ++latest.current;
    setLoading(true);
    getContextInsights({ range, sort, filters, limit: PAGE, signal: controller.signal })
      .then(
        (first) => {
          if (request !== latest.current) return;
          setPage(first);
          setRows(first.conversations);
          setCursor(first.next_cursor);
          setError(null);
        },
        (cause: unknown) => {
          if (!controller.signal.aborted && request === latest.current) fail(cause);
        },
      )
      .finally(() => {
        if (request === latest.current) setLoading(false);
      });
    return () => controller.abort();
  }, [range, sort, filters, attempt, fail]);

  // Ask again while the ranking is incomplete; a later read profiles more runs.
  const incomplete = page !== null && !page.profiling.complete && !error;
  useEffect(() => {
    if (!incomplete) return undefined;
    const timer = window.setTimeout(() => {
      if (document.visibilityState === 'visible') setAttempt((value) => value + 1);
    }, PROFILING_RETRY_MS);
    return () => window.clearTimeout(timer);
  }, [incomplete, page]);

  const showMore = async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    const request = latest.current;
    try {
      const next = await getContextInsights({ range, sort, filters, cursor, limit: PAGE });
      if (request !== latest.current) return;
      setRows((current) => [...current, ...next.conversations]);
      setCursor(next.next_cursor);
    } catch (cause) {
      if (request === latest.current) fail(cause);
    } finally {
      setLoadingMore(false);
    }
  };
  const retry = () => setAttempt((value) => value + 1);
  return { page, rows, cursor, error, loading, loadingMore, showMore, retry };
}

/**
 * The search, project and day filters. A day belongs to the range it was
 * picked from; it is dropped while rendering a new range, so no read pairs
 * the new range with the old day.
 */
function useFilters(range: UsageRangePreset) {
  const [filters, setFilters] = useState<ContextInsightsFilters>(NO_FILTERS);
  const [dayRange, setDayRange] = useState(range);
  if (dayRange !== range) {
    setDayRange(range);
    if (filters.day) setFilters({ ...filters, day: null });
  }
  return [filters, setFilters] as const;
}

/** Reads the Usage page's one range; search, project, grouping and sort are its own. */
export function ContextInsights({ range }: { range: UsageRangePreset }) {
  const { t } = useLocale();
  const [sort, setSort] = useState<ContextSort>('recent');
  const [filters, setFilters] = useFilters(range);
  const [grouped, setGrouped] = useState(false);
  const { page, rows, cursor, error, loading, loadingMore, showMore, retry } = useInsights(
    range,
    sort,
    filters,
  );
  // Only the first page is kept as `page`; later pages add rows, not charts.
  const projects = page?.projects ?? [];
  const nowMs = page ? Date.parse(page.generated_at) : 0;
  const widest = Math.max(0, ...rows.map((row) => row.peak_prompt_tokens ?? 0));
  const controls = (
    <Select
      id="settings-context-sort"
      value={sort}
      onValueChange={setSort}
      aria-label={t('contextInsights.sort')}
      className="ui-input min-h-9 w-44 text-sm [@media(pointer:coarse)]:min-h-11"
      options={CONTEXT_SORTS.map((value) => ({ value, label: t(SORT_LABEL[value]) }))}
    />
  );
  return (
    <Card id="settings-context-insights" title={t('contextInsights.title')} action={controls}>
      <p className="mt-1 text-sm text-muted">{t('contextInsights.description')}</p>
      <RangeNote period={page?.period} bars={false} />
      {error ? <LoadError message={error} onRetry={retry} /> : null}
      <ContextInsightsFilterBar
        filters={filters}
        projects={projects}
        grouped={grouped}
        matching={page && hasFilters(filters) ? page.summary.conversations : null}
        onChange={setFilters}
        onGroupedChange={setGrouped}
      />
      <InsightsTiles page={page} loading={loading} />
      {page?.activity ? (
        <ContextActivityHeatmap
          key={`${page.period.since}:${page.period.until}`}
          since={page.period.since}
          until={page.period.until}
          activity={page.activity}
          selected={filters.day}
          onSelect={(day) => setFilters((current) => ({ ...current, day }))}
        />
      ) : null}
      {page?.composition ? <ContextCompositionChart composition={page.composition} /> : null}
      <InsightsNotes page={page} empty={rows.length === 0 && !loading} />
      {grouped ? (
        <ContextProjectGroups
          projects={projects}
          onOpen={(project) => {
            setGrouped(false);
            setFilters((current) => ({
              ...current,
              repo: project.repo,
              temporary: project.repo === null,
            }));
          }}
        />
      ) : rows.length > 0 ? (
        <ul className="mt-3 divide-y divide-border rounded-lg border border-border">
          <ColumnHeads />
          {rows.map((row) => (
            <ConversationRow key={row.thread_id} row={row} widest={widest} nowMs={nowMs} />
          ))}
        </ul>
      ) : null}
      {cursor && !grouped ? (
        <button
          type="button"
          onClick={() => void showMore()}
          disabled={loadingMore}
          className="ui-button ui-button-secondary mt-3 min-h-11 disabled:opacity-60"
        >
          {t(loadingMore ? 'settings.usage.loadingMore' : 'contextInsights.showMore')}
        </button>
      ) : null}
      <p className="mt-4 text-xs text-muted">{t('contextInsights.semantics')}</p>
    </Card>
  );
}

/** Totals over every conversation in the range, not only the rows shown. */
function InsightsTiles({ page, loading }: { page: ContextInsightsPage | null; loading: boolean }) {
  const { t, locale } = useLocale();
  const summary = page?.summary;
  const count = (value: number | undefined) =>
    value === undefined ? '—' : value.toLocaleString(locale);
  let observed: string | undefined;
  let subagents: string | undefined;
  if (summary) {
    observed = t('contextInsights.conversationsObserved', {
      count: count(summary.conversations_observed),
    });
  }
  if (summary && summary.subagent_requests > 0) {
    subagents = t('contextInsights.requestsSubagent', { count: count(summary.subagent_requests) });
  }
  return (
    <dl
      aria-busy={loading}
      className={`mt-3 grid grid-cols-2 gap-2 lg:grid-cols-4 ${loading ? 'opacity-60' : ''}`}
    >
      <Tile
        label={t('contextInsights.conversations')}
        value={count(summary?.conversations)}
        detail={observed}
      />
      <Tile
        label={t('contextInsights.requests')}
        value={count(summary?.main_requests)}
        detail={subagents}
      />
      <Tile
        label={t('contextInsights.compactions')}
        value={count(summary?.compactions)}
        detail={t('contextInsights.compactionsDetail')}
      />
      <Tile
        label={t('contextInsights.peak')}
        value={compactCount(summary?.peak_prompt_tokens)}
        detail={t('contextInsights.peakDetail')}
      />
    </dl>
  );
}

/** Whether the ranking is complete, what is left out, and an empty range in words. */
function InsightsNotes({ page, empty }: { page: ContextInsightsPage | null; empty: boolean }) {
  const { t, locale } = useLocale();
  if (!page) return null;
  const { profiling, summary } = page;
  const pendingKey = profiling.pending_capped
    ? 'contextInsights.pendingCapped'
    : 'contextInsights.pending';
  return (
    <>
      {profiling.complete ? null : (
        <p role="status" className="mt-2 text-xs text-muted">
          {t(pendingKey, { count: profiling.pending_attempts.toLocaleString(locale) })}
        </p>
      )}
      {summary.deleted_conversation_runs > 0 ? (
        <p className="mt-2 text-xs text-muted">
          {t('contextInsights.deleted', {
            count: summary.deleted_conversation_runs.toLocaleString(locale),
          })}
        </p>
      ) : null}
      {empty ? <p className="mt-3 text-sm text-muted">{t('contextInsights.empty')}</p> : null}
    </>
  );
}

function ConversationRow({
  row,
  widest,
  nowMs,
}: {
  row: ConversationContext;
  widest: number;
  nowMs: number;
}) {
  const { t, locale } = useLocale();
  const bar = contextBar(
    row.peak_prompt_tokens,
    row.latest_prompt_tokens,
    row.context_window,
    widest,
  );
  const usage = row.usage;
  const title = row.title || t('settings.usage.untitled');
  // Nothing recorded is not zero requests: without an observation, both counts are unknown.
  const observed = row.peak_prompt_tokens !== null || row.compactions > 0 || row.main_requests > 0;
  return (
    <li className={`grid gap-2 px-3 py-2.5 text-sm ${ROW_GRID}`}>
      <div className="min-w-0">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <Link
            href={contextHref(row.latest_job_id)}
            title={title}
            className="ui-focus-ring min-w-0 truncate rounded font-medium text-foreground hover:underline"
          >
            {title}
          </Link>
          {row.archived ? (
            <span className="rounded border border-border px-1.5 text-[11px] text-muted">
              {t('settings.usage.archived')}
            </span>
          ) : null}
        </div>
        <p className="mt-0.5 truncate text-xs text-muted">
          {[row.repo, row.runtimes.join(', '), row.models.join(', ')].filter(Boolean).join(' · ')}
        </p>
        <p className="text-xs text-muted">
          {t('contextInsights.turns', { count: row.turns.toLocaleString(locale) })} ·{' '}
          {t('contextInsights.lastTurn', { when: relativeTime(row.last_turn_at, nowMs, locale) })}
        </p>
        <RowComposition row={row} />
      </div>
      <div className="min-w-0">
        {row.peak_prompt_tokens === null ? (
          <p className="text-xs text-muted">{t('contextInsights.notObserved')}</p>
        ) : (
          <>
            <p className="tabular-nums text-foreground">
              {compactCount(row.peak_prompt_tokens)} · {compactCount(row.latest_prompt_tokens)}
            </p>
            <div
              aria-hidden="true"
              className="relative mt-1 h-1.5 overflow-hidden rounded-full bg-surface-muted"
            >
              <span
                className="absolute inset-y-0 left-0 rounded-full bg-accent/40"
                style={{ width: `${bar.peak * 100}%` }}
              />
              {bar.latest !== null ? (
                <span
                  className="absolute inset-y-0 w-0.5 bg-accent"
                  style={{ left: `calc(${bar.latest * 100}% - 1px)` }}
                />
              ) : null}
            </div>
            <p className="mt-0.5 text-xs text-muted">
              {row.context_window
                ? t('contextInsights.ofWindow', { window: compactCount(row.context_window) })
                : t('contextInsights.windowNotReported')}
            </p>
          </>
        )}
        {row.attempts_profiled < row.attempts ? (
          <p className="text-xs text-muted">
            {t('contextInsights.partlyProfiled', {
              profiled: row.attempts_profiled.toLocaleString(locale),
              attempts: row.attempts.toLocaleString(locale),
            })}
          </p>
        ) : null}
      </div>
      <Stat
        label={t('contextInsights.col.compactions')}
        value={observed ? row.compactions.toLocaleString(locale) : '—'}
      />
      <Stat
        label={t('contextInsights.col.requests')}
        value={observed ? row.main_requests.toLocaleString(locale) : '—'}
      />
      <Stat label={t('contextInsights.col.cost')} value={formatCost(usage?.totals?.spent_usd)} />
      <Stat
        label={t('contextInsights.col.cache')}
        value={formatShare(usage ? cachedShare(usage) : null)}
      />
    </li>
  );
}

/** The conversation's recorded content by category, as the chart above draws it. */
function RowComposition({ row }: { row: ConversationContext }) {
  const { t } = useLocale();
  const composition = row.composition;
  if (!composition || composition.recorded.total_tokens === 0) return null;
  const label = t('contextInsights.row.recorded', {
    tokens: compactCount(composition.recorded.total_tokens),
  });
  return (
    <div className="mt-1 flex items-center gap-2" title={label}>
      <div className="w-24 shrink-0">
        <Bar parts={recordedSlices(composition)} label={label} />
      </div>
      <span className="mt-2 text-[11px] tabular-nums text-muted">{label}</span>
    </div>
  );
}

const ROW_GRID =
  'sm:grid-cols-[minmax(0,2fr)_minmax(0,2fr)_repeat(4,minmax(0,1fr))] sm:items-center sm:gap-3';

/** The list's column names, on screens wide enough to lay it out as columns. */
function ColumnHeads() {
  const { t } = useLocale();
  const heads: TranslationKey[] = [
    'contextInsights.col.conversation',
    'contextInsights.col.context',
    'contextInsights.col.compactions',
    'contextInsights.col.requests',
    'contextInsights.col.cost',
    'contextInsights.col.cache',
  ];
  return (
    <li aria-hidden="true" className={`hidden px-3 py-1.5 text-xs text-muted sm:grid ${ROW_GRID}`}>
      {heads.map((key, index) => (
        <span key={key} className={index > 1 ? 'text-right' : ''}>
          {t(key)}
        </span>
      ))}
    </li>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <p className="flex items-baseline justify-between gap-2 text-xs sm:block sm:text-right">
      <span className="text-muted sm:hidden">{label}</span>
      <span className="tabular-nums text-sm text-foreground">{value}</span>
    </p>
  );
}
