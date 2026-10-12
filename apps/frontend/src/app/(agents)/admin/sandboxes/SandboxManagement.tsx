'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';

import { conversationHref } from '@/lib/api/adminConversations';
import {
  archiveAdminSandbox,
  deleteAdminSandbox,
  getAdminSandbox,
  restoreAdminSandbox,
  stopAdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, dateTime, ErrorNotice } from '../runs/shared';

const REFRESH_MS = 30_000;
const TERMINAL_FRESHNESS_MS = 90_000;
const REVIEW_FRESHNESS_MS = 90_000;
const HOST_FRESHNESS_MS = 90_000;
type SandboxAction = 'stop' | 'archive' | 'restore' | 'delete';

export function SandboxManagement({
  sandboxId,
  nowMs,
  autoRefresh = true,
  onDenied,
  onChanged,
}: {
  sandboxId: string;
  nowMs: number;
  autoRefresh?: boolean;
  onDenied: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<AdminSandboxDetail | null>(null);
  const [observedAtMs, setObservedAtMs] = useState(0);
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<SandboxAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const request = useRef<AbortController | null>(null);
  const action = useRef<AbortController | null>(null);
  const accessRevoked = useRef(false);
  const autoRefreshEnabled = useRef(autoRefresh);

  useEffect(() => {
    autoRefreshEnabled.current = autoRefresh;
  }, [autoRefresh]);

  const clearAccess = useCallback(() => {
    accessRevoked.current = true;
    request.current?.abort();
    action.current?.abort();
    request.current = null;
    action.current = null;
    setDetail(null);
    setError(null);
    setLoading(false);
    setPendingAction(null);
    onDenied();
  }, [onDenied]);

  const load = useCallback(async () => {
    if (accessRevoked.current || request.current || action.current) return;
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
      if (accessDenied(cause)) clearAccess();
      else setError(getErrorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
      if (request.current === controller) request.current = null;
    }
  }, [sandboxId, clearAccess]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      if (autoRefreshEnabled.current) void load();
    }, REFRESH_MS);
    return () => {
      clearInterval(timer);
      request.current?.abort();
      action.current?.abort();
      request.current = null;
      action.current = null;
    };
  }, [load]);

  const sandbox = detail?.sandbox;
  const busy = pendingAction !== null;
  const serverNow = detail
    ? Date.parse(detail.observed_at) + Math.max(0, nowMs - observedAtMs)
    : NaN;
  const terminalObserved = Date.parse(sandbox?.terminal_count_observed_at ?? '');
  const deletionPending = ['pending', 'archived'].includes(sandbox?.deletion_state ?? 'none');
  const deletionComplete = sandbox?.deletion_state === 'deleted';
  const deletionBusyReason = deletionPending
    ? 'Wait for the S3 archive and container deletion to finish.'
    : null;
  const reviewBlocked = !sandbox
    ? 'Load the current sandbox details before changing it.'
    : nowMs - observedAtMs > REVIEW_FRESHNESS_MS
      ? 'Refresh details before changing this sandbox.'
      : sandbox.generation == null
        ? 'The current container identity is unavailable.'
        : null;
  const idleBlocked = sandbox
    ? sandbox.active_attempt_count > 0
      ? 'Wait for all active turns to finish.'
      : (sandbox.active_operation_count ?? 0) > 0
        ? 'Wait for active workspace operations to finish.'
        : ['pending', 'snapshotting', 'importing'].includes(sandbox.snapshot_state ?? '')
          ? 'Wait for the workspace snapshot to finish.'
          : null
    : null;
  const hostHeartbeat = Date.parse(sandbox?.host_last_heartbeat_at ?? '');
  const staleInventoryEntry =
    Boolean(sandbox) &&
    sandbox?.state !== 'stopped' &&
    !deletionComplete &&
    (sandbox?.host_last_heartbeat_at === null ||
      (Number.isFinite(serverNow) &&
        Number.isFinite(hostHeartbeat) &&
        serverNow - hostHeartbeat > HOST_FRESHNESS_MS));
  const forceDelete =
    sandbox?.state === 'running' &&
    reviewBlocked === null &&
    deletionBusyReason === null &&
    !deletionComplete &&
    idleBlocked === null &&
    Boolean(sandbox.container_id);
  const stopBlocked =
    reviewBlocked ??
    deletionBusyReason ??
    (sandbox?.state !== 'running' ? 'Only a running idle sandbox can be stopped.' : null) ??
    idleBlocked ??
    detail?.terminal_activity_error ??
    (sandbox?.terminal_count == null || !Number.isFinite(terminalObserved)
      ? 'Terminal activity is unavailable. Refresh details to check the host.'
      : !Number.isFinite(serverNow) || serverNow - terminalObserved > TERMINAL_FRESHNESS_MS
        ? 'Terminal activity is stale. Refresh details to check the host.'
        : sandbox.terminal_count > 0
          ? 'Close all open terminals before stopping the sandbox.'
          : !sandbox.container_id
            ? 'The current container identity is unavailable.'
            : null);
  const removalBlocked =
    sandbox?.state !== 'stopped' && !deletionComplete && !staleInventoryEntry
      ? 'Wait for the host to confirm a clean stop before removing this sandbox.'
      : idleBlocked;
  const inventoryBlocked =
    reviewBlocked ?? deletionBusyReason ?? (sandbox?.archived_at ? null : removalBlocked);
  const deleteBlocked =
    reviewBlocked ??
    deletionBusyReason ??
    (deletionComplete ? 'This sandbox has already been deleted.' : null) ??
    (sandbox?.state !== 'stopped' && sandbox?.state !== 'running'
      ? 'Wait for the host to confirm a clean stop before deleting this sandbox.'
      : null) ??
    idleBlocked ??
    (!sandbox?.container_id ? 'The retained container identity is unavailable.' : null);
  const controlsBlocked = reviewBlocked ?? deletionBusyReason;

  async function applyAction(kind: SandboxAction) {
    const blocked =
      kind === 'stop' ? stopBlocked : kind === 'delete' ? deleteBlocked : inventoryBlocked;
    if (!sandbox || blocked || request.current || action.current || accessRevoked.current) return;
    if (
      kind === 'stop' &&
      !window.confirm(
        `Stop the idle sandbox shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'}? ` +
          'This stops the container for every member and retains its workspace files. ' +
          'New work is blocked until the host confirms the stop.',
      )
    )
      return;
    if (kind === 'delete') {
      const confirmation =
        sandbox.state === 'running'
          ? `Verify the exact retained container shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'} is stopped and idle, then permanently delete it? ` +
            'The server refuses if that exact container is still running or has activity; this action does not stop a running container. ' +
            'Workspace files and native session history are still uploaded to S3 and verified before deletion. Live native resume will no longer be available. Chat history remains.'
          : `Archive to S3 and permanently delete the stopped sandbox shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'}? ` +
            'Workspace files and native session history will be uploaded to S3 and verified before the retained container is deleted for every member. ' +
            'Live native resume will no longer be available. Chat history remains.';
      if (!window.confirm(confirmation)) return;
    }
    if (
      kind === 'archive' &&
      !window.confirm(
        deletionComplete
          ? 'Remove this deleted sandbox from the admin inventory? Its S3 archive and chat history remain available. ' +
              'You can restore its inventory entry using Include removed.'
          : staleInventoryEntry
            ? `Remove the stale sandbox entry shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'} from the admin inventory? ` +
              'This removes only the inventory entry. It does not delete any retained container or files and creates no S3 archive. ' +
              'Chat history and recorded native state are retained. ' +
              'If the host reports the sandbox running or new work resumes, the entry returns to the inventory. You can also restore it using Include removed.'
            : `Remove the stopped sandbox shared by ${sandbox.member_count} ${sandbox.member_count === 1 ? 'session' : 'sessions'} from the admin inventory? ` +
              'Workspace files, native sessions, and chat history are retained. ' +
              'Resuming a session returns the sandbox to the inventory. You can also restore it using Include removed.',
      )
    )
      return;
    if (performance.now() - observedAtMs > REVIEW_FRESHNESS_MS) {
      setError('Refresh details before changing this sandbox.');
      void load();
      return;
    }
    const controller = new AbortController();
    action.current = controller;
    setLoading(false);
    setPendingAction(kind);
    setError(null);
    try {
      const reviewed = {
        generation: sandbox.generation!,
        container_id: sandbox.container_id,
        last_activity_at: sandbox.last_activity_at,
      };
      let next: AdminSandboxDetail;
      if (kind === 'stop') {
        next = await stopAdminSandbox(
          sandbox.id,
          { ...reviewed, container_id: sandbox.container_id! },
          controller.signal,
        );
      } else if (kind === 'delete') {
        const deleteRequest = { ...reviewed, container_id: sandbox.container_id! };
        next =
          sandbox.state === 'running'
            ? await deleteAdminSandbox(sandbox.id, deleteRequest, controller.signal, true)
            : await deleteAdminSandbox(sandbox.id, deleteRequest, controller.signal);
      } else if (kind === 'archive') {
        next = staleInventoryEntry
          ? await archiveAdminSandbox(sandbox.id, reviewed, controller.signal, true)
          : await archiveAdminSandbox(sandbox.id, reviewed, controller.signal);
      } else {
        next = await restoreAdminSandbox(sandbox.id, reviewed, controller.signal);
      }
      if (controller.signal.aborted) return;
      setDetail(next);
      setObservedAtMs(performance.now());
      onChanged();
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (accessDenied(cause)) clearAccess();
      else {
        setError(getErrorMessage(cause));
        // The server may have refused newer activity than the reviewed snapshot.
        // Refresh that snapshot before another attempt.
        action.current = null;
        void load();
      }
    } finally {
      if (!controller.signal.aborted) setPendingAction(null);
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
          disabled={loading || busy || accessRevoked.current}
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
          {sandbox.archived_at && (
            <p role="status" className="mt-2 text-sm text-gray-700 dark:text-gray-200">
              Removed from the admin inventory.{' '}
              {deletionComplete
                ? 'Its S3 archive and chat history remain available.'
                : sandbox.state !== 'stopped'
                  ? 'Any retained container and workspace files are unchanged. Chat history and recorded native state remain.'
                  : 'Workspace files, native sessions, and chat history are retained.'}
            </p>
          )}
          {sandbox.deletion_state === 'pending' && (
            <p role="status" className="mt-2 text-sm text-amber-700 dark:text-amber-400">
              Archiving to S3. Waiting for the archive to be uploaded and verified before deletion.
            </p>
          )}
          {sandbox.deletion_state === 'archived' && (
            <p role="status" className="mt-2 text-sm text-amber-700 dark:text-amber-400">
              Archive verified in S3. Waiting for the host to delete the retained container.
            </p>
          )}
          {deletionComplete && (
            <p role="status" className="mt-2 text-sm text-gray-700 dark:text-gray-200">
              Sandbox deleted. Its files and native session history are archived in S3; live native
              resume is no longer available.
              {sandbox.deleted_at && <> Deleted {dateTime(sandbox.deleted_at)}.</>}
            </p>
          )}
          {sandbox.deletion_state === 'failed' && (
            <ErrorNotice>
              Deletion failed:{' '}
              {sandbox.deletion_error ?? 'The host could not complete the archive and deletion.'}
            </ErrorNotice>
          )}
          {sandbox.deletion_state !== 'failed' && sandbox.deletion_error && (
            <ErrorNotice>Deletion is waiting to retry: {sandbox.deletion_error}</ErrorNotice>
          )}
          {sandbox.deletion_archive_key && (
            <div className="mt-2 break-all text-xs text-gray-500 dark:text-gray-400">
              <p>
                S3 archive: <code>{sandbox.deletion_archive_key}</code>
              </p>
              {sandbox.deletion_sha256 && (
                <p>
                  SHA-256: <code>{sandbox.deletion_sha256}</code>
                </p>
              )}
              {sandbox.deletion_size_bytes != null && (
                <p>Archive size: {sandbox.deletion_size_bytes.toLocaleString()} bytes</p>
              )}
            </div>
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
          {controlsBlocked && (
            <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">{controlsBlocked}</p>
          )}
          <div className="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={BUTTON}
              disabled={busy || loading || stopBlocked !== null}
              onClick={() => void applyAction('stop')}
            >
              {pendingAction === 'stop' ? 'Requesting stop…' : 'Stop idle sandbox'}
            </button>
            {!controlsBlocked && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {stopBlocked ??
                  `Affects all ${sandbox.member_count} member sessions. The server checks that the sandbox is still idle.`}
              </p>
            )}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-3">
            <button
              type="button"
              className={BUTTON}
              disabled={busy || loading || inventoryBlocked !== null}
              onClick={() => void applyAction(sandbox.archived_at ? 'restore' : 'archive')}
            >
              {pendingAction === 'archive'
                ? 'Removing…'
                : pendingAction === 'restore'
                  ? 'Restoring…'
                  : sandbox.archived_at
                    ? 'Restore to inventory'
                    : staleInventoryEntry
                      ? 'Remove stale entry'
                      : 'Remove from inventory'}
            </button>
            {!controlsBlocked && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {inventoryBlocked ??
                  (sandbox.archived_at
                    ? 'Makes this sandbox visible in the admin inventory again.'
                    : deletionComplete
                      ? 'Hides this deleted sandbox. Its S3 archive and chat history remain available.'
                      : staleInventoryEntry
                        ? 'Removes only the offline host’s inventory entry. Retained containers and files are not deleted; chat history and recorded native state remain. If the host reports it running or new work resumes, the entry returns.'
                        : 'Hides this stopped sandbox. Workspace files, native sessions, and chat history are retained.')}
              </p>
            )}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-3 border-t border-gray-200 pt-4 dark:border-white/10">
            <button
              type="button"
              className={`${BUTTON} text-red-700 dark:text-red-400`}
              disabled={busy || loading || deleteBlocked !== null}
              onClick={() => void applyAction('delete')}
            >
              {pendingAction === 'delete'
                ? forceDelete
                  ? 'Verifying stopped state…'
                  : 'Requesting deletion…'
                : forceDelete
                  ? 'Verify stopped and force delete'
                  : 'Archive to S3 and delete'}
            </button>
            {!controlsBlocked && (forceDelete || deleteBlocked !== stopBlocked) && (
              <p className="text-xs text-gray-500 dark:text-gray-400">
                {deleteBlocked ??
                  (forceDelete
                    ? 'Asks the server to verify that this exact container is already stopped and idle. It will not stop a running container, and S3 archival is still required before deletion.'
                    : 'Uploads and verifies files and native session history in S3 before deleting the retained container. Live native resume will no longer be available.')}
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}
