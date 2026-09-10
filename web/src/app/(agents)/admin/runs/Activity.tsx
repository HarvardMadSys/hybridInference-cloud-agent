'use client';

import { useEffect, useRef, useState } from 'react';

import {
  getAdminRunActivity,
  revealAdminEventContent,
  type AdminActivityEvent,
  type AdminActivityPage,
  type AdminEventContent,
} from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice } from './shared';

export function Activity({
  jobId,
  attemptId,
  onAccessDenied,
}: {
  jobId: string;
  attemptId?: number;
  onAccessDenied: () => void;
}) {
  const [page, setPage] = useState<AdminActivityPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    moreRequest.current?.abort();
    setPage(null);
    setLoading(true);
    setMoreLoading(false);
    setError(null);
    getAdminRunActivity(jobId, { attempt_id: attemptId, limit: 100 }, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setPage(next);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          if (accessDenied(e)) onAccessDenied();
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
  }, [jobId, attemptId, revision, onAccessDenied]);

  async function more() {
    if (!page?.next_cursor || moreLoading) return;
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminRunActivity(
        jobId,
        { attempt_id: attemptId, limit: 100, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) =>
          previous ? { ...next, events: [...previous.events, ...next.events] } : next,
        );
    } catch (e) {
      if (!controller.signal.aborted) {
        if (accessDenied(e)) onAccessDenied();
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  return (
    <section aria-labelledby="activity-heading" className="mt-8">
      <div className="flex items-center justify-between gap-4">
        <h2 id="activity-heading" className="text-lg font-semibold text-gray-900 dark:text-gray-50">
          Runtime activity
        </h2>
        <button
          type="button"
          className={BUTTON}
          disabled={loading}
          onClick={() => setRevision((value) => value + 1)}
        >
          Refresh activity
        </button>
      </div>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Ordered within each execution. Times show when events reached the control plane; they do not
        measure model latency. Content access is audited.
      </p>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {loading ? (
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          Loading activity…
        </p>
      ) : (
        page && (
          <div
            className={`${CARD} mt-4 max-h-[36rem] overflow-y-auto divide-y divide-gray-100 dark:divide-white/5`}
          >
            {page.events.length ? (
              page.events.map((event) => (
                <ActivityRow
                  key={`${jobId}:${event.id}`}
                  jobId={jobId}
                  event={event}
                  onAccessDenied={onAccessDenied}
                />
              ))
            ) : (
              <p className="p-5 text-sm text-gray-500 dark:text-gray-400">No activity recorded.</p>
            )}
          </div>
        )
      )}
      {page?.next_cursor && (
        <button
          type="button"
          className={`${BUTTON} mt-4`}
          disabled={moreLoading}
          onClick={() => void more()}
        >
          {moreLoading ? 'Loading…' : 'Load more activity'}
        </button>
      )}
    </section>
  );
}

function ActivityRow({
  jobId,
  event,
  onAccessDenied,
}: {
  jobId: string;
  event: AdminActivityEvent;
  onAccessDenied: () => void;
}) {
  const [content, setContent] = useState<AdminEventContent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  useEffect(() => () => request.current?.abort(), []);

  async function reveal() {
    if (loading) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const next = await revealAdminEventContent(jobId, event.id, controller.signal);
      if (!controller.signal.aborted) setContent(next);
    } catch (e) {
      if (!controller.signal.aborted) {
        if (accessDenied(e)) onAccessDenied();
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  return (
    <article className="px-5 py-4" aria-label={`Execution ${event.attempt_no} event ${event.seq}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium text-gray-800 dark:text-gray-200">
              {event.event_type.replaceAll('_', ' ')}
            </span>
            {event.tool_name && (
              <span className="break-all text-gray-600 dark:text-gray-400">{event.tool_name}</span>
            )}
            {event.is_error && (
              <span className="rounded-full bg-red-50 px-2 py-0.5 text-xs text-red-700 dark:bg-red-500/10 dark:text-red-400">
                Error
              </span>
            )}
          </div>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Execution {event.attempt_no} · event {event.seq} · received {dateTime(event.created_at)}
          </p>
          {event.tool_use_id && (
            <p className="mt-1 break-all font-mono text-[11px] text-gray-400 dark:text-gray-500">
              Tool ID: {event.tool_use_id}
            </p>
          )}
        </div>
        {event.has_content &&
          (content ? (
            <button type="button" className={BUTTON} onClick={() => setContent(null)}>
              Hide content
            </button>
          ) : (
            <button
              type="button"
              className={BUTTON}
              disabled={loading}
              onClick={() => void reveal()}
            >
              {loading ? 'Loading content…' : 'Reveal content'}
            </button>
          ))}
      </div>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {content && (
        <div className="mt-3">
          {content.truncated && (
            <p className="mb-2 text-xs text-amber-700 dark:text-amber-400">
              Content was truncated to the event size limit.
            </p>
          )}
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words rounded-lg bg-gray-50 p-4 text-xs text-gray-700 dark:bg-black/20 dark:text-gray-300">
            {JSON.stringify(content.payload, null, 2)}
          </pre>
        </div>
      )}
    </article>
  );
}
