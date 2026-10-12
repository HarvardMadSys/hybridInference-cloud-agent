'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { conversationHref } from '@/lib/api/adminConversations';
import {
  deleteAdminSandbox,
  getAdminSandboxTrajectory,
  type AdminSandbox,
  type AdminSandboxMember,
  type AdminSandboxTrajectory,
  type AdminSandboxTrajectoryEvent,
} from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
} from '../../runs/shared';

const PAGE_SIZE = 200;
const DELETE_POLL_MS = 3_000;

function eventTime(event: AdminSandboxTrajectoryEvent): number {
  const parsed = Date.parse(event.created_at);
  return Number.isFinite(parsed) ? parsed : Number.MAX_SAFE_INTEGER;
}

function orderedEvents(events: AdminSandboxTrajectoryEvent[]): AdminSandboxTrajectoryEvent[] {
  return [...events].sort(
    (left, right) => eventTime(left) - eventTime(right) || left.id - right.id,
  );
}

function mergeEvents(
  current: AdminSandboxTrajectoryEvent[],
  incoming: AdminSandboxTrajectoryEvent[],
): AdminSandboxTrajectoryEvent[] {
  const byId = new Map<number, AdminSandboxTrajectoryEvent>();
  for (const event of current) byId.set(event.id, event);
  for (const event of incoming) byId.set(event.id, event);
  return orderedEvents([...byId.values()]);
}

function mergeRefresh(
  current: AdminSandboxTrajectory | null,
  next: AdminSandboxTrajectory,
): AdminSandboxTrajectory {
  if (!current) return { ...next, events: orderedEvents(next.events) };
  return {
    ...next,
    events: mergeEvents(current.events, next.events),
    // A refresh reads the first page. Keep the cursor reached by explicit
    // pagination so polling cannot make already-loaded history disappear.
    next_cursor: current.next_cursor,
  };
}

function eventLabel(value: string): string {
  const label = value.trim().replace(/[._-]+/g, ' ');
  return label ? label[0].toUpperCase() + label.slice(1) : 'Lifecycle event';
}

function stateLabel(value: string): string {
  return value.replaceAll('_', ' ');
}

function detailValue(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function AdminSandboxTrajectoryPage() {
  const { state } = useAuth();
  const params = useParams<{ sandboxId: string }>();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  const sandboxId = params?.sandboxId ?? '';
  return (
    <SandboxTrajectory key={`${state.user?.id ?? 'admin'}:${sandboxId}`} sandboxId={sandboxId} />
  );
}

function SandboxTrajectory({ sandboxId }: { sandboxId: string }) {
  const [trajectory, setTrajectory] = useState<AdminSandboxTrajectory | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);
  const actionRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    moreRequest.current?.abort();
    moreRequest.current = null;
    setLoadingMore(false);
    setTrajectory(null);
    setLoading(true);
    setLoadError(null);
    getAdminSandboxTrajectory(sandboxId, { limit: PAGE_SIZE }, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        setTrajectory({ ...next, events: orderedEvents(next.events) });
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(cause)) {
          setTrajectory(null);
          setDenied(true);
        } else {
          setLoadError(getErrorMessage(cause));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [revision, sandboxId]);

  useEffect(
    () => () => {
      moreRequest.current?.abort();
      actionRequest.current?.abort();
    },
    [],
  );

  const deletionState = trajectory?.sandbox.deletion_state ?? 'none';
  const deletionPending = deletionState === 'pending' || deletionState === 'archived';
  const deletionComplete = deletionState === 'deleted';

  useEffect(() => {
    if (!deletionPending || denied) return;
    let request: AbortController | null = null;

    async function poll() {
      if (request) return;
      const controller = new AbortController();
      request = controller;
      try {
        const next = await getAdminSandboxTrajectory(
          sandboxId,
          { limit: PAGE_SIZE },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setTrajectory((current) => mergeRefresh(current, next));
        setLoadError(null);
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (accessDenied(cause)) {
          setTrajectory(null);
          setDenied(true);
        } else {
          setLoadError(`Deletion status refresh failed. ${getErrorMessage(cause)}`);
        }
      } finally {
        if (request === controller) request = null;
      }
    }

    const timer = setInterval(() => void poll(), DELETE_POLL_MS);
    return () => {
      clearInterval(timer);
      request?.abort();
    };
  }, [deletionPending, denied, sandboxId]);

  async function loadMore() {
    const cursor = trajectory?.next_cursor;
    if (!cursor || moreRequest.current) return;
    const controller = new AbortController();
    moreRequest.current = controller;
    setLoadingMore(true);
    setLoadError(null);
    try {
      const next = await getAdminSandboxTrajectory(
        sandboxId,
        { limit: PAGE_SIZE, cursor },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      if (next.next_cursor === cursor) throw new Error('Unable to load the complete trajectory.');
      setTrajectory((current) => {
        if (!current) return { ...next, events: orderedEvents(next.events) };
        return {
          ...current,
          events: mergeEvents(current.events, next.events),
          next_cursor: next.next_cursor,
        };
      });
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (accessDenied(cause)) {
        setTrajectory(null);
        setDenied(true);
      } else {
        setLoadError(getErrorMessage(cause));
      }
    } finally {
      if (!controller.signal.aborted) setLoadingMore(false);
      if (moreRequest.current === controller) moreRequest.current = null;
    }
  }

  const sandbox = trajectory?.sandbox;
  const memberCount =
    trajectory?.member_history_count ?? trajectory?.members.length ?? sandbox?.member_count ?? 0;
  const idleBlocked = sandbox
    ? sandbox.active_attempt_count > 0
      ? 'Wait for all active turns to finish.'
      : (sandbox.active_operation_count ?? 0) > 0
        ? 'Wait for active workspace operations to finish.'
        : ['pending', 'snapshotting', 'importing'].includes(sandbox.snapshot_state ?? '')
          ? 'Wait for the workspace snapshot to finish.'
          : null
    : null;
  const deleteBlocked = !sandbox
    ? 'Load the current sandbox before deleting it.'
    : deletionPending
      ? 'Wait for the S3 archive and container deletion to finish.'
      : deletionComplete
        ? 'This sandbox has already been deleted.'
        : sandbox.state !== 'stopped'
          ? 'Wait for the host to confirm a clean stop before deleting this sandbox.'
          : (idleBlocked ??
            (sandbox.generation == null || !sandbox.container_id
              ? 'The retained container identity is unavailable.'
              : null));

  async function requestDeletion() {
    if (!sandbox || deleteBlocked || actionRequest.current) return;
    if (
      !window.confirm(
        `Archive to S3 and permanently delete sandbox ${sandbox.id}, shared by ${memberCount} ${
          memberCount === 1 ? 'session' : 'sessions'
        }? Workspace files and native session history will be uploaded to S3 and verified before the retained container is deleted. ` +
          'Live native resume will no longer be available. Chat history and the lifecycle audit remain. Container deletion cannot be undone.',
      )
    )
      return;

    const controller = new AbortController();
    actionRequest.current = controller;
    moreRequest.current?.abort();
    moreRequest.current = null;
    setLoadingMore(false);
    setDeleting(true);
    setActionError(null);
    try {
      const next = await deleteAdminSandbox(
        sandbox.id,
        {
          generation: sandbox.generation!,
          container_id: sandbox.container_id!,
          last_activity_at: sandbox.last_activity_at,
        },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setTrajectory((current) => {
        if (!current) return current;
        return {
          ...current,
          sandbox: next.sandbox,
          observed_at: next.observed_at,
        };
      });
      if (next.sandbox.deletion_state === 'deleted') setRevision((value) => value + 1);
    } catch (cause) {
      if (controller.signal.aborted) return;
      if (accessDenied(cause)) {
        setTrajectory(null);
        setDenied(true);
      } else {
        setActionError(getErrorMessage(cause));
      }
    } finally {
      if (!controller.signal.aborted) setDeleting(false);
      if (actionRequest.current === controller) actionRequest.current = null;
    }
  }

  const events = useMemo(() => orderedEvents(trajectory?.events ?? []), [trajectory?.events]);

  if (denied) return <AdminRequired />;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-6xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="sandboxes" />
        <div className="mt-6 flex flex-wrap items-center justify-between gap-4">
          <Link
            href="/admin/sandboxes"
            className="text-sm text-crimson hover:underline dark:text-crimson-light"
          >
            ← All sandboxes
          </Link>
          <button
            type="button"
            className={BUTTON}
            disabled={loading || deleting}
            onClick={() => {
              setActionError(null);
              setRevision((value) => value + 1);
            }}
          >
            Refresh trajectory
          </button>
        </div>

        {loadError && <ErrorNotice>{loadError}</ErrorNotice>}
        {actionError && <ErrorNotice>{actionError}</ErrorNotice>}
        {loading && !trajectory && (
          <p role="status" className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Loading sandbox trajectory…
          </p>
        )}

        {trajectory && sandbox && (
          <>
            <div className="mt-8 flex flex-wrap items-center gap-3">
              <h2 className="break-words text-xl font-semibold text-gray-900 dark:text-gray-50">
                {sandbox.title ?? sandbox.id}
              </h2>
              <span className="rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-700 dark:bg-white/10 dark:text-gray-200">
                {deletionComplete ? 'deleted' : stateLabel(sandbox.state)}
              </span>
            </div>
            <p className="mt-2 break-all font-mono text-xs text-gray-500 dark:text-gray-400">
              {sandbox.id}
            </p>

            {deletionComplete && (
              <div
                role="status"
                className="mt-5 rounded-xl border border-gray-200 bg-gray-50 px-4 py-3 text-sm text-gray-700 dark:border-white/10 dark:bg-white/5 dark:text-gray-200"
              >
                Sandbox deleted{sandbox.deleted_at ? ` ${dateTime(sandbox.deleted_at)}` : ''}. Its
                workspace files and native session history were archived and verified in S3 before
                the retained container was removed. Live native resume is no longer available.
                <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                  Chat history and lifecycle history remain available for administrative audit.
                </span>
              </div>
            )}

            {deletionState === 'pending' && (
              <div
                role="status"
                className="mt-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
              >
                {sandbox.deletion_requested_at && (
                  <>Deletion requested {dateTime(sandbox.deletion_requested_at)}. </>
                )}
                Archiving to S3. The retained container will be deleted only after the upload is
                verified.
              </div>
            )}

            {deletionState === 'archived' && (
              <div
                role="status"
                className="mt-5 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
              >
                Archive verified in S3. Waiting for the host to delete the retained container.
              </div>
            )}

            {deletionState === 'failed' && (
              <ErrorNotice>
                Deletion failed:{' '}
                {sandbox.deletion_error ??
                  'The archive could not be verified. Confirm again to retry.'}
              </ErrorNotice>
            )}
            {deletionPending && sandbox.deletion_error && (
              <ErrorNotice>Deletion is waiting to retry: {sandbox.deletion_error}</ErrorNotice>
            )}
            {sandbox.deletion_archive_key && <DeletionReceipt sandbox={sandbox} />}

            <dl className={`${CARD} mt-5 grid grid-cols-2 gap-5 p-5 text-sm lg:grid-cols-4`}>
              {[
                ['Owner', sandbox.user_id],
                ['Repository', sandbox.repo],
                ['Host', sandbox.host],
                [
                  'Runtime',
                  sandbox.runtime_version
                    ? `${sandbox.runtime_id} ${sandbox.runtime_version}`
                    : sandbox.runtime_id,
                ],
                ['Container', sandbox.container_name],
                ['Members', String(memberCount)],
                ['Created', dateTime(sandbox.created_at)],
                ['Last activity', dateTime(sandbox.last_activity_at)],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 break-words text-gray-800 dark:text-gray-200">{value}</dd>
                </div>
              ))}
            </dl>

            <section className={`${CARD} mt-6 p-5`} aria-labelledby="sandbox-delete-heading">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="max-w-2xl">
                  <h3
                    id="sandbox-delete-heading"
                    className="text-sm font-semibold text-gray-900 dark:text-gray-100"
                  >
                    Archive and delete sandbox
                  </h3>
                  <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                    Uploads and verifies workspace files and native session history in S3 before
                    deleting the retained container. Live native resume will no longer be available.
                    Only a fully stopped, idle sandbox can be deleted.
                  </p>
                </div>
                <button
                  type="button"
                  disabled={deleting || deleteBlocked !== null}
                  onClick={() => void requestDeletion()}
                  className="rounded-lg border border-red-300 px-3 py-1.5 text-sm font-medium text-red-700 transition-colors hover:bg-red-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-600 disabled:cursor-not-allowed disabled:opacity-40 dark:border-red-500/40 dark:text-red-300 dark:hover:bg-red-500/10"
                >
                  {deleting
                    ? 'Requesting deletion…'
                    : deletionComplete
                      ? 'Sandbox deleted'
                      : deletionState === 'pending'
                        ? 'Archiving to S3…'
                        : deletionState === 'archived'
                          ? 'Deleting archived sandbox…'
                          : deletionState === 'failed'
                            ? 'Retry archive and delete'
                            : 'Archive to S3 and delete'}
                </button>
              </div>
              {deleteBlocked && (
                <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">{deleteBlocked}</p>
              )}
            </section>

            <Members
              members={trajectory.members}
              totalCount={trajectory.member_history_count}
              truncated={trajectory.member_history_truncated}
            />

            <section className="mt-8" aria-labelledby="sandbox-trajectory-heading">
              <div className="flex flex-wrap items-end justify-between gap-3">
                <div>
                  <h3
                    id="sandbox-trajectory-heading"
                    className="text-lg font-semibold text-gray-900 dark:text-gray-50"
                  >
                    Trajectory
                  </h3>
                  <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                    Recorded lifecycle, ordered from earliest to latest.
                  </p>
                </div>
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Observed {dateTime(trajectory.observed_at)}
                </p>
              </div>
              {events.length ? (
                <ol
                  aria-label="Sandbox trajectory"
                  className="mt-5 divide-y divide-gray-100 rounded-2xl border border-gray-200 bg-white dark:divide-white/5 dark:border-white/10 dark:bg-gray-900"
                >
                  {events.map((event) => (
                    <TrajectoryEvent key={event.id} event={event} />
                  ))}
                </ol>
              ) : (
                <p className="mt-5 text-sm text-gray-500 dark:text-gray-400">
                  No lifecycle events are recorded for this sandbox.
                </p>
              )}
              {trajectory.next_cursor && (
                <div className="mt-5 flex justify-center">
                  <button
                    type="button"
                    className={BUTTON}
                    disabled={loadingMore}
                    onClick={() => void loadMore()}
                  >
                    {loadingMore ? 'Loading…' : 'Load more events'}
                  </button>
                </div>
              )}
            </section>
          </>
        )}
      </section>
    </div>
  );
}

function DeletionReceipt({ sandbox }: { sandbox: AdminSandbox }) {
  return (
    <div className={`${CARD} mt-3 break-all p-4 text-xs text-gray-500 dark:text-gray-400`}>
      <p>
        S3 archive: <code>{sandbox.deletion_archive_key}</code>
      </p>
      {sandbox.deletion_sha256 && (
        <p className="mt-1">
          SHA-256: <code>{sandbox.deletion_sha256}</code>
        </p>
      )}
      {sandbox.deletion_size_bytes != null && (
        <p className="mt-1">Archive size: {sandbox.deletion_size_bytes.toLocaleString()} bytes</p>
      )}
    </div>
  );
}

function Members({
  members,
  totalCount,
  truncated,
}: {
  members: AdminSandboxMember[];
  totalCount: number;
  truncated: boolean;
}) {
  return (
    <section className="mt-8" aria-labelledby="sandbox-members-heading">
      <h3
        id="sandbox-members-heading"
        className="text-lg font-semibold text-gray-900 dark:text-gray-50"
      >
        Member sessions ({totalCount})
      </h3>
      {truncated && (
        <p className="mt-2 text-sm text-amber-700 dark:text-amber-300" role="status">
          Showing {members.length} of {totalCount} historical member sessions. The complete count is
          retained even though this view is limited.
        </p>
      )}
      {members.length ? (
        <ul
          aria-label="Sandbox member sessions"
          className="mt-4 divide-y divide-gray-100 rounded-2xl border border-gray-200 bg-white dark:divide-white/5 dark:border-white/10 dark:bg-gray-900"
        >
          {members.map((member) => (
            <li key={member.thread_id} className="px-5 py-3 text-sm">
              <Link
                href={conversationHref(member.thread_id)}
                className="font-medium text-crimson hover:underline dark:text-crimson-light"
              >
                {member.title ?? member.thread_id}
              </Link>
              <span className="ml-2 font-mono text-xs text-gray-500 dark:text-gray-400">
                {member.user_id}
              </span>
              <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                Resident: {member.resident_state ?? 'unknown'} · Context:{' '}
                {member.context_status ?? 'unknown'} · Agent: {member.native_state ?? 'unknown'}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">
          No member sessions are currently attached.
        </p>
      )}
    </section>
  );
}

function TrajectoryEvent({ event }: { event: AdminSandboxTrajectoryEvent }) {
  const details = Object.entries(event.details ?? {});
  return (
    <li className="relative px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h4 className="font-medium text-gray-900 dark:text-gray-100">
          {eventLabel(event.event_type)}
        </h4>
        <time
          dateTime={event.created_at}
          className="text-xs tabular-nums text-gray-500 dark:text-gray-400"
        >
          {dateTime(event.created_at)}
        </time>
      </div>
      {(event.from_state || event.to_state) && (
        <p className="mt-1 text-sm text-gray-700 dark:text-gray-200">
          {event.from_state ? stateLabel(event.from_state) : 'unknown'}
          {' → '}
          {event.to_state ? stateLabel(event.to_state) : 'unknown'}
        </p>
      )}
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Source: {event.source}
        {event.actor_id ? ` · Actor: ${event.actor_id}` : ''}
        {event.generation !== undefined && <> · Generation {event.generation}</>}
        {' · '}Event #{event.id}
      </p>
      {details.length > 0 && (
        <dl className="mt-3 grid gap-x-5 gap-y-2 rounded-lg bg-gray-50 p-3 text-xs sm:grid-cols-2 dark:bg-white/[0.03]">
          {details.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="font-medium text-gray-500 dark:text-gray-400">{eventLabel(label)}</dt>
              <dd className="mt-0.5 whitespace-pre-wrap break-words font-mono text-gray-800 dark:text-gray-200">
                {detailValue(value)}
              </dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}
