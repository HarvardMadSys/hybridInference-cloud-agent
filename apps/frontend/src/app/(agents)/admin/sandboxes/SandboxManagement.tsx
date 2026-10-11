'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';

import { conversationHref } from '@/lib/api/adminConversations';
import {
  getAdminSandbox,
  stopAdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, dateTime, ErrorNotice } from '../runs/shared';

const REFRESH_MS = 30_000;
const TERMINAL_FRESHNESS_MS = 90_000;

export function SandboxManagement({
  sandboxId,
  nowMs,
  onDenied,
  onChanged,
}: {
  sandboxId: string;
  nowMs: number;
  onDenied: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AdminSandboxDetail | null>(null);
  const [observedAtMs, setObservedAtMs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const action = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    if (request.current || action.current) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    try {
      const next = await getAdminSandbox(sandboxId, controller.signal);
      if (controller.signal.aborted) return;
      setDetail(next);
      setObservedAtMs(performance.now());
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (accessDenied(cause)) onDenied();
      else setError(getErrorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
      if (request.current === controller) request.current = null;
    }
  }, [sandboxId, onDenied]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => {
      clearInterval(timer);
      request.current?.abort();
      action.current?.abort();
      request.current = null;
      action.current = null;
    };
  }, [load]);

  const sandbox = detail?.sandbox;
  const serverNow = detail
    ? Date.parse(detail.observed_at) + Math.max(0, nowMs - observedAtMs)
    : NaN;
  const terminalObserved = Date.parse(sandbox?.terminal_count_observed_at ?? '');
  const blocked = !sandbox
    ? 'Load the current sandbox details before stopping it.'
    : sandbox.state !== 'running'
      ? 'Only a running idle sandbox can be stopped.'
      : sandbox.active_attempt_count > 0
        ? 'Wait for all active turns to finish.'
        : (sandbox.active_operation_count ?? 0) > 0
          ? 'Wait for active workspace operations to finish.'
          : ['pending', 'snapshotting', 'importing'].includes(sandbox.snapshot_state ?? '')
            ? 'Wait for the workspace snapshot to finish.'
            : detail?.terminal_activity_error
              ? detail.terminal_activity_error
              : sandbox.terminal_count == null || !Number.isFinite(terminalObserved)
                ? 'Terminal activity is unavailable. Refresh details to check the host.'
                : !Number.isFinite(serverNow) ||
                    serverNow - terminalObserved > TERMINAL_FRESHNESS_MS
                  ? 'Terminal activity is stale. Refresh details to check the host.'
                  : sandbox.terminal_count > 0
                    ? 'Close all open terminals before stopping the sandbox.'
                    : sandbox.generation == null || !sandbox.container_id
                      ? 'The current container identity is unavailable.'
                      : null;

  async function stop() {
    if (!sandbox || blocked || action.current) return;
    if (
      !window.confirm(
        `Stop the idle sandbox shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'}? ` +
          'This stops the container for every member and retains its workspace files. ' +
          'New work is blocked until the host confirms the stop.',
      )
    )
      return;
    const controller = new AbortController();
    action.current = controller;
    // A read begun before this action must not restore an earlier running state.
    request.current?.abort();
    request.current = null;
    setLoading(false);
    setBusy(true);
    setError(null);
    try {
      const next = await stopAdminSandbox(
        sandbox.id,
        {
          generation: sandbox.generation!,
          container_id: sandbox.container_id!,
          last_activity_at: sandbox.last_activity_at,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setDetail(next);
      setObservedAtMs(performance.now());
      onChanged();
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (accessDenied(cause)) onDenied();
      else {
        setError(getErrorMessage(cause));
        // The server may have refused newer activity than the reviewed snapshot.
        // Refresh that snapshot before another attempt.
        action.current = null;
        void load();
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
      if (action.current === controller) action.current = null;
    }
  }

  return (
    <section
      aria-label="Sandbox management"
      className="mt-5 border-t border-gray-200 pt-4 dark:border-white/10"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
          Sessions and controls
        </h3>
        <button
          type="button"
          className={BUTTON}
          disabled={loading || busy}
          onClick={() => {
            setError(null);
            void load();
          }}
        >
          Refresh details
        </button>
      </div>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {loading && !detail && (
        <p role="status" className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          Loading sandbox details…
        </p>
      )}
      {detail && sandbox && (
        <>
          <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
            State:{' '}
            <strong className="font-medium text-gray-900 dark:text-gray-100">
              {sandbox.state.replaceAll('_', ' ')}
            </strong>
            {' · '}Reviewed {dateTime(detail.observed_at)}
          </p>
          {sandbox.state === 'stopping' && (
            <p role="status" className="mt-2 text-sm text-amber-700 dark:text-amber-400">
              {sandbox.stop_requested_at ? 'Stop requested.' : 'Container is stopping.'} Waiting for
              the host to confirm that the container has stopped.
            </p>
          )}
          {(sandbox.state === 'stopped' || sandbox.state === 'stopped_dirty') && (
            <p role="status" className="mt-2 text-sm text-gray-700 dark:text-gray-200">
              The host reports this sandbox as {sandbox.state.replaceAll('_', ' ')}.
            </p>
          )}
          <ul
            aria-label="Sandbox sessions"
            className="mt-3 divide-y divide-gray-200 dark:divide-white/10"
          >
            {detail.members.map((member) => (
              <li key={member.thread_id} className="py-2 text-xs text-gray-500 dark:text-gray-400">
                <Link
                  href={conversationHref(member.thread_id)}
                  className="font-medium text-gray-900 hover:underline dark:text-gray-100"
                >
                  {member.title ?? member.thread_id}
                </Link>
                <span className="ml-2 font-mono">{member.user_id}</span>
                <span className="mt-1 block">
                  Resident: {member.resident_state ?? 'unknown'} · Context:{' '}
                  {member.context_status ?? 'unknown'} · Agent: {member.native_state ?? 'unknown'}
                  {member.active_attempt_id ? ' · Active turn' : ''}
                </span>
              </li>
            ))}
          </ul>
          {detail.members.length === 0 && (
            <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
              No member sessions are recorded.
            </p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={BUTTON}
              disabled={busy || loading || blocked !== null}
              onClick={() => void stop()}
            >
              {busy ? 'Requesting stop…' : 'Stop idle sandbox'}
            </button>
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {blocked ??
                `Affects all ${sandbox.member_count} member sessions. The server checks that the sandbox is still idle.`}
            </p>
          </div>
        </>
      )}
    </section>
  );
}
