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
import { accessDenied, ago, BUTTON, dateTime, ErrorNotice, RunState } from './shared';

// The column set is fixed: this list is metadata only, so every cell maps to a
// field the runs endpoint already returns. Resources are three columns rather
// than one because "measured a little" and "measured nothing" must not read
// alike; process I/O is deliberately absent, since it belongs to the detail
// view and would widen the list past what a reader scans here. Every cell is
// one line: what does not fit is in the cell's tooltip.
const COLUMNS = [
  { label: 'Turn', width: 340 },
  { label: 'User', width: 144 },
  { label: 'Status', width: 104 },
  { label: 'Agent / model', width: 216 },
  { label: 'Host', width: 120 },
  { label: 'Tool calls', width: 88 },
  { label: 'CPU', width: 152 },
  { label: 'Peak memory', width: 176 },
  { label: 'Resources', width: 128 },
  { label: 'Created', width: 96 },
] as const;

// Keep readable column widths instead of squeezing metrics into percentages.
const TABLE_WIDTH = COLUMNS.reduce((total, column) => total + column.width, 0);

//: The right-aligned columns: counts, measurements and the time.
const NUMERIC_COLUMNS = new Set(['Tool calls', 'CPU', 'Peak memory', 'Created']);

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

/** The repository's own name; the owner is the same for nearly every row. */
function repoName(repo: string): string {
  return repo.split('/').pop() || repo;
}

//: The row's frame on the page background. The sticky first cell needs an
//: opaque copy of it, or scrolled cells would show through.
const ROW_HOVER = 'transition-colors hover:bg-surface-hover/50';
const STICKY_CELL =
  'bg-surface-raised group-hover:bg-surface-hover lg:sticky lg:left-0 lg:z-10 lg:shadow-[inset_-1px_0_0_rgb(var(--ui-border))]';

function RunsTable({ runs }: { runs: AdminRunsPage['runs'] }) {
  const now = Date.now();
  return (
    <RunsTableScroll>
      <table className="w-full table-fixed text-left text-sm" style={{ minWidth: TABLE_WIDTH }}>
        <colgroup>
          {COLUMNS.map(({ label, width }) => (
            <col key={label} style={{ width }} />
          ))}
        </colgroup>
        <thead className="bg-surface-muted text-xs text-muted">
          <tr className="border-b border-border">
            {COLUMNS.map(({ label }) => (
              <th
                key={label}
                className={`whitespace-nowrap px-3 py-3 font-medium ${
                  NUMERIC_COLUMNS.has(label) ? 'text-right' : ''
                } ${label === 'Turn' ? STICKY_CELL : ''}`}
                scope="col"
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {runs.map((run) => {
            // Derived from this row's own summary, never from a previous
            // row's: a cell cannot survive a filter or page change.
            const cells = runResourceCells(run.resource_summary);
            return (
              <tr key={run.id} className={`group ${ROW_HOVER}`}>
                <th scope="row" className={`px-3 py-2.5 font-normal ${STICKY_CELL}`}>
                  <div className="flex min-w-0 items-baseline gap-2">
                    <Link
                      href={`/admin/runs/${encodeURIComponent(run.id)}`}
                      title={run.id}
                      className="shrink-0 rounded-sm font-medium text-gray-900 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:text-gray-100 dark:focus-visible:ring-crimson-light"
                    >
                      Turn {run.turn_no}
                    </Link>
                    {run.title && (
                      <span
                        className="min-w-0 truncate text-gray-600 dark:text-gray-300"
                        title={run.title}
                      >
                        {run.title}
                      </span>
                    )}
                    {/* The repository gives way first, so a narrow table keeps titles. */}
                    <span
                      className="min-w-0 shrink-[4] truncate text-xs text-gray-400 dark:text-gray-500"
                      title={run.repo}
                    >
                      {repoName(run.repo)}
                    </span>
                  </div>
                </th>
                <td className="px-3 py-2.5">
                  <span
                    className="block truncate font-mono text-xs text-gray-500 dark:text-gray-400"
                    title={run.user_id}
                  >
                    {run.user_id}
                  </span>
                </td>
                <td className="overflow-hidden whitespace-nowrap px-3 py-2.5">
                  <RunState state={run.state} quiet />
                </td>
                <td className="px-3 py-2.5">
                  <span
                    className="block truncate text-xs text-gray-500 dark:text-gray-400"
                    title={`${run.runtime} · ${run.model}`}
                  >
                    {run.runtime} · {run.model}
                  </span>
                </td>
                <td className="px-3 py-2.5">
                  <RunHosts hosts={run.physical_hosts ?? run.hosts} />
                </td>
                <td className="px-3 py-2.5 text-right tabular-nums text-gray-700 dark:text-gray-200">
                  <span className="block truncate" title={run.tool_calls.toLocaleString()}>
                    {run.tool_calls.toLocaleString()}
                  </span>
                </td>
                {[cells.cpu, cells.memory].map((cell, index) => (
                  <td
                    key={COLUMNS[index + 6].label}
                    className="px-3 py-2.5 text-right text-xs tabular-nums text-gray-700 dark:text-gray-200"
                  >
                    <span className="block truncate" title={cell.title ?? cell.text}>
                      {cell.text}
                    </span>
                  </td>
                ))}
                <td className="overflow-hidden whitespace-nowrap px-3 py-2.5">
                  <RunResourceState cells={cells} />
                </td>
                <td
                  className="whitespace-nowrap px-3 py-2.5 text-right text-xs text-gray-500 dark:text-gray-400"
                  title={dateTime(run.created_at)}
                >
                  {ago(run.created_at, now)}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </RunsTableScroll>
  );
}

//: One dot per resource completeness, beside a grey word, like the status
//: column. "Not collected" keeps its word rather than a dash: a dash reads as
//: a zero, and no measurement is not a measured zero.
const RESOURCE_STATE_DOTS: Record<ResourceCompleteness, string> = {
  complete: 'bg-emerald-500',
  partial: 'bg-amber-500',
  collecting: 'bg-blue-500',
  interrupted: 'bg-orange-500',
  unavailable: 'bg-gray-400 dark:bg-gray-500',
};

function RunResourceState({ cells }: { cells: TurnResourceCells }) {
  const title = cells.state.title ?? cells.state.text;
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-gray-500 dark:text-gray-400"
      title={title}
    >
      <span
        aria-hidden
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${RESOURCE_STATE_DOTS[cells.completeness]}`}
      />
      {cells.state.text}
    </span>
  );
}

function LoadingTable() {
  return (
    <div className="mt-4 overflow-hidden" role="status" aria-label="Loading turns">
      <span className="sr-only">Loading turns…</span>
      <div className="divide-y divide-border">
        {Array.from({ length: SKELETON_ROWS }, (_, row) => (
          <div key={row} className="flex items-center gap-5 px-3 py-3">
            <div className="h-3.5 w-1/4 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-3.5 w-1/6 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-3.5 w-16 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-3.5 flex-1 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
            <div className="h-3.5 w-20 animate-pulse rounded bg-gray-100 dark:bg-white/5" />
          </div>
        ))}
      </div>
    </div>
  );
}

function EmptyRuns() {
  return (
    <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
      No turns match these filters. Widen the time window or adjust the filters.
    </p>
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
            className="min-w-0 truncate font-mono text-xs text-gray-500 dark:text-gray-400"
          >
            {hosts[0]}
          </span>
          {hosts.length > 1 && (
            <span aria-hidden className="shrink-0 text-xs text-gray-400 dark:text-gray-500">
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
