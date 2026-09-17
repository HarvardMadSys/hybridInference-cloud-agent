'use client';

import Link from 'next/link';
import { useParams, useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { getAdminRun, type AdminRunDetail } from '@/lib/api/adminRuns';
import { conversationHref } from '@/lib/api/adminConversations';
import { conversationLink } from '../../conversations/filters';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import { LLMRequests } from '../LLMRequests';
import { Resources } from '../Resources';
import { SessionStatistics } from '../SessionStatistics';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  INPUT,
  RunState,
} from '../shared';
import { UsageSnapshot } from '../UsageSnapshot';

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
  const [historyOpen, setHistoryOpen] = useState(false);
  // A null selection follows the latest attempt, including newly started ones.
  const [historicalAttemptId, setHistoricalAttemptId] = useState<number | null>(null);
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
        setHistoricalAttemptId((current) =>
          next.attempts.some((attempt) => attempt.attempt_id === current) ? current : null,
        );
        if (next.attempts.length <= 1) setHistoryOpen(false);
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
  const latestAttempt = attempts[0];
  const attempt =
    (historyOpen && attempts.find((item) => item.attempt_id === historicalAttemptId)) ||
    latestAttempt;
  const attemptId = attempt?.attempt_id ?? null;
  const viewingHistory = attempt !== latestAttempt;
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
                : '/admin/conversations'
            }
            className="text-sm text-crimson hover:underline dark:text-crimson-light"
          >
            {detail?.run.thread_id ? '← Back to conversation' : '← All conversations'}
          </Link>
          <button
            className={BUTTON}
            disabled={loading}
            type="button"
            onClick={() => setRevision((value) => value + 1)}
          >
            Refresh run
          </button>
        </div>
        {error && <ErrorNotice>{error}</ErrorNotice>}
        {loading && !detail && (
          <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Loading run…
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
                ['Turn', String(detail.run.turn_no)],
                ['Thread', detail.run.thread_id || 'Unavailable'],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 break-words text-gray-800 dark:text-gray-200">{value}</dd>
                </div>
              ))}
            </dl>
            {attempt ? (
              <div className={`${CARD} mt-8 p-5`}>
                <dl className="flex flex-wrap gap-x-8 gap-y-4 text-sm">
                  {[
                    ['Server', attempt.host || 'Unknown host'],
                    ['Status', attempt.status],
                    ['Started', dateTime(attempt.started_at)],
                    ...(attempt.finished_at ? [['Finished', dateTime(attempt.finished_at)]] : []),
                  ].map(([label, value]) => (
                    <div key={label}>
                      <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                      <dd className="mt-1 text-gray-800 dark:text-gray-200">{value}</dd>
                    </div>
                  ))}
                </dl>
                {attempts.length > 1 && (
                  <div className="mt-4 border-t border-gray-100 pt-4 dark:border-white/5">
                    <button
                      type="button"
                      className="text-sm text-crimson hover:underline dark:text-crimson-light"
                      aria-expanded={historyOpen}
                      aria-controls="attempt-history"
                      onClick={() => {
                        setHistoryOpen((open) => !open);
                        setHistoricalAttemptId(null);
                      }}
                    >
                      Previous attempts ({attempts.length - 1})
                    </button>
                    {historyOpen && (
                      <div id="attempt-history" className="mt-3">
                        <label className="text-sm font-medium">
                          View attempt
                          <select
                            className={`${INPUT} mt-2 sm:ml-3 sm:mt-0 sm:inline-block sm:w-auto`}
                            value={viewingHistory ? attempt.attempt_id : ''}
                            onChange={(event) =>
                              setHistoricalAttemptId(
                                event.target.value ? Number(event.target.value) : null,
                              )
                            }
                          >
                            <option value="">Latest attempt</option>
                            {attempts.slice(1).map((item) => (
                              <option key={item.attempt_id} value={item.attempt_id}>
                                Attempt {item.attempt_no} · {item.status} ·{' '}
                                {dateTime(item.started_at)}
                              </option>
                            ))}
                          </select>
                        </label>
                        <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                          {viewingHistory
                            ? 'Viewing a previous attempt. Resources and LLM requests below show this attempt.'
                            : 'Viewing the latest attempt. Choose a previous attempt to inspect its records.'}
                        </p>
                      </div>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                No execution has started.
              </p>
            )}
            {attemptId !== null && (
              <Resources
                key={`resources:${attemptId}:${revision}`}
                jobId={jobId}
                attemptId={attemptId}
                // Whether the numbers cover the whole run is a run-level fact,
                // not an attempt one: a total over three of five executions
                // must not read as the run's total in this panel either.
                attemptCoverage={detail.run.resource_summary ?? null}
                onAccessDenied={onAccessDenied}
              />
            )}
            <SessionStatistics statistics={detail.session_statistics} />
            <UsageSnapshot usage={detail.usage} />
            {detail.capabilities.llm_requests ? (
              <LLMRequests
                key={`requests:${attemptId}`}
                jobId={jobId}
                attemptId={attemptId ?? undefined}
                running={attempt?.finished_at == null}
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
