'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { getAdminRuns, type AdminRunFilters, type AdminRunsPage } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { RunsTableScroll } from './RunsTableScroll';
import {
  runResourceCells,
  type ResourceCompleteness,
  type TurnResourceCells,
} from './presentTurnResources';
import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice, RunState } from './shared';

// The column set is fixed: this list is metadata only, so every cell maps to a
// field the runs endpoint already returns. Resources are three columns rather
// than one because "measured a little" and "measured nothing" must not read
// alike; process I/O is deliberately absent, since it belongs to the detail
// view and would widen the list past what a reader scans here.
const COLUMNS = [
  { label: 'Turn', width: 320 },
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

/**
 * The turn level of the Sessions tab: every matching turn, from every session,
 * newest first.
 *
 * The filters belong to the page, so switching between sessions and turns
 * keeps the selection; only what one row stands for changes.
 */
export function TurnResults({
  filters,
  onLoading,
  onDenied,
}: {
  filters: AdminRunFilters;
  onLoading: (loading: boolean) => void;
  onDenied: () => void;
}) {
  const [page, setPage] = useState<AdminRunsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => onLoading(loading), [loading, onLoading]);

  useEffect(() => {
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
          if (accessDenied(e)) onDenied();
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
  }, [filters, revision, onDenied]);

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
        if (accessDenied(e)) onDenied();
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  return (
    <>
      {error && (
        <ErrorNotice>
          {error}
          {!page && (
            <button
              type="button"
              className="ml-3 underline"
              onClick={() => setRevision((value) => value + 1)}
            >
              Retry
            </button>
          )}
        </ErrorNotice>
      )}
      {loading ? (
        <LoadingTable />
      ) : (
        page && (
          <>
            <p className="mt-6 text-xs text-gray-500 dark:text-gray-400">
              Turns created {dateTime(page.since)} – {dateTime(page.until)}
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
                  {moreLoading ? 'Loading…' : 'Load more turns'}
                </button>
              </div>
            )}
          </>
        )
      )}
    </>
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
                } ${label === 'Turn' ? 'bg-gray-50 dark:bg-gray-900 lg:sticky lg:left-0 lg:z-10 lg:shadow-[inset_-1px_0_0_#e5e7eb] dark:lg:shadow-[inset_-1px_0_0_#374151]' : ''}`}
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
    <div className={`${CARD} mt-6 overflow-hidden`} role="status" aria-label="Loading turns">
      <span className="sr-only">Loading turns…</span>
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
        No turns match these filters.
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
