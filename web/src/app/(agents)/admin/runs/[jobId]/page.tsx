'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { getAdminRun, type AdminRunDetail } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import { Activity } from '../Activity';
import { Resources } from '../Resources';
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
  return <RunDetails key={`${state.user?.id}:${params?.jobId}`} jobId={params?.jobId ?? ''} />;
}

function RunDetails({ jobId }: { jobId: string }) {
  const [detail, setDetail] = useState<AdminRunDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [attemptId, setAttemptId] = useState<number | null>(null);
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
        setAttemptId((current) =>
          next.attempts.some((attempt) => attempt.attempt_id === current)
            ? current
            : (next.attempts[0]?.attempt_id ?? null),
        );
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

  const attempt = detail?.attempts.find((item) => item.attempt_id === attemptId);
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
            href="/admin/runs"
            className="text-sm text-crimson hover:underline dark:text-crimson-light"
          >
            ← All runs
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
            {detail.attempts.length > 0 ? (
              <>
                <div className={`${CARD} mt-8 flex flex-wrap items-center gap-4 p-5`}>
                  <label className="text-sm font-medium">
                    Execution
                    <select
                      className={`${INPUT} ml-3 inline-block w-auto`}
                      value={attemptId ?? ''}
                      onChange={(event) => setAttemptId(Number(event.target.value))}
                    >
                      {detail.attempts.map((item) => (
                        <option key={item.attempt_id} value={item.attempt_id}>
                          {item.attempt_no} · {item.host || 'Unknown host'}
                        </option>
                      ))}
                    </select>
                  </label>
                  {attempt && (
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      <p>
                        {attempt.status} · Started {dateTime(attempt.started_at)}
                      </p>
                      {attempt.finished_at && (
                        <p className="mt-1">Finished {dateTime(attempt.finished_at)}</p>
                      )}
                    </div>
                  )}
                </div>
              </>
            ) : (
              <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                No execution has started.
              </p>
            )}
            <Activity
              key={`activity:${attemptId}:${revision}`}
              jobId={jobId}
              attemptId={attemptId ?? undefined}
              onAccessDenied={onAccessDenied}
            />
            {attemptId !== null && (
              <Resources
                key={`resources:${attemptId}:${revision}`}
                jobId={jobId}
                attemptId={attemptId}
                onAccessDenied={onAccessDenied}
              />
            )}
            <UsageSnapshot usage={detail.usage} />
            {!detail.capabilities.llm_requests && (
              <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
                Per-request LLM records are not stored here. Gateway usage above is an aggregate
                snapshot.
              </p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
