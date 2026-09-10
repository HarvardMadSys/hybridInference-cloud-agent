'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentJobApiState } from '@/lib/api/agents';
import { getAdminRuns, type AdminRunFilters, type AdminRunsPage } from '@/lib/api/adminRuns';
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
  RunState,
} from './shared';

const STATES: AgentJobApiState[] = [
  'waiting',
  'queued',
  'running',
  'publishing',
  'succeeded',
  'failed',
  'cancelled',
];

// The column set is fixed: this list is metadata only, so every cell maps to a
// field the runs endpoint already returns.
const COLUMNS = [
  { label: 'Run', width: '27%' },
  { label: 'User', width: '11%' },
  { label: 'Status', width: '10%' },
  { label: 'Agent / model', width: '16%' },
  { label: 'Host', width: '12%' },
  { label: 'Tool calls', width: '9%' },
  { label: 'Created', width: '15%' },
] as const;

const SKELETON_ROWS = 6;

function windowFilters(days: number, state?: AgentJobApiState): AdminRunFilters {
  const until = new Date();
  return {
    state,
    since: new Date(until.getTime() - days * 86_400_000).toISOString(),
    until: until.toISOString(),
    limit: 50,
  };
}

export default function AdminRunsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return <RunsList key={state.user?.id ?? 'admin'} />;
}

function RunsList() {
  const [days, setDays] = useState(7);
  const [filters, setFilters] = useState<AdminRunFilters>(() => windowFilters(7));
  const [page, setPage] = useState<AdminRunsPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const moreRequest = useRef<AbortController | null>(null);

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
  }, [filters]);

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
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-10 lg:px-12 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="runs" />

        <div className="mt-8">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Runs</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Cross-user agent runs in the selected window.
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
              value={days}
              onChange={(e) => {
                const nextDays = Number(e.target.value);
                setDays(nextDays);
                setFilters((current) => windowFilters(nextDays, current.state));
              }}
            >
              <option value={7}>Last 7 days</option>
              <option value={30}>Last 30 days</option>
              <option value={90}>Last 90 days</option>
            </select>
          </label>
          <label className="w-40 shrink-0">
            <span className="sr-only">Status</span>
            <select
              className={INPUT}
              value={filters.state ?? ''}
              onChange={(e) =>
                setFilters(
                  windowFilters(
                    days,
                    (e.target.value || undefined) as AgentJobApiState | undefined,
                  ),
                )
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
            onClick={() => setFilters((current) => windowFilters(days, current.state))}
          >
            Refresh
          </button>
        </div>

        {error && (
          <ErrorNotice>
            {error}
            {!page && (
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

        {loading ? (
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
    <div className={`${CARD} mt-3 overflow-hidden`}>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[1180px] table-fixed text-left text-sm">
          <colgroup>
            {COLUMNS.map(({ label, width }) => (
              <col key={label} style={{ width }} />
            ))}
          </colgroup>
          <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
            <tr>
              {COLUMNS.map(({ label }, index) => (
                <th
                  key={label}
                  className={`whitespace-nowrap px-4 py-3 font-medium ${
                    index === 5 ? 'text-right' : ''
                  }`}
                >
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 dark:divide-white/5">
            {runs.map((run) => (
              <tr
                key={run.id}
                className="h-16 align-middle transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03]"
              >
                <th scope="row" className="px-4 py-3 font-normal">
                  <div className="flex min-w-0 items-baseline gap-2 leading-5">
                    <Link
                      href={`/admin/runs/${encodeURIComponent(run.id)}`}
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
                  {run.hosts.length > 0 ? (
                    <div className="flex min-w-0 items-center gap-1" title={run.hosts.join(', ')}>
                      <span className="sr-only">{run.hosts.join(', ')}</span>
                      <span
                        aria-hidden
                        className="min-w-0 truncate rounded-md bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-600 dark:bg-white/5 dark:text-gray-300"
                      >
                        {run.hosts[0]}
                      </span>
                      {run.hosts.length > 1 && (
                        <span
                          aria-hidden
                          className="shrink-0 text-xs text-gray-500 dark:text-gray-400"
                        >
                          +{run.hosts.length - 1}
                        </span>
                      )}
                    </div>
                  ) : (
                    <span className="block truncate text-xs text-gray-400 dark:text-gray-500">
                      Unassigned
                    </span>
                  )}
                </td>
                <td className="px-4 py-3 text-right tabular-nums text-gray-800 dark:text-gray-200">
                  <span className="block truncate" title={run.tool_calls.toLocaleString()}>
                    {run.tool_calls.toLocaleString()}
                  </span>
                </td>
                <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400">
                  <span className="block truncate" title={dateTime(run.created_at)}>
                    {dateTime(run.created_at)}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
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
