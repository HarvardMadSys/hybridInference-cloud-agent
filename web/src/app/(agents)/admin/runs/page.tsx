'use client';

import Link from 'next/link';
import { useEffect, useRef, useState, type FormEvent } from 'react';

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
  'Run',
  'User',
  'Status',
  'Agent / model',
  'Host',
  'Tool calls',
  'Created',
] as const;

const SKELETON_ROWS = 6;

function windowFilters(days: number, filters: AdminRunFilters = {}): AdminRunFilters {
  const until = new Date();
  return {
    ...filters,
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
  const [draft, setDraft] = useState<AdminRunFilters>({});
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

  function apply(event: FormEvent) {
    event.preventDefault();
    setFilters(windowFilters(days, draft));
  }

  if (denied) return <AdminRequired />;
  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-10 lg:px-12 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="runs" />

        <div className="mt-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Runs</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Cross-user agent runs in the selected window.
            </p>
          </div>
          <button
            className={BUTTON}
            type="button"
            disabled={loading}
            onClick={() => setFilters(windowFilters(days, draft))}
          >
            Refresh
          </button>
        </div>

        <form onSubmit={apply} aria-label="Run filters" className={`${CARD} mt-5 p-5`}>
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <label className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Created
              <select
                className={`${INPUT} mt-1.5`}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              >
                <option value={7}>Last 7 days</option>
                <option value={30}>Last 30 days</option>
                <option value={90}>Last 90 days</option>
              </select>
            </label>
            <label className="text-xs font-medium text-gray-500 dark:text-gray-400">
              Status
              <select
                className={`${INPUT} mt-1.5`}
                value={draft.state ?? ''}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    state: (e.target.value || undefined) as AgentJobApiState | undefined,
                  })
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
            {(
              [
                ['user_id', 'User ID'],
                ['repo', 'Repository'],
                ['runtime', 'Agent'],
                ['model', 'Model'],
                ['host', 'Host'],
              ] as const
            ).map(([key, label]) => (
              <label key={key} className="text-xs font-medium text-gray-500 dark:text-gray-400">
                {label}
                <input
                  className={`${INPUT} mt-1.5`}
                  value={draft[key] ?? ''}
                  placeholder="Any"
                  onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
                />
              </label>
            ))}
          </div>
          <div className="mt-5 flex justify-end border-t border-gray-100 pt-4 dark:border-white/5">
            <button type="submit" className={BUTTON}>
              Apply filters
            </button>
          </div>
        </form>

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
        <table className="min-w-full text-left text-sm">
          <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
            <tr>
              {COLUMNS.map((label, index) => (
                <th
                  key={label}
                  className={`whitespace-nowrap px-5 py-3 font-medium ${
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
                className="align-top transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03]"
              >
                <th scope="row" className="max-w-xs px-5 py-4 font-normal">
                  <Link
                    href={`/admin/runs/${encodeURIComponent(run.id)}`}
                    className="break-words font-medium text-crimson hover:underline dark:text-crimson-light"
                  >
                    {run.title || run.id}
                  </Link>
                  <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
                    <span className="break-all">{run.repo}</span>
                    <span aria-hidden className="text-gray-300 dark:text-gray-600">
                      ·
                    </span>
                    <span>Turn {run.turn_no}</span>
                  </div>
                </th>
                <td className="max-w-40 px-5 py-4">
                  <span
                    className="block truncate font-mono text-xs text-gray-600 dark:text-gray-300"
                    title={run.user_id}
                  >
                    {run.user_id}
                  </span>
                </td>
                <td className="px-5 py-4">
                  <RunState state={run.state} />
                </td>
                <td className="max-w-48 px-5 py-4">
                  <span className="block break-words text-gray-800 dark:text-gray-200">
                    {run.runtime}
                  </span>
                  <span className="mt-0.5 block break-words text-xs text-gray-500 dark:text-gray-400">
                    {run.model}
                  </span>
                </td>
                <td className="max-w-40 px-5 py-4">
                  {run.hosts.length > 0 ? (
                    <div className="flex min-w-0 flex-wrap gap-1">
                      {run.hosts.map((host) => (
                        <span
                          key={host}
                          className="min-w-0 max-w-full break-all rounded-md bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-600 dark:bg-white/5 dark:text-gray-300"
                        >
                          {host}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <span className="text-xs text-gray-400 dark:text-gray-500">Unassigned</span>
                  )}
                </td>
                <td className="px-5 py-4 text-right tabular-nums text-gray-800 dark:text-gray-200">
                  {run.tool_calls.toLocaleString()}
                </td>
                <td className="whitespace-nowrap px-5 py-4 text-xs text-gray-500 dark:text-gray-400">
                  {dateTime(run.created_at)}
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
