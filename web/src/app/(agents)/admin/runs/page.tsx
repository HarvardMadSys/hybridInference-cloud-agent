'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentJobApiState } from '@/lib/api/agents';
import { getAdminRuns, type AdminRunFilters, type AdminRunsPage } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { presetKey } from '../usage/present';
import { RunsTableScroll } from './RunsTableScroll';
import {
  runResourceCells,
  type ResourceCompleteness,
  type TurnResourceCells,
} from './presentTurnResources';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  INPUT,
  RunState,
} from './shared';

const STATES: AgentJobApiState[] = ['queued', 'running', 'succeeded', 'failed', 'cancelled'];

// The column set is fixed: this list is metadata only, so every cell maps to a
// field the runs endpoint already returns. Resources are three columns rather
// than one because "measured a little" and "measured nothing" must not read
// alike; process I/O is deliberately absent, since it belongs to the detail
// view and would widen the list past what a reader scans here.
const COLUMNS = [
  { label: 'Run', width: 320 },
  { label: 'User', width: 160 },
  { label: 'Status', width: 128 },
  { label: 'Agent / model', width: 200 },
  { label: 'Host', width: 144 },
  { label: 'Tool calls', width: 104 },
  { label: 'CPU', width: 224 },
  { label: 'Peak memory', width: 272 },
  { label: 'Resources', width: 152 },
  { label: 'Created', width: 264 },
] as const;

// Keep readable column widths instead of squeezing metrics into percentages.
const TABLE_WIDTH = COLUMNS.reduce((total, column) => total + column.width, 0);

//: The numeric columns, which are right-aligned like the tool-call count.
const NUMERIC_COLUMNS = new Set(['Tool calls', 'CPU', 'Peak memory']);

const SKELETON_ROWS = 6;

//: Said only when the list came from a usage breakdown row, where two of the
//: filters are identities no control on this page displays.
const LINKED_GROUP_BASIS =
  'the usage filters from that page, including the user, model, agent or repository the row was grouped by';

const DEFAULT_WINDOW_MS = 7 * 86_400_000;

/** How long the applied window is, read from the window itself. */
function windowLength(filters: AdminRunFilters): number {
  const span = Date.parse(filters.until ?? '') - Date.parse(filters.since ?? '');
  return Number.isFinite(span) && span > 0 ? span : DEFAULT_WINDOW_MS;
}

/**
 * Re-anchor a window of a given length at an instant.
 *
 * The length is taken from the window on screen rather than from the preset
 * control: the control reads "Custom" for a linked or a grown-old window, and
 * a preset key is not a number of days.
 */
function windowOfLength(
  length: number,
  keep: AdminRunFilters = {},
  now: Date = new Date(),
): AdminRunFilters {
  return {
    ...keep,
    since: new Date(now.getTime() - length).toISOString(),
    until: now.toISOString(),
    limit: 50,
  };
}

function windowFilters(
  days: number,
  keep: AdminRunFilters = {},
  now: Date = new Date(),
): AdminRunFilters {
  return windowOfLength(days * 86_400_000, keep, now);
}

// Deep links from the Usage page arrive as query parameters. Only these are
// read. A link that names a window this list cannot use is an error rather than
// a fallback: answering with the default window would describe other runs, and
// a missing end or an inverted one is a broken link, not an empty selection.
const DEEP_LINK_KEYS = ['user_id', 'model', 'runtime', 'repo'] as const;

interface RunLink {
  filters: AdminRunFilters | null;
  error: string | null;
  linked: boolean;
}

function deepLink(query: string): RunLink {
  const params = new URLSearchParams(query);
  const unknown = [...params.keys()].filter(
    (key) =>
      key !== 'since' && key !== 'until' && !(DEEP_LINK_KEYS as readonly string[]).includes(key),
  );
  if (unknown.length) {
    return { filters: null, error: `Unsupported filter: ${unknown[0]}`, linked: true };
  }
  const since = params.get('since');
  const until = params.get('until');
  const named = Boolean(since || until) || DEEP_LINK_KEYS.some((key) => params.get(key));
  if (!named) return { filters: null, error: null, linked: false };
  if (!since || !until) {
    return { filters: null, error: 'This link is missing one end of its window.', linked: true };
  }
  if (!(Date.parse(since) < Date.parse(until))) {
    return { filters: null, error: 'This link has an empty or inverted window.', linked: true };
  }
  const filters: AdminRunFilters = { since, until, limit: 50 };
  for (const key of DEEP_LINK_KEYS) {
    const value = params.get(key);
    if (value) filters[key] = value;
  }
  return { filters, error: null, linked: true };
}

export default function AdminRunsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<RunsSkeletonFallback />}>
      <LinkedRunsList key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
}

/** Read the linked filters once, keyed by the query so a new link remounts. */
function LinkedRunsList() {
  const query = useSearchParams().toString();
  return <RunsList key={query} initialQuery={query} />;
}

function RunsSkeletonFallback() {
  return (
    <div className="min-w-0 flex-1">
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <div className={`${CARD} mt-8 h-64 animate-pulse`} />
      </section>
    </div>
  );
}

function RunsList({ initialQuery }: { initialQuery: string }) {
  // A link from the usage page carries an absolute window and the identities
  // of the group it came from. It replaces the seven-day default and is kept
  // for as long as the admin stays on this selection.
  const linked = deepLink(initialQuery);
  const [filters, setFilters] = useState<AdminRunFilters>(() => linked.filters ?? windowFilters(7));
  // The window the admin is actually looking at, so the control names it
  // instead of claiming the last preset that was clicked. A one-day link is
  // Custom, and changing Status must not re-anchor it to seven days.
  const windowDays = presetKey(filters);
  // A linked window is absolute and stays exactly as linked; a window chosen
  // here re-anchors on Refresh so the list picks up work finished meanwhile.
  const [linkedWindow, setLinkedWindow] = useState(linked.linked);
  const [page, setPage] = useState<AdminRunsPage | null>(null);
  const [loading, setLoading] = useState(!linked.error);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(linked.error);
  const [denied, setDenied] = useState(false);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    if (linked.error) {
      // A link this list cannot use is reported as it is. Requesting with a
      // substituted window would return runs the link never named.
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    moreRequest.current?.abort();
    setMoreLoading(false);
    setPage(null);
    setLoading(true);
    setError(null);
    getAdminRuns(filters, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setPage(next);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          if (accessDenied(e)) setDenied(true);
          else setError(getErrorMessage(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      moreRequest.current?.abort();
    };
  }, [filters, linked.error]);

  async function loadMore() {
    if (!page?.next_cursor || moreLoading) return;
    moreRequest.current?.abort();
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminRuns(
        { ...filters, since: page.since, until: page.until, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) =>
          previous ? { ...next, runs: [...previous.runs, ...next.runs] } : next,
        );
    } catch (e) {
      if (!controller.signal.aborted) {
        if (accessDenied(e)) setDenied(true);
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  if (denied) return <AdminRequired />;
  return (
    <div className="min-w-0 flex-1">
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="runs" />

        <div className="mt-8">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Runs</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            {linked
              ? `Cross-user agent runs from a linked usage group: ${LINKED_GROUP_BASIS}`
              : 'Cross-user agent runs in the selected window.'}
          </p>
        </div>

        <div
          role="group"
          aria-label="Run filters"
          className="mt-5 flex flex-wrap items-center gap-2"
        >
          <label className="w-40 shrink-0">
            <span className="sr-only">Created</span>
            <select
              className={INPUT}
              value={windowDays}
              onChange={(e) => {
                // Choosing a preset is the only action that re-anchors the
                // window, and it keeps every other filter, including the
                // identity a usage link arrived with.
                const nextDays = Number(e.target.value);
                setLinkedWindow(false);
                setFilters((current) => windowFilters(nextDays, current));
              }}
            >
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
              {windowDays === 'custom' && <option value="custom">Custom</option>}
            </select>
          </label>
          <label className="w-40 shrink-0">
            <span className="sr-only">Status</span>
            <select
              className={INPUT}
              value={filters.state ?? ''}
              onChange={(e) =>
                // Status is not part of the window. Changing it keeps the exact
                // since/until on screen: re-deriving them from a preset here
                // widened a one-day link back to seven days.
                setFilters((current) => ({
                  ...current,
                  state: (e.target.value || undefined) as AgentJobApiState | undefined,
                }))
              }
            >
              <option value="">All statuses</option>
              {STATES.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
          </label>
          <button
            className={`${BUTTON} ml-auto h-10`}
            type="button"
            disabled={loading}
            onClick={() =>
              setFilters((current) =>
                // A linked window is absolute and stays as linked. Any other
                // window keeps its own length and re-anchors at now, which is
                // what refreshing a list is for.
                linkedWindow ? { ...current } : windowOfLength(windowLength(current), current),
              )
            }
          >
            Refresh
          </button>
        </div>

        {error && (
          <ErrorNotice>
            {error}
            {/* A broken link has nothing to retry: the URL has to change. */}
            {!page && !linked.error && (
              <button
                type="button"
                className="ml-3 underline"
                onClick={() => setFilters({ ...filters })}
              >
                Retry
              </button>
            )}
          </ErrorNotice>
        )}

        {linked.error ? null : loading ? (
          <LoadingTable />
        ) : (
          page && (
            <>
              <p className="mt-6 text-xs text-gray-500 dark:text-gray-400">
                Created {dateTime(page.since)} – {dateTime(page.until)}
              </p>
              {page.runs.length === 0 ? <EmptyRuns /> : <RunsTable runs={page.runs} />}
              {page.next_cursor && (
                <div className="mt-5 flex justify-center">
                  <button
                    type="button"
                    className={BUTTON}
                    disabled={moreLoading}
                    onClick={() => void loadMore()}
                  >
                    {moreLoading ? 'Loading…' : 'Load more runs'}
                  </button>
                </div>
              )}
            </>
          )
        )}
      </section>
    </div>
  );
}

function RunsTable({ runs }: { runs: AdminRunsPage['runs'] }) {
  return (
    <RunsTableScroll>
      <table className="w-full table-fixed text-left text-sm" style={{ minWidth: TABLE_WIDTH }}>
        <colgroup>
          {COLUMNS.map(({ label, width }) => (
            <col key={label} style={{ width }} />
          ))}
        </colgroup>
        <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
          <tr>
            {COLUMNS.map(({ label }) => (
              <th
                key={label}
                className={`whitespace-nowrap px-4 py-3 font-medium ${
                  NUMERIC_COLUMNS.has(label) ? 'text-right' : ''
                } ${label === 'Run' ? 'bg-gray-50 dark:bg-gray-900 lg:sticky lg:left-0 lg:z-10 lg:shadow-[inset_-1px_0_0_#e5e7eb] dark:lg:shadow-[inset_-1px_0_0_#374151]' : ''}`}
                scope="col"
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {runs.map((run) => {
            // Derived from this row's own summary, never from a previous
            // row's: a cell cannot survive a filter or page change.
            const cells = runResourceCells(run.resource_summary);
            return (
              <tr
                key={run.id}
                className="group h-16 align-middle transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03]"
              >
                <th
                  scope="row"
                  className="bg-white px-4 py-3 font-normal group-hover:bg-gray-50 dark:bg-gray-900 dark:group-hover:bg-gray-800 lg:sticky lg:left-0 lg:z-10 lg:shadow-[inset_-1px_0_0_#e5e7eb] dark:lg:shadow-[inset_-1px_0_0_#374151]"
                >
                  <div className="flex min-w-0 items-baseline gap-2 leading-5">
                    <Link
                      href={`/admin/runs/${encodeURIComponent(run.id)}`}
                      title={run.id}
                      className="min-w-0 truncate rounded-sm font-mono text-xs font-medium text-crimson hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson focus-visible:ring-offset-2 focus-visible:ring-offset-white dark:text-crimson-light dark:focus-visible:ring-crimson-light dark:focus-visible:ring-offset-gray-900"
                    >
                      {run.id}
                    </Link>
                    <span
                      className="max-w-20 shrink-0 truncate text-xs text-gray-500 dark:text-gray-400"
                      title={`Turn ${run.turn_no}`}
                    >
                      Turn {run.turn_no}
                    </span>
                  </div>
                  <span
                    className="mt-1 block truncate text-xs leading-4 text-gray-500 dark:text-gray-400"
                    title={run.repo}
                  >
                    {run.repo}
                  </span>
                </th>
                <td className="px-4 py-3">
                  <span
                    className="block truncate font-mono text-xs text-gray-600 dark:text-gray-300"
                    title={run.user_id}
                  >
                    {run.user_id}
                  </span>
                </td>
                <td className="overflow-hidden whitespace-nowrap px-4 py-3">
                  <RunState state={run.state} />
                </td>
                <td className="px-4 py-3">
                  <span
                    className="block truncate leading-5 text-gray-800 dark:text-gray-200"
                    title={run.runtime}
                  >
                    {run.runtime}
                  </span>
                  <span
                    className="mt-1 block truncate text-xs leading-4 text-gray-500 dark:text-gray-400"
                    title={run.model}
                  >
                    {run.model}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <RunHosts hosts={run.physical_hosts ?? run.hosts} />
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-800 dark:text-gray-200">
                  <span className="block break-words" title={run.tool_calls.toLocaleString()}>
                    {run.tool_calls.toLocaleString()}
                  </span>
                </td>
                {[cells.cpu, cells.memory].map((cell, index) => (
                  <td
                    key={COLUMNS[index + 6].label}
                    className="px-4 py-3 text-right tabular-nums text-gray-800 dark:text-gray-200"
                  >
                    <span className="block break-words" title={cell.title ?? cell.text}>
                      {cell.text}
                    </span>
                  </td>
                ))}
                <td className="overflow-hidden whitespace-nowrap px-4 py-3">
                  <RunResourceState cells={cells} />
                </td>
                <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                  <span className="block" title={dateTime(run.created_at)}>
                    {dateTime(run.created_at)}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </RunsTableScroll>
  );
}

//: One tinted pill per resource completeness, in the same shape the run-state
//: column uses. "Not collected" is a neutral pill rather than a dash: a dash
//: reads as a zero, and no measurement is not a measured zero.
const RESOURCE_STATE_STYLES: Record<ResourceCompleteness, { badge: string; dot: string }> = {
  complete: {
    badge:
      'bg-emerald-50 text-emerald-700 ring-emerald-600/20 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/25',
    dot: 'bg-emerald-500',
  },
  partial: {
    badge:
      'bg-amber-50 text-amber-700 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-400 dark:ring-amber-500/25',
    dot: 'bg-amber-500',
  },
  collecting: {
    badge:
      'bg-blue-50 text-blue-700 ring-blue-600/20 dark:bg-blue-500/10 dark:text-blue-400 dark:ring-blue-500/25',
    dot: 'bg-blue-500',
  },
  interrupted: {
    badge:
      'bg-orange-50 text-orange-700 ring-orange-600/20 dark:bg-orange-500/10 dark:text-orange-400 dark:ring-orange-500/25',
    dot: 'bg-orange-500',
  },
  unavailable: {
    badge:
      'bg-gray-100 text-gray-600 ring-gray-500/20 dark:bg-white/5 dark:text-gray-400 dark:ring-white/15',
    dot: 'bg-gray-500 dark:bg-gray-400',
  },
};

function RunResourceState({ cells }: { cells: TurnResourceCells }) {
  const style = RESOURCE_STATE_STYLES[cells.completeness];
  const title = cells.state.title ?? cells.state.text;
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${style.badge}`}
      title={title}
    >
      <span aria-hidden className={`h-1.5 w-1.5 shrink-0 rounded-full ${style.dot}`} />
      {cells.state.text}
    </span>
  );
}

function LoadingTable() {
  return (
    <div className={`${CARD} mt-6 overflow-hidden`} role="status" aria-label="Loading runs">
      <span className="sr-only">Loading runs…</span>
      <div className="divide-y divide-gray-100 dark:divide-white/5">
        {Array.from({ length: SKELETON_ROWS }, (_, row) => (
          <div key={row} className="flex items-center gap-5 px-5 py-4">
            <div className="h-4 w-1/4 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-4 w-1/6 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-5 w-20 animate-pulse rounded-full bg-gray-100 dark:bg-white/5" />
            <div className="h-4 flex-1 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-4 w-24 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
          </div>
        ))}
      </div>
    </div>
  );
}

function EmptyRuns() {
  return (
    <div className={`${CARD} mt-3 flex flex-col items-center gap-2 px-6 py-16 text-center`}>
      <svg
        aria-hidden
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        className="h-8 w-8 text-gray-300 dark:text-gray-600"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M21 21l-4.35-4.35M17 10.5a6.5 6.5 0 11-13 0 6.5 6.5 0 0113 0z"
        />
      </svg>
      <p className="text-sm font-medium text-gray-700 dark:text-gray-200">
        No runs match these filters.
      </p>
      <p className="text-xs text-gray-500 dark:text-gray-400">
        Widen the time window or adjust the filters.
      </p>
    </div>
  );
}

function RunHosts({ hosts }: { hosts: string[] }) {
  return (
    <>
      {hosts.length > 0 ? (
        <div className="flex min-w-0 items-center gap-1" title={hosts.join(', ')}>
          <span className="sr-only">{hosts.join(', ')}</span>
          <span
            aria-hidden
            className="min-w-0 truncate rounded-md bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-600 dark:bg-white/5 dark:text-gray-300"
          >
            {hosts[0]}
          </span>
          {hosts.length > 1 && (
            <span aria-hidden className="shrink-0 text-xs text-gray-500 dark:text-gray-400">
              +{hosts.length - 1}
            </span>
          )}
        </div>
      ) : (
        <span className="block truncate text-xs text-gray-400 dark:text-gray-500">Unassigned</span>
      )}
    </>
  );
}
