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
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="runs" />
        <div className="mt-8 flex items-center justify-between gap-4">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Runs</h2>
          <button
            className={BUTTON}
            type="button"
            disabled={loading}
            onClick={() => setFilters(windowFilters(days, draft))}
          >
            Refresh
          </button>
        </div>
        <form
          onSubmit={apply}
          aria-label="Run filters"
          className={`${CARD} mt-5 grid grid-cols-2 items-end gap-4 p-5 lg:grid-cols-4`}
        >
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
          <button type="submit" className={`${BUTTON} py-2`}>
            Apply filters
          </button>
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
          <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Loading runs…
          </p>
        ) : (
          page && (
            <>
              <p className="my-4 text-xs text-gray-500 dark:text-gray-400">
                Created {dateTime(page.since)} – {dateTime(page.until)}
              </p>
              {page.runs.length === 0 ? (
                <p className={`${CARD} p-8 text-center text-sm text-gray-500 dark:text-gray-400`}>
                  No runs match these filters.
                </p>
              ) : (
                <div className={`${CARD} overflow-x-auto`}>
                  <table className="min-w-full text-left text-sm">
                    <thead className="border-b border-gray-100 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
                      <tr>
                        {[
                          'Run',
                          'User',
                          'Status',
                          'Agent / model',
                          'Host',
                          'Tool calls',
                          'Created',
                        ].map((label) => (
                          <th key={label} className="whitespace-nowrap px-4 py-3 font-medium">
                            {label}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                      {page.runs.map((run) => (
                        <tr key={run.id} className="align-top">
                          <th scope="row" className="max-w-xs px-4 py-4 font-normal">
                            <Link
                              href={`/admin/runs/${encodeURIComponent(run.id)}`}
                              className="break-words font-medium text-crimson hover:underline dark:text-crimson-light"
                            >
                              {run.title || run.id}
                            </Link>
                            <p className="mt-1 break-all text-xs text-gray-500 dark:text-gray-400">
                              {run.repo}
                            </p>
                            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                              Turn {run.turn_no}
                            </p>
                          </th>
                          <td className="max-w-40 break-all px-4 py-4 text-xs">{run.user_id}</td>
                          <td className="px-4 py-4">
                            <RunState state={run.state} />
                          </td>
                          <td className="max-w-48 break-words px-4 py-4">
                            {run.runtime}
                            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                              {run.model}
                            </p>
                          </td>
                          <td className="max-w-40 break-all px-4 py-4 text-xs">
                            {run.hosts.join(', ') || 'Unassigned'}
                          </td>
                          <td className="px-4 py-4 tabular-nums">
                            {run.tool_calls.toLocaleString()}
                          </td>
                          <td className="whitespace-nowrap px-4 py-4 text-xs text-gray-500 dark:text-gray-400">
                            {dateTime(run.created_at)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {page.next_cursor && (
                <button
                  type="button"
                  className={`${BUTTON} mt-5`}
                  disabled={moreLoading}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load more runs'}
                </button>
              )}
            </>
          )
        )}
      </section>
    </div>
  );
}
