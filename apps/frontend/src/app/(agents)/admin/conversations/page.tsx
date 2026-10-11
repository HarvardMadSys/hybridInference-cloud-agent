'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentJobApiState } from '@/lib/api/agents';
import type { AdminRunFilters } from '@/lib/api/adminRuns';
import {
  getAdminConversations,
  conversationHref,
  type AdminConversationsPage,
} from '@/lib/api/adminConversations';
import { conversationLink } from './filters';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { TurnResults } from '../runs/AllRuns';
import { StepResults } from '../runs/StepResults';
import { SandboxResults } from '../sandboxes/SandboxResults';
import { presetKey } from '../usage/present';
import {
  accessDenied,
  AdminRequired,
  ago,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  ICON_BUTTON,
  RunState,
  TOOLBAR_SELECT,
} from '../runs/shared';

const STATES: AgentJobApiState[] = ['queued', 'running', 'succeeded', 'failed', 'cancelled'];

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

/** A UTC calendar day as a toolbar can afford it: `Sep 21`. */
function shortDay(value: string | undefined): string {
  const time = Date.parse(value ?? '');
  if (!Number.isFinite(time)) return '—';
  return new Date(time).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

function windowFilters(
  days: number,
  keep: AdminRunFilters = {},
  now: Date = new Date(),
): AdminRunFilters {
  return windowOfLength(days * 86_400_000, keep, now);
}

/**
 * The levels Activity lists, one selection at a time.
 *
 * A session is made of turns, and a turn of steps (its model requests), so
 * those levels share a turn selection. Sandboxes show the current fleet.
 */
const LEVELS = [
  {
    key: 'sandboxes',
    label: 'Sandboxes',
    description:
      'Current sandboxes across users, including idle containers retained between turns.',
  },
  {
    key: 'sessions',
    label: 'Sessions',
    description: 'Sessions with turns created in the selected window, newest matching turn first.',
  },
  {
    key: 'turns',
    label: 'Turns',
    description: 'Turns created in the selected window from every session, newest first.',
  },
  {
    key: 'steps',
    label: 'Steps',
    description:
      'Model requests of the turns created in the selected window, newest first, read a few turns at a time.',
  },
] as const;

type Level = (typeof LEVELS)[number]['key'];

export default function AdminConversationsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<ConversationsSkeleton />}>
      <LinkedConversationsList key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
}

/**
 * Read the linked filters once, keyed by the query so a new link remounts.
 *
 * `level=sandboxes`, `level=turns` or `level=steps` opens that level. It chooses
 * what is listed rather than which turns match, so it is taken off before the
 * filters are read.
 */
function LinkedConversationsList() {
  const params = new URLSearchParams(useSearchParams().toString());
  const level: Level = LEVELS.find((item) => item.key === params.get('level'))?.key ?? 'sessions';
  params.delete('level');
  const query = params.toString();
  return <ConversationsList key={`${level}:${query}`} initialQuery={query} initialLevel={level} />;
}

function ConversationsSkeleton() {
  return (
    <div className="min-w-0 flex-1">
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <div className={`${CARD} mt-8 h-64 animate-pulse`} />
      </section>
    </div>
  );
}

function ConversationsList({
  initialQuery,
  initialLevel,
}: {
  initialQuery: string;
  initialLevel: Level;
}) {
  // A link from the usage page carries an absolute window and the identities
  // of the group it came from. It replaces the seven-day default and is kept
  // for as long as the admin stays on this selection.
  const linked = conversationLink(initialQuery);
  const [filters, setFilters] = useState<AdminRunFilters>(() =>
    linked.filters ? { ...linked.filters, limit: 50 } : windowFilters(7),
  );
  // The window the admin is actually looking at, so the control names it
  // instead of claiming the last preset that was clicked. A one-day link is
  // Custom, and changing Status must not re-anchor it to seven days.
  const windowDays = presetKey(filters);
  // A linked window is absolute and stays exactly as linked; a window chosen
  // here re-anchors on Refresh so the list picks up work finished meanwhile.
  const [linkedWindow, setLinkedWindow] = useState(linked.linked);
  // Sessions and turns are two readings of one selection: switching level
  // keeps every filter, the window included.
  const [level, setLevel] = useState<Level>(initialLevel);
  const [loading, setLoading] = useState(!linked.error);
  const [denied, setDenied] = useState(false);
  const onDenied = useCallback(() => setDenied(true), []);
  const active = LEVELS.find((item) => item.key === level) ?? LEVELS[0];
  // The identities a usage link arrived with. No control on this page shows
  // them, so they are stated here.
  const selection = [
    filters.user_id && `User: ${filters.user_id}`,
    filters.repo && `Repository: ${filters.repo}`,
    filters.runtime && `Agent: ${filters.runtime}`,
    filters.model && `Model: ${filters.model}`,
    filters.host && `Host: ${filters.host}`,
  ]
    .filter(Boolean)
    .join(' · ');

  if (denied) return <AdminRequired />;
  return (
    <div className="min-w-0 flex-1">
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="activity" />

        {/* One toolbar: what a row stands for on the left, which turns match on
            the right. The level's description is its tooltip, not a paragraph. */}
        <h2 className="sr-only">{active.label}</h2>
        <div className="mt-8 flex flex-wrap items-center gap-x-3 gap-y-2">
          <div
            role="group"
            aria-label="Level"
            title={active.description}
            className="inline-flex rounded-lg bg-gray-100 p-0.5 dark:bg-white/5"
          >
            {LEVELS.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-pressed={item.key === level}
                onClick={() => setLevel(item.key)}
                className={`rounded-md px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson ${
                  item.key === level
                    ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-gray-50'
                    : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
          {level !== 'sandboxes' && selection && (
            <span
              className="min-w-0 truncate text-xs text-gray-500 dark:text-gray-400"
              title={selection}
            >
              {selection}
            </span>
          )}
          {level !== 'sandboxes' && (
            <div role="group" aria-label="Filters" className="ml-auto flex items-center gap-2">
              {windowDays === 'custom' && (
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {shortDay(filters.since)} – {shortDay(filters.until)}
                </span>
              )}
              <label className="shrink-0">
                <span className="sr-only">Turn created</span>
                <select
                  className={TOOLBAR_SELECT}
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
              <label className="shrink-0">
                <span className="sr-only">Turn result</span>
                <select
                  className={TOOLBAR_SELECT}
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
                  <option value="">All results</option>
                  {STATES.map((value) => (
                    <option key={value} value={value}>
                      {value}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className={ICON_BUTTON}
                type="button"
                aria-label="Refresh"
                title="Refresh"
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
          )}
        </div>

        {linked.error ? (
          // A broken link has nothing to retry: the URL has to change.
          <ErrorNotice>{linked.error}</ErrorNotice>
        ) : level === 'sandboxes' ? (
          <SandboxResults onDenied={onDenied} />
        ) : level === 'sessions' ? (
          <SessionResults filters={filters} onLoading={setLoading} onDenied={onDenied} />
        ) : level === 'turns' ? (
          <TurnResults filters={filters} onLoading={setLoading} onDenied={onDenied} />
        ) : (
          <StepResults filters={filters} onLoading={setLoading} onDenied={onDenied} />
        )}
      </section>
    </div>
  );
}

/** The session level: each session with a matching turn, newest matching turn first. */
function SessionResults({
  filters,
  onLoading,
  onDenied,
}: {
  filters: AdminRunFilters;
  onLoading: (loading: boolean) => void;
  onDenied: () => void;
}) {
  const [page, setPage] = useState<AdminConversationsPage | null>(null);
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
    getAdminConversations(filters, controller.signal)
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
      const next = await getAdminConversations(
        { ...filters, since: page.since, until: page.until, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) =>
          previous
            ? { ...next, conversations: [...previous.conversations, ...next.conversations] }
            : next,
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
        <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
          Loading sessions…
        </p>
      ) : (
        page && (
          <>
            {page.conversations.length === 0 ? (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                No sessions match these filters.
              </p>
            ) : (
              <ConversationsTable page={page} filters={filters} />
            )}
            {page.next_cursor && (
              <div className="mt-5 flex justify-center">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={moreLoading}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load more sessions'}
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

/**
 * One quiet line per session: its title and repository, then who, how it
 * stands, what ran last, how many turns and when. Identifiers and exact times
 * are one hover away rather than a line of their own.
 */
function ConversationsTable({
  page,
  filters,
}: {
  page: AdminConversationsPage;
  filters: AdminRunFilters;
}) {
  const now = Date.now();
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[900px] table-fixed text-left text-sm">
        <colgroup>
          <col />
          <col className="w-36" />
          <col className="w-24" />
          <col className="w-56" />
          <col className="w-16" />
          <col className="w-24" />
        </colgroup>
        <thead className="text-xs text-gray-400 dark:text-gray-500">
          <tr className="border-b border-gray-100 dark:border-white/5">
            {['Session', 'User', 'State', 'Latest agent / model', 'Turns', 'Last activity'].map(
              (label) => (
                <th
                  key={label}
                  className={`px-3 py-2 font-normal ${
                    label === 'Turns' || label === 'Last activity' ? 'text-right' : ''
                  }`}
                >
                  {label}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {page.conversations.map((conversation) => (
            <tr key={conversation.thread_id} className="hover:bg-gray-100 dark:hover:bg-gray-900">
              <th scope="row" className="px-3 py-2.5 font-normal">
                <div className="flex min-w-0 items-baseline gap-2">
                  <Link
                    href={conversationHref(conversation.thread_id, {
                      ...filters,
                      since: page.since,
                      until: page.until,
                    })}
                    className="min-w-0 truncate font-medium text-gray-900 hover:underline dark:text-gray-100"
                    title={conversation.thread_id}
                  >
                    {conversation.title ?? conversation.thread_id}
                  </Link>
                  {/* The repository gives way first, so a narrow table keeps titles. */}
                  <span
                    className="min-w-0 shrink-[4] truncate text-xs text-gray-400 dark:text-gray-500"
                    title={conversation.repo}
                  >
                    {repoName(conversation.repo)}
                  </span>
                  {(conversation.deleted_at || conversation.archived_at) && (
                    <span className="shrink-0 text-xs text-gray-400 dark:text-gray-500">
                      {conversation.deleted_at ? 'Deleted' : 'Archived'}
                    </span>
                  )}
                </div>
              </th>
              <td className="px-3 py-2.5">
                <span
                  className="block truncate font-mono text-xs text-gray-500 dark:text-gray-400"
                  title={conversation.user_id}
                >
                  {conversation.user_id}
                </span>
              </td>
              <td className="px-3 py-2.5">
                <RunState state={conversation.state} quiet />
              </td>
              <td className="px-3 py-2.5">
                <span
                  className="block truncate text-xs text-gray-500 dark:text-gray-400"
                  title={`${conversation.latest_runtime} · ${conversation.latest_model}`}
                >
                  {conversation.latest_runtime} · {conversation.latest_model}
                </span>
              </td>
              <td className="px-3 py-2.5 text-right tabular-nums text-gray-700 dark:text-gray-200">
                {conversation.turn_count}
              </td>
              <td
                className="whitespace-nowrap px-3 py-2.5 text-right text-xs text-gray-500 dark:text-gray-400"
                title={dateTime(conversation.last_activity_at)}
              >
                {ago(conversation.last_activity_at, now)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
