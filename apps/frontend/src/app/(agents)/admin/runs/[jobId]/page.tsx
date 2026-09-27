'use client';

import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { getAdminRun, type AdminAttempt, type AdminRunDetail } from '@/lib/api/adminRuns';
import { conversationHref } from '@/lib/api/adminConversations';
import { conversationLink } from '../../conversations/filters';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import { LLMRequests } from '../LLMRequests';
import { Resources } from '../Resources';
import { SessionStatistics } from '../SessionStatistics';
import { StartupMeasurement } from '../StartupMeasurement';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  RunState,
} from '../shared';
import { UsageSnapshot } from '../UsageSnapshot';

const TERMINAL = new Set(['succeeded', 'failed', 'cancelled']);

/** How an earlier run of the turn ended, in the words an admin reads it in. */
function earlierRun(attempt: AdminAttempt): string {
  const host = attempt.host || 'an unknown server';
  const at = dateTime(attempt.finished_at ?? attempt.started_at);
  if (attempt.status === 'superseded') return `the run on ${host} stopped responding (${at})`;
  if (attempt.status === 'aborted') return `the run on ${host} could not start (${at})`;
  return `the run on ${host} ended (${at})`;
}

/** Which sections describe the latest run and which the whole turn. */
const RESTART_SCOPE =
  'Resources and startup show the latest run; turn statistics, token usage and LLM requests cover every run.';

/**
 * Said only when the turn ran more than once.
 *
 * A turn normally runs once. A second run exists because the platform restarted
 * the turn after the first one stopped reporting or could not start, which is
 * the one thing about runs an admin needs to know.
 */
function restartNote(attempts: AdminAttempt[]): string | null {
  // Newest first, so every run after the first entry is an earlier one.
  const earlier = attempts.slice(1).reverse();
  if (!earlier.length) return null;
  const times = earlier.length === 1 ? 'once' : `${earlier.length} times`;
  return `Restarted ${times}: ${earlier.map(earlierRun).join('; ')}.`;
}

export default function AdminRunPage() {
  const params = useParams<{ jobId: string }>();
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense>
      <RunDetails key={`${state.user?.id}:${params?.jobId}`} jobId={params?.jobId ?? ''} />
    </Suspense>
  );
}

function RunDetails({ jobId }: { jobId: string }) {
  const linked = conversationLink(useSearchParams().toString(), false);
  const [detail, setDetail] = useState<AdminRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [denied, setDenied] = useState(false);
  const onAccessDenied = useCallback(() => setDenied(true), []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    getAdminRun(jobId, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setDetail(next);
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
    return () => controller.abort();
  }, [jobId, revision]);

  const attempts = [...(detail?.attempts ?? [])].sort(
    (a, b) => b.attempt_no - a.attempt_no || b.attempt_id - a.attempt_id,
  );
  // The latest run is the one that produced, or is producing, the turn's result.
  const latest = attempts[0];
  const first = attempts[attempts.length - 1];
  const restarted = restartNote(attempts);
  if (denied) return <AdminRequired />;
  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-6xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="runs" />
        <div className="mt-6 flex items-center justify-between gap-4">
          <Link
            href={
              detail?.run.thread_id
                ? conversationHref(detail.run.thread_id, linked.filters ?? {})
                : '/admin/sessions'
            }
            className="text-sm text-crimson hover:underline dark:text-crimson-light"
          >
            {detail?.run.thread_id ? '← Back to session' : '← All sessions'}
          </Link>
          <button
            className={BUTTON}
            disabled={loading}
            type="button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Refresh turn
          </button>
        </div>
        {error && <ErrorNotice>{error}</ErrorNotice>}
        {loading && !detail && (
          <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Loading turn…
          </p>
        )}
        {detail && (
          <>
            <div className="mt-6 flex flex-wrap items-center gap-3">
              <h2 className="break-words text-xl font-semibold text-gray-900 dark:text-gray-50">
                {detail.run.title || detail.run.id}
              </h2>
              <RunState state={detail.run.state} />
            </div>
            {detail.run.state === 'queued' && (
              <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">Waiting to start</p>
            )}
            <p className="mt-2 break-all font-mono text-xs text-gray-400 dark:text-gray-500">
              {detail.run.id}
            </p>
            <dl className={`${CARD} mt-5 grid grid-cols-2 gap-5 p-5 text-sm lg:grid-cols-3`}>
              {[
                ['User ID', detail.run.user_id],
                ['Repository', detail.run.repo],
                ['Agent', detail.run.runtime],
                ['Model', detail.run.model],
                ['Effort', detail.run.reasoning_effort || 'Auto'],
                ['Sandbox profile', detail.run.execution_profile],
                ['Created', dateTime(detail.run.created_at)],
                ...(first ? [['Started', dateTime(first.started_at)]] : []),
                ...(latest?.finished_at && TERMINAL.has(detail.run.state)
                  ? [['Finished', dateTime(latest.finished_at)]]
                  : []),
                ...(latest ? [['Server', latest.host || 'Unknown host']] : []),
                ['Turn', String(detail.run.turn_no)],
                ['Session', detail.run.thread_id || 'Unavailable'],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 break-words text-gray-800 dark:text-gray-200">{value}</dd>
                </div>
              ))}
            </dl>
            {restarted && (
              <p className="mt-3 text-sm text-amber-700 dark:text-amber-400" title={RESTART_SCOPE}>
                {restarted}
              </p>
            )}
            {latest && (
              <Resources
                key={`resources:${latest.attempt_id}:${revision}`}
                jobId={jobId}
                attemptId={latest.attempt_id}
                onAccessDenied={onAccessDenied}
              />
            )}
            {latest && (
              // Keyed by the run, so a restart remounts it: one run's startup
              // must never sit under another's.
              <StartupMeasurement
                key={`startup:${latest.attempt_id}:${revision}`}
                jobId={jobId}
                attemptId={latest.attempt_id}
                onAccessDenied={onAccessDenied}
              />
            )}
            <SessionStatistics statistics={detail.session_statistics} />
            <UsageSnapshot usage={detail.usage} />
            {detail.capabilities.llm_requests ? (
              <LLMRequests
                refreshKey={revision}
                jobId={jobId}
                running={latest?.finished_at == null}
                skipAttemptIds={attempts
                  .filter((attempt) => attempt.status === 'aborted')
                  .map((attempt) => attempt.attempt_id)}
                onAccessDenied={onAccessDenied}
              />
            ) : (
              <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
                Per-request LLM records are not served by this build. Gateway usage above is an
                aggregate snapshot.
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
