'use client';

import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { Suspense, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getAdminConversation,
  conversationQuery,
  type AdminConversationDetail,
} from '@/lib/api/adminConversations';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../../AdminPageHeader';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  RunState,
} from '../../runs/shared';
import { conversationLink } from '../filters';

export default function AdminConversationPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense>
      <LinkedConversation key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
}

function LinkedConversation() {
  const { threadId } = useParams<{ threadId: string }>();
  const query = useSearchParams().toString();
  return <ConversationDetails key={`${threadId}:${query}`} threadId={threadId} query={query} />;
}

function ConversationDetails({ threadId, query }: { threadId: string; query: string }) {
  const linked = conversationLink(query, false);
  const [page, setPage] = useState<AdminConversationDetail | null>(null);
  const [error, setError] = useState<string | null>(linked.error);
  const [loading, setLoading] = useState(!linked.error);
  const [moreLoading, setMoreLoading] = useState(false);
  const [denied, setDenied] = useState(false);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const selection = conversationLink(query, false);
    if (selection.error) return;
    const controller = new AbortController();
    moreRequest.current?.abort();
    setMoreLoading(false);
    setPage(null);
    setLoading(true);
    setError(null);
    getAdminConversation(threadId, selection.filters ?? {}, controller.signal)
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
  }, [threadId, query, revision]);

  async function loadMore() {
    if (!page?.next_cursor || moreLoading) return;
    moreRequest.current?.abort();
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminConversation(
        threadId,
        { ...linked.filters, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) =>
          previous ? { ...next, turns: [...previous.turns, ...next.turns] } : next,
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
  const context = conversationQuery(linked.filters ?? {});
  const conversation = page?.conversation;
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Session details"
          description="Inspect session activity, agent turns, and resource usage."
        />
        <AdminNavigation current="activity" />
        <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
          <Link
            href={`/admin/activity${context}`}
            className="ui-focus-ring inline-flex min-h-11 items-center rounded-lg text-sm text-accent hover:underline"
          >
            ← All sessions
          </Link>
          <button
            type="button"
            className={BUTTON}
            disabled={loading || Boolean(linked.error)}
            onClick={() => setRevision((value) => value + 1)}
          >
            Refresh session
          </button>
        </div>
        {error && <ErrorNotice>{error}</ErrorNotice>}
        {loading && (
          <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Loading session…
          </p>
        )}
        {conversation && page && (
          <>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <h2 className="break-words text-xl font-semibold text-gray-900 dark:text-gray-50">
                {conversation.title ?? conversation.thread_id}
              </h2>
              <RunState state={conversation.state} />
              {(conversation.deleted_at || conversation.archived_at) && (
                <span className="text-xs text-gray-500 dark:text-gray-400">
                  {conversation.deleted_at ? 'Deleted by user' : 'Archived'}
                </span>
              )}
            </div>
            <p className="mt-2 break-all font-mono text-xs text-gray-500 dark:text-gray-400">
              {conversation.thread_id}
            </p>
            <dl className={`${CARD} mt-5 grid grid-cols-2 gap-5 p-5 text-sm lg:grid-cols-4`}>
              {[
                ['User', conversation.user_id],
                ['Repository', conversation.repo ?? 'Start from scratch'],
                ['Created', dateTime(conversation.created_at)],
                ['Last activity', dateTime(conversation.last_activity_at)],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 break-words text-gray-800 dark:text-gray-200">{value}</dd>
                </div>
              ))}
            </dl>
            <h3 className="mt-8 text-lg font-semibold text-gray-900 dark:text-gray-50">
              Turns ({conversation.turn_count})
            </h3>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Newest first. Open a turn to inspect its usage, resources and model requests.
            </p>
            {linked.linked && (
              <div className="mt-3 text-sm text-gray-600 dark:text-gray-300">
                <p>Showing the full session. Matching turns are marked for the selected filters.</p>
                <p className="mt-1 break-words text-xs text-gray-500 dark:text-gray-400">
                  {[
                    linked.filters?.since &&
                      `Turns created ${dateTime(linked.filters.since)} – ${dateTime(linked.filters.until)}`,
                    ...Object.entries(linked.filters ?? {})
                      .filter(([key]) => key !== 'since' && key !== 'until')
                      .map(([key, value]) => `${key}: ${value}`),
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </p>
              </div>
            )}
            <div className={`${CARD} mt-4 overflow-x-auto`}>
              <table className="w-full min-w-[720px] text-left text-sm">
                <thead className="border-b border-gray-100 bg-gray-50/70 text-xs uppercase text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                  <tr>
                    {['Turn', 'Result', 'Agent / model', 'Tool calls', 'Created'].map((label) => (
                      <th key={label} className="px-5 py-3 font-medium">
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                  {page.turns.map((turn) => (
                    <tr
                      key={turn.id}
                      className="align-top hover:bg-gray-50/70 dark:hover:bg-white/[0.03]"
                    >
                      <th scope="row" className="px-5 py-4 font-normal">
                        <Link
                          href={`/admin/runs/${encodeURIComponent(turn.id)}${context}`}
                          className="font-medium text-crimson hover:underline dark:text-crimson-light"
                        >
                          Turn {turn.turn_no}
                        </Link>
                        <span className="mt-1 block font-mono text-xs text-gray-500 dark:text-gray-400">
                          {turn.id}
                        </span>
                        {linked.linked && (
                          <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                            {turn.matches_filters ? 'Matches filters' : 'Outside filters'}
                          </span>
                        )}
                        {turn.fork_source_job_id && (
                          <Link
                            href={`/admin/runs/${encodeURIComponent(turn.fork_source_job_id)}`}
                            className="mt-1 block text-xs text-crimson hover:underline dark:text-crimson-light"
                          >
                            Inherited · view original execution
                          </Link>
                        )}
                      </th>
                      <td className="px-5 py-4">
                        <RunState state={turn.state} />
                      </td>
                      <td className="px-5 py-4 text-gray-700 dark:text-gray-200">
                        {turn.runtime}
                        <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                          {turn.model}
                        </span>
                      </td>
                      <td className="px-5 py-4 tabular-nums text-gray-700 dark:text-gray-200">
                        {turn.tool_calls}
                      </td>
                      <td className="whitespace-nowrap px-5 py-4 text-xs text-gray-500 dark:text-gray-400">
                        {dateTime(turn.created_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {page.next_cursor && (
              <div className="mt-5 flex justify-center">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={moreLoading}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load older turns'}
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
