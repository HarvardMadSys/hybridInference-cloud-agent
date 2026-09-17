'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useState, type FormEvent } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getAdminUsage,
  type AdminUsage,
  type AdminUsageAggregate,
  type AdminUsageFilters,
} from '@/lib/api/adminUsage';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  INPUT,
  money,
  number,
  utcDate,
} from '../runs/shared';
import {
  CACHE_NOTICE,
  COVERAGE_BASIS,
  PRESETS,
  WINDOW_BASIS,
  breakdownKey,
  coverageSummary,
  groupLabel,
  identityFromForm,
  selectionFromQuery,
  headline,
  presetKey,
  runsHref,
  type RunsTarget,
  statusLines,
  urlFor,
  windowFor,
} from './present';

const DIMENSIONS = [
  { key: 'user_id', label: 'Users', column: 'User' },
  { key: 'model', label: 'Configured models', column: 'Configured model' },
  { key: 'day', label: 'Created date', column: 'Created (UTC)' },
] as const;

type Dimension = (typeof DIMENSIONS)[number]['key'];

const SKELETON_CARDS = 4;

export default function AdminUsagePage() {
  const { state } = useAuth();
  // Subscribed here, one level above the panel, so a URL change re-renders
  // this page rather than only the component that happens to read it.
  const query = useSearchParams().toString();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<UsageSkeleton />}>
      <UsageView key={`${state.user?.id ?? 'admin'}:${query}`} initialQuery={query} />
    </Suspense>
  );
}

function UsageView({ initialQuery }: { initialQuery: string }) {
  const linked = selectionFromQuery(initialQuery);
  // The absolute window this page reads. A link supplies it; otherwise the
  // default is resolved once and then reused, so a reload of the same URL
  // selects the same runs.
  const [filters, setFilters] = useState<AdminUsageFilters>(() => linked.filters ?? windowFor(7));
  const [preset, setPreset] = useState(() => {
    const applied = linked.filters ?? windowFor(7);
    return presetKey(applied);
  });
  // Only an explicit preset choice re-anchors the window. Applying a changed
  // identity filter keeps the absolute window the page already shows.
  const [presetChosen, setPresetChosen] = useState(false);
  // Each result carries the selection that produced it. A result from a
  // different selection is never shown beside a link built from the current
  // one, which would point at runs the numbers do not describe.
  const [result, setResult] = useState<{
    selection: string;
    usage: AdminUsage;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  // A link this page cannot serve, as a state rather than a constant: an
  // explicit Apply is the admin saying which selection they want instead, and
  // that has to be able to clear it.
  const [linkError, setLinkError] = useState<string | null>(linked.error);
  const [error, setError] = useState<string | null>(linked.error);
  const [denied, setDenied] = useState(false);
  const [dimension, setDimension] = useState<Dimension>('user_id');
  const [showDetails, setShowDetails] = useState(false);

  const selectionKey = JSON.stringify(filters);
  const current = result?.selection === selectionKey ? result.usage : null;

  useEffect(() => {
    if (linkError) {
      // A link this page cannot serve is shown as it is. Writing a selection
      // back would replace the link being reported, and the remount that
      // follows would clear the error and query every owner. Only an explicit
      // Apply clears this, and it writes the selection it applies.
      return;
    }
    // Native history keeps the app router out of this: the panel already holds
    // the applied selection, so a reactive navigation would only re-render it.
    window.history.replaceState(null, '', urlFor(window.location.pathname, filters));
  }, [filters, linkError]);

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
    return () => controller.abort();
    // The key carries the selection: this panel is remounted rather than
    // re-fetched when the URL names a different one.
  }, [filters, selectionKey, linkError]);

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget.elements;
    const days = presetChosen && preset !== 'custom' ? Number(preset) : null;
    // The identity filters come only from the form: a field the admin cleared
    // must disappear from the request rather than keep its previous value.
    const next: AdminUsageFilters = {
      ...(days === null ? { since: filters.since, until: filters.until } : windowFor(days)),
      ...identityFromForm((key) => {
        const field = form.namedItem(key);
        return field instanceof HTMLInputElement ? field.value : '';
      }),
    };
    // Applying is the admin choosing a selection, which is exactly what an
    // unusable link could not provide. Clearing this first lets the write-back
    // and the request proceed with the selection just applied.
    setLinkError(null);
    setFilters(next);
    setPresetChosen(false);
  }

  function refresh() {
    // Re-reading the cache is not a new window: the applied one is unchanged.
    setFilters({ ...filters });
  }

  if (denied) return <AdminRequired />;

  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-10 lg:px-12 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="usage" />

        <div className="mt-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Usage</h2>
            <p className="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
              {WINDOW_BASIS}
            </p>
          </div>
          <button className={BUTTON} type="button" disabled={loading} onClick={refresh}>
            Refresh
          </button>
        </div>

        <form onSubmit={apply} aria-label="Usage filters" className={`${CARD} mt-5 p-5`}>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <label className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Created
              <select
                className={`${INPUT} mt-1.5`}
                name="preset"
                value={preset}
                onChange={(e) => {
                  setPreset(e.target.value);
                  setPresetChosen(true);
                }}
              >
                {PRESETS.map((option) => (
                  <option key={option.key} value={option.key}>
                    {option.label}
                  </option>
                ))}
                <option value="custom">Custom</option>
              </select>
            </label>
            {(
              [
                ['user_id', 'User ID'],
                ['model', 'Configured model'],
                ['runtime', 'Agent'],
                ['repo', 'Repository'],
              ] as const
            ).map(([key, label]) => (
              <label
                key={`${key}:${filters[key] ?? ''}`}
                className="text-xs font-medium text-gray-500 dark:text-gray-400"
              >
                {label}
                <input
                  className={`${INPUT} mt-1.5`}
                  name={key}
                  defaultValue={filters[key] ?? ''}
                  placeholder="Any"
                />
              </label>
            ))}
          </div>
          <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
            Applied window: {dateTime(filters.since)} to {dateTime(filters.until)}
          </p>
          <div className="mt-4 flex justify-end border-t border-gray-100 pt-4 dark:border-white/5">
            <button type="submit" className={BUTTON}>
              Apply filters
            </button>
          </div>
        </form>

        {error && (
          <ErrorNotice>
            {error}
            <ErrorRecovery linkError={Boolean(linkError)} retry={refresh} showRetry={!current} />
          </ErrorNotice>
        )}

        {loading && !current ? (
          <UsageSkeleton />
        ) : (
          current && (
            <>
              <Headline
                aggregate={current.summary}
                generatedAt={current.generated_at}
                windowSince={current.selection.since}
                windowUntil={current.selection.until}
                failed={Boolean(error)}
              />
              <Coverage
                aggregate={current.summary}
                open={showDetails}
                onToggle={() => setShowDetails((value) => !value)}
              />
              <Breakdown
                usage={current}
                dimension={dimension}
                onDimension={setDimension}
                filters={filters}
              />
            </>
          )
        )}
      </section>
    </div>
  );
}

function Headline({
  aggregate,
  generatedAt,
  windowSince,
  windowUntil,
  failed,
}: {
  aggregate: AdminUsageAggregate;
  generatedAt: string;
  windowSince: string;
  windowUntil: string;
  failed: boolean;
}) {
  return (
    <>
      <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {headline(aggregate).map((metric) => (
          <div key={metric.label} className={`${CARD} p-5`}>
            <p className="text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              {metric.label}
            </p>
            <p
              className="mt-2 text-2xl font-semibold tabular-nums text-gray-900 dark:text-gray-50"
              title={metric.exact ?? undefined}
            >
              {metric.value}
            </p>
            {metric.detail && (
              <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">{metric.detail}</p>
            )}
          </div>
        ))}
      </div>
      <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
        Page read at {dateTime(generatedAt)}. Selected window {dateTime(windowSince)} to{' '}
        {dateTime(windowUntil)}. {CACHE_NOTICE}
        {failed && ' The last refresh failed; these values are the previous successful read.'}
      </p>
    </>
  );
}

function Coverage({
  aggregate,
  open,
  onToggle,
}: {
  aggregate: AdminUsageAggregate;
  open: boolean;
  onToggle: () => void;
}) {
  const statuses = statusLines(aggregate);
  const unknown = Object.entries(aggregate.unknown_rows ?? {}).filter(([, count]) => count > 0);
  return (
    <section className={`${CARD} mt-5 p-5`} aria-labelledby="coverage-heading">
      <h3 id="coverage-heading" className="text-sm font-semibold text-gray-900 dark:text-gray-50">
        Usage coverage
      </h3>
      <p className="mt-2 text-sm text-gray-700 dark:text-gray-300">{coverageSummary(aggregate)}</p>
      <ul className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
        {aggregate.missing_attempts > 0 && (
          <li>{number(aggregate.missing_attempts)} executions without a readable snapshot</li>
        )}
        {statuses.map((line) => (
          <li key={line.label}>
            {number(line.count)} {line.label.toLowerCase()}
          </li>
        ))}
        {aggregate.active_runs > 0 && <li>{number(aggregate.active_runs)} still active</li>}
        {aggregate.refreshing_runs > 0 && (
          <li>{number(aggregate.refreshing_runs)} with an accounting update in progress</li>
        )}
      </ul>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Oldest known snapshot query: {dateTime(aggregate.oldest_queried_at)}. Not finalized.{' '}
        {COVERAGE_BASIS}
      </p>
      <button type="button" className="mt-3 text-xs underline" onClick={onToggle}>
        {open ? 'Hide metric detail' : 'Show metric detail'}
      </button>
      {open && (
        <div className="mt-3 border-t border-gray-100 pt-3 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
          <p>
            {aggregate.estimated_calls > 0
              ? `${number(aggregate.estimated_calls)} recordings include an estimated usage value.`
              : 'No recording used an estimated usage value.'}
          </p>
          {unknown.length === 0 ? (
            <p className="mt-1">Every recorded metric came back with a value.</p>
          ) : (
            <ul className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
              {unknown.map(([metric, count]) => (
                <li key={metric}>
                  {metric}: {number(count)} recordings unknown
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

/** The exact filter a row links to: a day keeps its intersection, a key its identity. */
function runsTarget(
  dimension: Dimension,
  row: { key: string | null; since?: string; until?: string },
): RunsTarget {
  return dimension === 'day'
    ? { dimension: 'day', since: row.since, until: row.until }
    : { dimension, value: row.key };
}

/** What the ranked table shows, including the groups folded into the remainder. */
function rankedBasis(shown: number, groupCount: number): string {
  return `Top ${shown} groups by recorded cost, out of ${number(
    groupCount,
  )}; a group with no recorded cost is listed last.`;
}

/**
 * What an error offers next.
 *
 * A broken link needs a selection, not a retry: it cannot succeed until the
 * admin applies one. Any other failure is worth retrying as it stands.
 */
function ErrorRecovery({
  linkError,
  showRetry,
  retry,
}: {
  linkError: boolean;
  showRetry: boolean;
  retry: () => void;
}) {
  if (linkError) return <> Choose a window and apply it to read a selection instead.</>;
  if (!showRetry) return null;
  return (
    <button type="button" className="ml-3 underline" onClick={retry}>
      Retry
    </button>
  );
}

function dayRows(usage: AdminUsage) {
  return usage.days.map((day) => ({
    key: day.date,
    since: day.since,
    until: day.until,
    usage: day.usage,
  }));
}

function breakdownGroups(usage: AdminUsage, dimension: 'user_id' | 'model') {
  return usage[breakdownKey(dimension)].groups.map((group) => ({
    key: group.key,
    usage: group.usage,
  }));
}

function Breakdown({
  usage,
  dimension,
  onDimension,
  filters,
}: {
  usage: AdminUsage;
  dimension: Dimension;
  onDimension: (value: Dimension) => void;
  filters: AdminUsageFilters;
}) {
  const label = DIMENSIONS.find((entry) => entry.key === dimension) ?? DIMENSIONS[0];
  const rows = dimension === 'day' ? dayRows(usage) : breakdownGroups(usage, dimension);
  // Day rows are never ranked, so only the two identity dimensions have a
  // remainder row.
  const other = dimension === 'day' ? null : usage[breakdownKey(dimension)].other;
  const otherGroups =
    dimension === 'day' ? 0 : usage[breakdownKey(dimension)].group_count - rows.length;
  const breakdownBasis =
    dimension === 'day'
      ? 'Every UTC creation day in the selected window.'
      : rankedBasis(rows.length, usage[breakdownKey(dimension)].group_count);

  return (
    <section className={`${CARD} mt-5 overflow-hidden`} aria-labelledby="breakdown-heading">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-gray-100 px-5 py-4 dark:border-white/5">
        <div>
          <h3
            id="breakdown-heading"
            className="text-sm font-semibold text-gray-900 dark:text-gray-50"
          >
            Breakdown
          </h3>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{breakdownBasis}</p>
        </div>
        <div className="flex gap-1">
          {DIMENSIONS.map((entry) => (
            <button
              key={entry.key}
              type="button"
              aria-pressed={dimension === entry.key}
              className={`${BUTTON} ${dimension === entry.key ? 'border-crimson text-crimson' : ''}`}
              onClick={() => onDimension(entry.key)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
            <tr>
              {[
                label.column,
                'Runs',
                'Recorded cost',
                'Calls',
                'Input',
                'Output',
                'With snapshot',
                '',
              ].map((column, index) => (
                <th
                  key={column || 'actions'}
                  className={`whitespace-nowrap px-5 py-3 font-medium ${
                    index > 0 && index < 7 ? 'text-right' : ''
                  }`}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-white/5">
            {rows.length === 0 && (
              <tr>
                <td
                  colSpan={8}
                  className="px-5 py-6 text-center text-sm text-gray-500 dark:text-gray-400"
                >
                  No runs in this period.
                </td>
              </tr>
            )}
            {rows.map((row) => (
              <BreakdownRow
                key={row.key ?? 'unknown'}
                row={row}
                dimension={dimension}
                filters={filters}
              />
            ))}
            {other && (
              <BreakdownRow
                row={{ key: null, usage: other.usage, otherGroups }}
                dimension={dimension}
                filters={filters}
                isOther
              />
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function BreakdownRow({
  row,
  dimension,
  filters,
  isOther = false,
}: {
  row: {
    key: string | null;
    usage: AdminUsageAggregate;
    since?: string;
    until?: string;
    otherGroups?: number;
  };
  dimension: Dimension;
  filters: AdminUsageFilters;
  isOther?: boolean;
}) {
  const cost = money(row.usage.totals?.spent_usd);
  const href = isOther ? null : runsHref(filters, runsTarget(dimension, row));
  const name = isOther
    ? `Other · ${number(row.otherGroups ?? 0)} groups`
    : dimension === 'day'
      ? utcDate(row.since)
      : groupLabel(dimension, { key: row.key, usage: row.usage });
  return (
    <tr className="align-top transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03]">
      <th scope="row" className="max-w-xs px-5 py-3 font-normal">
        <span
          className="block truncate font-medium text-gray-900 dark:text-gray-100"
          title={row.key ?? undefined}
        >
          {name}
        </span>
        {dimension === 'day' && (
          <span className="text-xs text-gray-500 dark:text-gray-400">
            Runs created on this UTC day
          </span>
        )}
      </th>
      <Cell>{number(row.usage.run_count)}</Cell>
      <Cell title={cost.exact ?? undefined}>{cost.display}</Cell>
      <Cell>{row.usage.totals ? number(row.usage.totals.calls) : 'Unavailable'}</Cell>
      <Cell>{number(row.usage.totals?.tokens_in)}</Cell>
      <Cell>{number(row.usage.totals?.tokens_out)}</Cell>
      <Cell>
        {number(row.usage.runs_with_snapshot)} / {number(row.usage.run_count)}
      </Cell>
      <td className="px-5 py-3 text-right">
        {href ? (
          <Link
            href={href}
            className="text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
          >
            View conversations
          </Link>
        ) : (
          <span className="text-xs text-gray-400 dark:text-gray-500">
            {isOther ? 'Not a single filter' : 'Cannot be filtered exactly'}
          </span>
        )}
      </td>
    </tr>
  );
}

function Cell({ children, title }: { children: React.ReactNode; title?: string }) {
  return (
    <td
      className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300"
      title={title}
    >
      {children}
    </td>
  );
}

function UsageSkeleton() {
  return (
    <>
      <div className="mt-6 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: SKELETON_CARDS }, (_, index) => (
          <div key={index} className={`${CARD} h-28 animate-pulse p-5`} />
        ))}
      </div>
      <div className={`${CARD} mt-5 h-40 animate-pulse`} />
      <div className={`${CARD} mt-5 h-64 animate-pulse`} />
    </>
  );
}
