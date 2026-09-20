'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';

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
import { presetKey } from '../usage/present';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  INPUT,
  RunState,
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

function windowFilters(
  days: number,
  keep: AdminRunFilters = {},
  now: Date = new Date(),
): AdminRunFilters {
  return windowOfLength(days * 86_400_000, keep, now);
}

export default function AdminConversationsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<ConversationsSkeleton />}>
      <LinkedConversationsList key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
}

/** Read the linked filters once, keyed by the query so a new link remounts. */
function LinkedConversationsList() {
  const query = useSearchParams().toString();
  return <ConversationsList key={query} initialQuery={query} />;
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

function ConversationsList({ initialQuery }: { initialQuery: string }) {
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
  const [page, setPage] = useState<AdminConversationsPage | null>(null);
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
    getAdminConversations(filters, controller.signal)
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
        <AdminNavigation current="conversations" />

        <div className="mt-8">
          <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Conversations</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Conversations with turns created in the selected window, newest matching turn first.
            {linked.linked && ' Filters from the linked selection are preserved.'}
          </p>
        </div>

        <div
          role="group"
          aria-label="Conversation filters"
          className="mt-5 flex flex-wrap items-center gap-2"
        >
          <label className="w-40 shrink-0">
            <span className="sr-only">Turn created</span>
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
            <span className="sr-only">Turn result</span>
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
              <option value="">All turn results</option>
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

        <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400">
          <span>
            {[
              filters.user_id && `User: ${filters.user_id}`,
              filters.repo && `Repository: ${filters.repo}`,
              filters.runtime && `Agent: ${filters.runtime}`,
              filters.model && `Model: ${filters.model}`,
              filters.host && `Host: ${filters.host}`,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
          <Link
            href="/admin/runs/all"
            className="text-crimson hover:underline dark:text-crimson-light"
          >
            All execution records
          </Link>
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
          <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
            Loading conversations…
          </p>
        ) : (
          page && (
            <>
              <p className="mt-6 text-xs text-gray-500 dark:text-gray-400">
                Turns created {dateTime(page.since)} – {dateTime(page.until)}
              </p>
              {page.conversations.length === 0 ? (
                <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                  No conversations match these filters.
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
                    {moreLoading ? 'Loading…' : 'Load more conversations'}
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

function ConversationsTable({
  page,
  filters,
}: {
  page: AdminConversationsPage;
  filters: AdminRunFilters;
}) {
  return (
    <div className={`${CARD} mt-4 overflow-x-auto`}>
      <table className="w-full min-w-[900px] text-left text-sm">
        <thead className="border-b border-gray-100 bg-gray-50/70 text-xs uppercase text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
          <tr>
            {[
              'Conversation',
              'User',
              'State',
              'Latest agent / model',
              'Turns',
              'Last activity',
            ].map((label) => (
              <th key={label} className="px-5 py-3 font-medium">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {page.conversations.map((conversation) => (
            <tr
              key={conversation.thread_id}
              className="align-top hover:bg-gray-50/70 dark:hover:bg-white/[0.03]"
            >
              <th scope="row" className="max-w-sm px-5 py-4 font-normal">
                <Link
                  href={conversationHref(conversation.thread_id, {
                    ...filters,
                    since: page.since,
                    until: page.until,
                  })}
                  className="block truncate font-medium text-crimson hover:underline dark:text-crimson-light"
                  title={conversation.title ?? conversation.thread_id}
                >
                  {conversation.title ?? conversation.thread_id}
                </Link>
                <span
                  className="mt-1 block truncate font-mono text-xs text-gray-500 dark:text-gray-400"
                  title={conversation.thread_id}
                >
                  {conversation.thread_id}
                </span>
                <span
                  className="mt-1 block truncate text-xs text-gray-500 dark:text-gray-400"
                  title={conversation.repo}
                >
                  {conversation.repo}
                </span>
                {(conversation.deleted_at || conversation.archived_at) && (
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {conversation.deleted_at ? 'Deleted' : 'Archived'}
                  </span>
                )}
              </th>
              <td className="max-w-48 px-5 py-4">
                <span
                  className="block truncate font-mono text-xs text-gray-600 dark:text-gray-300"
                  title={conversation.user_id}
                >
                  {conversation.user_id}
                </span>
              </td>
              <td className="px-5 py-4">
                <RunState state={conversation.state} />
              </td>
              <td className="max-w-56 px-5 py-4 text-gray-700 dark:text-gray-200">
                <span className="block truncate" title={conversation.latest_runtime}>
                  {conversation.latest_runtime}
                </span>
                <span
                  className="mt-1 block truncate text-xs text-gray-500 dark:text-gray-400"
                  title={conversation.latest_model}
                >
                  {conversation.latest_model}
                </span>
              </td>
              <td className="whitespace-nowrap px-5 py-4 text-gray-700 dark:text-gray-200">
                {conversation.turn_count}
              </td>
              <td className="whitespace-nowrap px-5 py-4 text-xs text-gray-500 dark:text-gray-400">
                {dateTime(conversation.last_activity_at)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
