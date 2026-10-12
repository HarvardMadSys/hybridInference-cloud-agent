'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getAdminUsage,
  type AdminUsage,
  type AdminUsageAggregate,
  type AdminUsageBreakdown,
  type AdminUsageDay,
  type AdminUsageFilters,
} from '@/lib/api/adminUsage';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import {
  accessDenied,
  AdminRequired,
  dateTime,
  ErrorNotice,
  ICON_BUTTON,
  number,
  TOOLBAR_SELECT,
} from '../runs/shared';
import {
  cachedShare,
  change,
  costDisplay,
  formatMetric,
  IDENTITY_LABELS,
  isPartialDay,
  metricDisplay,
  metricValue,
  notFinal,
  previousLabel,
  previousWindow,
  share,
  shortDay,
  tickIndexes,
  type UsageMetric,
} from './dashboard';
import {
  activityHref,
  IDENTITY_KEYS,
  MAX_FILTER_LENGTH,
  PRESETS,
  presetKey,
  runsHref,
  selectionFromQuery,
  urlFor,
  windowFor,
} from './present';

//: What a Usage selection is, said once as the window control's tooltip.
const BASIS =
  'Turns created in this window, with all the usage they recorded. Days are UTC. Usage is read from a cache, so a running turn may not be fully counted yet.';

const HEADLINE: { metric: UsageMetric; label: string; hint: string }[] = [
  {
    metric: 'cost',
    label: 'Cost',
    hint: 'What the gateway recorded for the model calls of these turns.',
  },
  { metric: 'turns', label: 'Turns', hint: 'Turns created in this window, from every session.' },
  { metric: 'calls', label: 'Model calls', hint: 'Model requests these turns made.' },
  {
    metric: 'tokens_in',
    label: 'Input tokens',
    hint: 'Input tokens of those requests, cache reads included.',
  },
  { metric: 'tokens_out', label: 'Output tokens', hint: 'Output tokens of those requests.' },
];

const CHART_METRICS: { metric: UsageMetric; label: string }[] = [
  { metric: 'cost', label: 'Cost' },
  { metric: 'turns', label: 'Turns' },
  { metric: 'calls', label: 'Model calls' },
  { metric: 'tokens_in', label: 'Input tokens' },
];

type IdentityKey = (typeof IDENTITY_KEYS)[number];
type RankedDimension = 'user_id' | 'model';

export default function AdminUsagePage() {
  const { state } = useAuth();
  // Subscribed here, one level above the panel, so a URL change re-renders
  // this page rather than only the component that happens to read it.
  const query = useSearchParams().toString();
  // The panel writes its selection into the address bar, and the router hands
  // that write back here as a new query. It is the selection already on
  // screen, so only a query the panel did not write starts a new panel.
  const written = useRef<string | null>(null);
  const seen = useRef(query);
  const [panel, setPanel] = useState({ query, version: 0 });
  useEffect(() => {
    if (query === seen.current) return;
    seen.current = query;
    if (query !== written.current) {
      setPanel((current) => ({ query, version: current.version + 1 }));
    }
  }, [query]);
  const onWrite = useCallback((next: string) => {
    written.current = next;
  }, []);
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<UsageSkeleton />}>
      <UsageView
        key={`${state.user?.id ?? 'admin'}:${panel.version}`}
        initialQuery={panel.query}
        onWrite={onWrite}
      />
    </Suspense>
  );
}

function UsageView({
  initialQuery,
  onWrite,
}: {
  initialQuery: string;
  onWrite: (query: string) => void;
}) {
  const linked = selectionFromQuery(initialQuery);
  // The absolute window this page reads. A link supplies it; otherwise the
  // default is resolved once, so a reload of the same URL selects the same turns.
  const [filters, setFilters] = useState<AdminUsageFilters>(() => linked.filters ?? windowFor(7));
  // The preset the window is. A link's window is one only while it still ends
  // now; a preset chosen here stays chosen as the clock moves on.
  const [windowKey, setWindowKey] = useState(() =>
    linked.filters ? presetKey(linked.filters) : '7',
  );
  // A linked window stays exactly as linked. One chosen here re-anchors at now
  // on Refresh, which is what refreshing "the last 7 days" means.
  const [linkedWindow, setLinkedWindow] = useState(linked.linked);
  // A link this page cannot serve. Choosing a window replaces it.
  const [linkError, setLinkError] = useState<string | null>(linked.error);
  // Each result carries the selection that produced it. A result from another
  // selection is never shown beside links built from the current one.
  const [result, setResult] = useState<{ selection: string; usage: AdminUsage } | null>(null);
  const [previous, setPrevious] = useState<{ selection: string; usage: AdminUsage } | null>(null);
  const [loading, setLoading] = useState(!linked.error);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);

  const selectionKey = JSON.stringify(filters);
  const current = result?.selection === selectionKey ? result.usage : null;
  const before = previous?.selection === selectionKey ? previous.usage : null;

  useEffect(() => {
    // A link this page cannot serve stays in the address bar as it is:
    // replacing it would hide the link being reported.
    if (linkError) return;
    const next = urlFor(window.location.pathname, filters);
    onWrite(next.slice(next.indexOf('?') + 1));
    window.history.replaceState(null, '', next);
  }, [filters, linkError, onWrite]);

  useEffect(() => {
    if (linkError) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getAdminUsage(filters, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setResult({ selection: selectionKey, usage: next });
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(e)) {
          // A revoked session must not keep showing the numbers it already has.
          setDenied(true);
          setResult(null);
        } else {
          setError(getErrorMessage(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    // The same selection one window earlier, for the change beside each
    // number. It is only a comparison: when it cannot be read, the numbers
    // are shown without one.
    const earlier = previousWindow(filters);
    if (earlier) {
      getAdminUsage(earlier, controller.signal)
        .then((next) => {
          if (!controller.signal.aborted) setPrevious({ selection: selectionKey, usage: next });
        })
        .catch(() => {});
    }
    return () => controller.abort();
  }, [filters, selectionKey, linkError]);

  function chooseWindow(key: string) {
    const days = PRESETS.find((preset) => preset.key === key)?.days;
    if (!days) return;
    setWindowKey(key);
    setLinkedWindow(false);
    // Choosing a window is the selection a broken link could not provide.
    setLinkError(null);
    setFilters((selected) => ({ ...identities(selected), ...windowFor(days) }));
  }

  function refresh() {
    const days = PRESETS.find((preset) => preset.key === windowKey)?.days;
    setFilters((selected) =>
      linkedWindow || !days ? { ...selected } : { ...selected, ...windowFor(days) },
    );
  }

  // Narrowing keeps the window on screen: only choosing a window moves it.
  const narrow = useCallback((dimension: RankedDimension, key: string) => {
    setFilters((selected) => ({ ...selected, [dimension]: key }));
  }, []);

  function widen(key: IdentityKey) {
    setFilters((selected) => {
      const next = { ...selected };
      delete next[key];
      return next;
    });
  }

  if (denied) return <AdminRequired />;

  const narrowedBy = IDENTITY_KEYS.filter((key) => filters[key]);
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Usage"
          description="Review token usage and costs across users, sessions, and agent turns."
        />
        <AdminNavigation current="usage" />

        <h2 className="sr-only">Usage</h2>
        <div className="ui-card mt-6 flex flex-wrap items-center gap-3 p-3">
          <label className="shrink-0" title={BASIS}>
            <span className="sr-only">Turn created</span>
            <select
              className={TOOLBAR_SELECT}
              value={windowKey}
              onChange={(e) => chooseWindow(e.target.value)}
            >
              {PRESETS.map((preset) => (
                <option key={preset.key} value={preset.key}>
                  {preset.label}
                </option>
              ))}
              {windowKey === 'custom' && <option value="custom">Custom</option>}
            </select>
          </label>
          {windowKey === 'custom' && !linkError && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {shortDay(filters.since)} – {shortDay(filters.until)}
            </span>
          )}
          {!linkError &&
            narrowedBy.map((key) => (
              <FilterChip
                key={key}
                label={IDENTITY_LABELS[key]}
                value={filters[key] ?? ''}
                onRemove={() => widen(key)}
              />
            ))}
          <div className="ml-auto flex flex-wrap items-center gap-1">
            {!linkError && (
              <Link
                href={activityHref(filters)}
                prefetch={false}
                className="ui-button ui-button-ghost min-h-11"
              >
                Open in Activity
                <ArrowIcon />
              </Link>
            )}
            <button
              className={ICON_BUTTON}
              type="button"
              aria-label="Refresh"
              title="Refresh"
              disabled={loading || Boolean(linkError)}
              onClick={refresh}
            >
              <svg
                aria-hidden
                viewBox="0 0 20 20"
                fill="none"
                stroke="currentColor"
                strokeWidth={1.6}
                className="h-4 w-4"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M16.3 10a6.3 6.3 0 1 1-1.85-4.45M16.3 3.8v3.4h-3.4"
                />
              </svg>
            </button>
          </div>
        </div>

        {linkError ? (
          <ErrorNotice>{linkError} Choose a window to read a selection instead.</ErrorNotice>
        ) : (
          error && (
            <ErrorNotice>
              {error}
              {current ? (
                ' The numbers below are from the previous read.'
              ) : (
                <button type="button" className="ml-3 underline" onClick={refresh}>
                  Retry
                </button>
              )}
            </ErrorNotice>
          )
        )}

        {loading && !current ? (
          <UsageSkeleton />
        ) : (
          current && (
            <Dashboard usage={current} previous={before} filters={filters} onNarrow={narrow} />
          )
        )}
      </section>
    </div>
  );
}

function Dashboard({
  usage,
  previous,
  filters,
  onNarrow,
}: {
  usage: AdminUsage;
  previous: AdminUsage | null;
  filters: AdminUsageFilters;
  onNarrow: (dimension: RankedDimension, key: string) => void;
}) {
  const summary = usage.summary;
  if (summary.run_count === 0) {
    return (
      <p className="mt-10 text-sm text-gray-500 dark:text-gray-400">
        No turns were created in this window.
      </p>
    );
  }
  const unfinished = notFinal(summary);
  return (
    <>
      <Headline summary={summary} previous={previous?.summary ?? null} filters={filters} />
      {unfinished.clauses.length > 0 && (
        <p
          className="mt-5 text-xs text-gray-500 dark:text-gray-400"
          title={[
            ...unfinished.detail,
            `Usage is read from a cache; the oldest read is from ${dateTime(summary.oldest_queried_at)}.`,
          ].join('\n')}
        >
          Not final: {unfinished.clauses.join(' · ')}.
        </p>
      )}
      <DailyChart days={usage.days} filters={filters} />
      <div className="mt-12 grid grid-cols-1 gap-x-12 gap-y-10 lg:grid-cols-2">
        <Ranking
          title="Users"
          noun="user"
          dimension="user_id"
          breakdown={usage.by_user}
          whole={summary}
          filters={filters}
          onNarrow={onNarrow}
        />
        <Ranking
          title="Models"
          noun="model"
          dimension="model"
          breakdown={usage.by_model}
          whole={summary}
          filters={filters}
          onNarrow={onNarrow}
        />
      </div>
    </>
  );
}

function Headline({
  summary,
  previous,
  filters,
}: {
  summary: AdminUsageAggregate;
  previous: AdminUsageAggregate | null;
  filters: AdminUsageFilters;
}) {
  const earlier = previousLabel(filters);
  return (
    <dl className="mt-8 grid grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-3 lg:grid-cols-5">
      {HEADLINE.map(({ metric, label, hint }) => {
        const shown = metricDisplay(summary, metric);
        const delta = previous
          ? change(metricValue(summary, metric), metricValue(previous, metric))
          : null;
        const cached = metric === 'tokens_in' ? cachedShare(summary) : null;
        return (
          <div key={metric} className="min-w-0">
            <dt className="text-xs text-gray-500 dark:text-gray-400" title={hint}>
              {label}
            </dt>
            <dd
              className="mt-1 truncate text-2xl font-semibold tabular-nums tracking-tight text-gray-900 dark:text-gray-50"
              title={shown.exact ?? undefined}
            >
              {shown.display}
            </dd>
            <dd className="mt-0.5 flex h-4 gap-2 whitespace-nowrap text-xs tabular-nums text-gray-500 dark:text-gray-400">
              {delta && previous && (
                <span title={`${capitalize(earlier)}: ${metricDisplay(previous, metric).display}`}>
                  {delta}
                  <span className="sr-only"> from the {earlier}</span>
                </span>
              )}
              {cached !== null && <span>{Math.round(cached * 100)}% cached</span>}
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function DailyChart({ days, filters }: { days: AdminUsageDay[]; filters: AdminUsageFilters }) {
  const [metric, setMetric] = useState<UsageMetric>('cost');
  const [pointed, setPointed] = useState<number | null>(null);
  if (days.length === 0) return null;
  const values = days.map((day) => metricValue(day.usage, metric));
  const peak = Math.max(0, ...values.map((value) => value ?? 0));
  const ticks = new Set(tickIndexes(days.length));
  // The day being read out: the one pointed at, otherwise the latest.
  const shown = pointed !== null && pointed < days.length ? pointed : days.length - 1;
  const label = CHART_METRICS.find((item) => item.metric === metric)?.label ?? '';
  return (
    <section aria-label={`${label} by day`} className="mt-10">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div
          role="group"
          aria-label="Chart"
          className="inline-flex rounded-lg bg-gray-100 p-0.5 dark:bg-white/5"
        >
          {CHART_METRICS.map((item) => (
            <button
              key={item.metric}
              type="button"
              aria-pressed={item.metric === metric}
              onClick={() => setMetric(item.metric)}
              className={`rounded-md px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson ${
                item.metric === metric
                  ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-gray-50'
                  : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              }`}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-xs tabular-nums text-gray-500 dark:text-gray-400">
          {dayReading(days[shown], metric)}
        </p>
      </div>
      <div className="relative mt-4 h-44 border-b border-gray-200 dark:border-white/10">
        <div className="absolute inset-x-0 top-0 border-t border-dashed border-gray-200 dark:border-white/10" />
        <span className="absolute left-0 top-1 text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
          {formatMetric(metric, peak)}
        </span>
        <div className="flex h-full items-end gap-[3px] pt-6">
          {days.map((day, index) => {
            const value = values[index];
            const height = peak > 0 && value ? Math.max(2, (value / peak) * 100) : 0;
            // A day with turns but no recorded value is unknown, not zero, so
            // it is marked instead of drawn as an empty bar.
            const unknown = value === null && day.usage.run_count > 0;
            const href = runsHref(filters, {
              dimension: 'day',
              since: day.since,
              until: day.until,
            });
            return (
              <Link
                key={day.date}
                href={href ?? activityHref(filters)}
                prefetch={false}
                aria-label={dayReading(day, metric)}
                onMouseEnter={() => setPointed(index)}
                onMouseLeave={() => setPointed(null)}
                onFocus={() => setPointed(index)}
                onBlur={() => setPointed(null)}
                className="group flex h-full min-w-0 flex-1 items-end justify-center focus-visible:outline-none"
              >
                {unknown ? (
                  <span className="block w-full max-w-10 border-t-2 border-dotted border-gray-400 dark:border-gray-500" />
                ) : (
                  <span
                    className={`block w-full max-w-10 rounded-t-sm transition-colors group-focus-visible:ring-2 group-focus-visible:ring-crimson ${
                      index === pointed
                        ? 'bg-gray-500 dark:bg-white/50'
                        : 'bg-gray-300 group-hover:bg-gray-500 dark:bg-white/20 dark:group-hover:bg-white/50'
                    } ${isPartialDay(day) ? 'opacity-60' : ''}`}
                    style={{ height: `${height}%` }}
                  />
                )}
              </Link>
            );
          })}
        </div>
      </div>
      <div className="relative mt-1.5 h-4 text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
        {days.map((day, index) =>
          ticks.has(index) ? (
            <span
              key={day.date}
              className="absolute top-0 whitespace-nowrap"
              style={tickPosition(index, days.length)}
            >
              {shortDay(day.date)}
            </span>
          ) : null,
        )}
      </div>
    </section>
  );
}

/** Where a day's label sits: under its bar, kept inside the chart at both ends. */
function tickPosition(index: number, total: number): React.CSSProperties {
  if (index === 0) return { left: 0 };
  if (index === total - 1) return { right: 0 };
  return { left: `${((index + 0.5) / total) * 100}%`, transform: 'translateX(-50%)' };
}

/** One day as the chart reads it out: `Sep 23 · $3.42 · 17 turns`. */
function dayReading(day: AdminUsageDay, metric: UsageMetric): string {
  const name = `${shortDay(day.date)}${isPartialDay(day) ? ' (part of the day)' : ''}`;
  const turns = day.usage.run_count;
  if (turns === 0) return `${name} · no turns`;
  const value = metricValue(day.usage, metric);
  const turnText = `${number(turns)} turn${turns === 1 ? '' : 's'}`;
  if (metric === 'turns') return `${name} · ${turnText}`;
  if (value === null) return `${name} · ${turnText}, no usage recorded yet`;
  return `${name} · ${formatMetric(metric, value)} · ${turnText}`;
}

function Ranking({
  title,
  noun,
  dimension,
  breakdown,
  whole,
  filters,
  onNarrow,
}: {
  title: string;
  noun: string;
  dimension: RankedDimension;
  breakdown: AdminUsageBreakdown;
  whole: AdminUsageAggregate;
  filters: AdminUsageFilters;
  onNarrow: (dimension: RankedDimension, key: string) => void;
}) {
  const others = breakdown.group_count - breakdown.groups.length;
  const headingId = `ranking-${dimension}`;
  return (
    <section aria-labelledby={headingId} className="min-w-0">
      <div className="flex items-center gap-2 px-2 pb-1 text-xs text-gray-500 dark:text-gray-400">
        <h3 id={headingId} className="min-w-0 flex-1 font-medium text-gray-900 dark:text-gray-100">
          {title}
        </h3>
        <span className="hidden w-20 shrink-0 sm:block" />
        <span className="w-12 shrink-0 text-right">Turns</span>
        <span className="w-20 shrink-0 text-right">Cost</span>
        <span className="w-5 shrink-0" />
      </div>
      <ul>
        {breakdown.groups.map((group) => {
          const identity =
            group.key !== null && group.key.length <= MAX_FILTER_LENGTH ? group.key : null;
          return (
            <RankRow
              key={group.key ?? 'unknown'}
              name={group.key ?? `Unknown ${noun}`}
              usage={group.usage}
              whole={whole}
              identity={identity}
              reason={identity === null ? unfilterable(group.key, noun) : null}
              narrowed={filters[dimension] === group.key}
              href={runsHref(filters, { dimension, value: group.key })}
              onNarrow={(key) => onNarrow(dimension, key)}
            />
          );
        })}
        {breakdown.other && (
          <RankRow
            name={`Other · ${number(others)} ${noun}${others === 1 ? '' : 's'}`}
            usage={breakdown.other.usage}
            whole={whole}
            identity={null}
            reason={`The ${noun}s after the first ${breakdown.groups.length}, added together; they are not one filter.`}
            narrowed={false}
            href={null}
            onNarrow={() => {}}
            muted
          />
        )}
      </ul>
      {breakdown.groups.length === 0 && (
        <p className="px-2 py-2 text-sm text-gray-500 dark:text-gray-400">No {noun}s.</p>
      )}
    </section>
  );
}

/** Why a group offers no filter, said in its tooltip rather than left unexplained. */
function unfilterable(key: string | null, noun: string): string {
  return key === null
    ? `An unknown ${noun} cannot be filtered by.`
    : 'Too long to filter by exactly. It is still counted.';
}

function RankRow({
  name,
  usage,
  whole,
  identity,
  reason,
  narrowed,
  href,
  onNarrow,
  muted = false,
}: {
  name: string;
  usage: AdminUsageAggregate;
  whole: AdminUsageAggregate;
  identity: string | null;
  reason: string | null;
  narrowed: boolean;
  href: string | null;
  onNarrow: (key: string) => void;
  muted?: boolean;
}) {
  const cost = costDisplay(usage.totals?.spent_usd);
  const part = share(usage, whole);
  const detail = [
    `${number(usage.run_count)} turn${usage.run_count === 1 ? '' : 's'}`,
    usage.totals ? `${number(usage.totals.calls)} model calls` : 'no usage recorded yet',
    `${Math.round(part * 100)}% of the selection`,
  ].join(' · ');
  const text = `min-w-0 flex-1 truncate text-left text-[13px] ${
    muted ? 'text-gray-500 dark:text-gray-400' : 'font-mono text-gray-900 dark:text-gray-100'
  }`;
  return (
    <li
      className="group relative flex h-9 items-center gap-2 rounded-md px-2 transition-colors hover:bg-gray-100 dark:hover:bg-gray-900"
      title={[name, detail, reason].filter(Boolean).join('\n')}
    >
      {identity !== null && !narrowed ? (
        <button
          type="button"
          onClick={() => onNarrow(identity)}
          className={`${text} after:absolute after:inset-0 after:rounded-md focus-visible:outline-none focus-visible:after:ring-2 focus-visible:after:ring-crimson`}
          aria-label={`Show only ${name}`}
        >
          {name}
        </button>
      ) : (
        <span className={text}>{name}</span>
      )}
      <span className="hidden h-1 w-20 shrink-0 overflow-hidden rounded-full bg-gray-200 sm:block dark:bg-white/10">
        <span
          className="block h-full rounded-full bg-gray-500 dark:bg-white/40"
          style={{ width: `${part * 100}%` }}
        />
      </span>
      <span className="w-12 shrink-0 text-right text-[13px] tabular-nums text-gray-500 dark:text-gray-400">
        {number(usage.run_count)}
      </span>
      <span
        className="w-20 shrink-0 text-right text-[13px] tabular-nums text-gray-900 dark:text-gray-100"
        title={cost.exact ?? undefined}
      >
        {cost.display}
      </span>
      <span className="flex w-5 shrink-0 justify-end">
        {href && !muted && (
          <Link
            href={href}
            prefetch={false}
            aria-label={`Open ${name} in Activity`}
            title="Open in Activity"
            className="relative z-10 rounded p-0.5 text-gray-400 opacity-0 dark:text-gray-500 transition-opacity hover:text-gray-900 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson group-hover:opacity-100 dark:hover:text-gray-100"
          >
            <ArrowIcon />
          </Link>
        )}
      </span>
    </li>
  );
}

function FilterChip({
  label,
  value,
  onRemove,
}: {
  label: string;
  value: string;
  onRemove: () => void;
}) {
  return (
    <span className="inline-flex min-h-9 min-w-0 max-w-xs items-center gap-1.5 rounded-lg bg-surface-muted pl-3 pr-1 text-xs text-foreground">
      <span className="text-gray-500 dark:text-gray-400">{label}</span>
      <span className="min-w-0 truncate font-mono" title={value}>
        {value}
      </span>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove the ${label.toLowerCase()} filter`}
        className="ui-icon-button ui-button-ghost h-8 w-8 shrink-0"
      >
        <svg aria-hidden viewBox="0 0 16 16" fill="none" stroke="currentColor" className="h-3 w-3">
          <path strokeWidth={1.6} strokeLinecap="round" d="M4.5 4.5l7 7m0-7l-7 7" />
        </svg>
      </button>
    </span>
  );
}

function ArrowIcon() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" fill="none" stroke="currentColor" className="h-3.5 w-3.5">
      <path
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 8h9.5M8.5 4l4 4-4 4"
      />
    </svg>
  );
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The identities of a selection, without its window. */
function identities(filters: AdminUsageFilters): AdminUsageFilters {
  const kept: AdminUsageFilters = {};
  for (const key of IDENTITY_KEYS) {
    if (filters[key]) kept[key] = filters[key];
  }
  return kept;
}

function UsageSkeleton() {
  return (
    <div role="status" className="mt-8">
      <span className="sr-only">Loading usage…</span>
      <div aria-hidden className="animate-pulse">
        <div className="grid grid-cols-2 gap-x-8 gap-y-6 sm:grid-cols-3 lg:grid-cols-5">
          {HEADLINE.map((item) => (
            <div key={item.metric} className="h-16 rounded-md bg-gray-100 dark:bg-white/5" />
          ))}
        </div>
        <div className="mt-10 h-56 rounded-md bg-gray-100 dark:bg-white/5" />
        <div className="mt-12 grid gap-x-12 gap-y-10 lg:grid-cols-2">
          <div className="h-48 rounded-md bg-gray-100 dark:bg-white/5" />
          <div className="h-48 rounded-md bg-gray-100 dark:bg-white/5" />
        </div>
      </div>
    </div>
  );
}
