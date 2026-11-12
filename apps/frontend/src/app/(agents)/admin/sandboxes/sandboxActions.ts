import {
  archiveAdminSandbox,
  deleteAdminSandbox,
  restoreAdminSandbox,
  stopAdminSandbox,
  type AdminSandbox,
  type AdminSandboxDetail,
} from '@/lib/api/adminSandboxes';

const TERMINAL_FRESHNESS_MS = 90_000;
export const REVIEW_FRESHNESS_MS = 90_000;
const HOST_FRESHNESS_MS = 90_000;
/** A host silent this long is gone: the control plane's `SILENT_HOST_HANDOVER_SECONDS`. */
export const HOST_GONE_MS = 30 * 60_000;

const CHECKPOINT_ONLY = 'Only its checkpoint in S3 holds this sandbox';

export type SandboxAction = 'stop' | 'archive' | 'restore' | 'delete' | 'discard';

/** The server's clock when it observed a sandbox, in milliseconds; NaN before any. */
export function observedAt(observed: string | undefined): number {
  return Date.parse(observed ?? '');
}

function heartbeatAge(sandbox: AdminSandbox, serverNow: number): number {
  return serverNow - Date.parse(sandbox.host_last_heartbeat_at ?? '');
}

/**
 * Whether the host a cold sandbox names is gone, as the server decides before it
 * deletes the sandbox without that host: no longer registered, removed from the
 * pool (`disabled`), or silent for 30 minutes.
 */
export function hostGone(sandbox: AdminSandbox, serverNow: number): boolean {
  return (
    sandbox.host_status === null ||
    sandbox.host_status === 'disabled' ||
    heartbeatAge(sandbox, serverNow) > HOST_GONE_MS
  );
}

/** Why a cold sandbox whose host may still report cannot be deleted yet. */
function coldWaitReason(sandbox: AdminSandbox, serverNow: number): string {
  const removable =
    sandbox.host_last_heartbeat_at === null || heartbeatAge(sandbox, serverNow) > HOST_FRESHNESS_MS;
  const until = removable
    ? 'Until then, remove it from inventory to hide it.'
    : 'Until then, the next message to one of its chats restores it.';
  return `${CHECKPOINT_ONLY}, so there is nothing to archive, and it can be deleted only once its host ${sandbox.host} is removed from the pool or has been silent for 30 minutes. ${until}`;
}

/**
 * Why a sandbox that has not stopped cannot be removed from inventory or deleted
 * yet. A cold one never stops: only its checkpoint holds it, and the host it
 * names is asked nothing.
 */
export function notStoppedReason(
  sandbox: AdminSandbox | undefined,
  action: 'removing' | 'deleting',
  serverNow: number,
): string {
  if (sandbox?.state !== 'cold')
    return `Wait for the host to confirm a clean stop before ${action} this sandbox.`;
  if (action === 'removing')
    return `${CHECKPOINT_ONLY}, and its host ${sandbox.host} still reports: the next message to one of its chats restores it. It can be removed from inventory once that host is offline.`;
  if (!hostGone(sandbox, serverNow)) return coldWaitReason(sandbox, serverNow);
  return `${CHECKPOINT_ONLY}, so there is no container to archive. Use Delete without archive: no host holds it, so it is deleted at once.`;
}

/**
 * Why Delete without archive is blocked. A cold sandbox is deleted at once,
 * without any host, once the host it names is gone; any other sandbox is
 * blocked exactly as Archive and delete is.
 */
export function discardBlockedReason(
  sandbox: AdminSandbox | undefined,
  deleteBlocked: string | null,
  controlsBlocked: string | null,
  idleBlocked: string | null,
  serverNow: number,
): string | null {
  if (sandbox?.state !== 'cold') return deleteBlocked;
  if (controlsBlocked) return controlsBlocked;
  return hostGone(sandbox, serverNow) ? idleBlocked : coldWaitReason(sandbox, serverNow);
}

/** What Delete without archive does to a cold sandbox, and `otherwise` to any other. */
export function discardDescription(sandbox: AdminSandbox | undefined, otherwise: string): string {
  if (sandbox?.state !== 'cold') return otherwise;
  return `${CHECKPOINT_ONLY}, so deleting it asks no host: once the host it names is gone, it is deleted at once. Its chats’ queued messages fail, and the checkpoint stays in S3 under the bucket’s lifecycle rules.`;
}

const DISCARD_DESCRIPTION =
  'Removes the retained container without uploading workspace files or native session history. The host copy cannot be restored.';

/** The hint under Delete without archive, unless the one above it already says it. */
export function discardHint(
  sandbox: AdminSandbox | undefined,
  deleteBlocked: string | null,
  discardBlocked: string | null,
): string | null {
  if (discardBlocked === null) return discardDescription(sandbox, DISCARD_DESCRIPTION);
  return discardBlocked === deleteBlocked ? null : discardBlocked;
}

/** The confirmation for Delete without archive: `otherwise` unless the sandbox is cold. */
export function discardConfirmation(
  sandbox: AdminSandbox,
  sessions: string,
  otherwise: string,
): string {
  if (sandbox.state !== 'cold') return otherwise;
  return `Permanently delete sandbox ${sandbox.id}, shared by ${sessions}? ${CHECKPOINT_ONLY} and its host is gone, so it is deleted at once and no host is asked. Its chats’ queued messages fail, and live native resume will no longer be available. Chat history remains. The checkpoint stays in S3 under the bucket’s lifecycle rules.`;
}

/** Keep individual and batch controls on the same reviewed eligibility rules. */
export function sandboxActionState(
  detail: AdminSandboxDetail | null,
  observedAtMs: number,
  nowMs: number,
) {
  const sandbox = detail?.sandbox;
  const serverNow = detail
    ? Date.parse(detail.observed_at) + Math.max(0, nowMs - observedAtMs)
    : NaN;
  const terminalObserved = Date.parse(sandbox?.terminal_count_observed_at ?? '');
  const deletionRetryExhausted = Boolean(sandbox?.deletion_retry_exhausted_at);
  const deletionPending =
    ['pending', 'archived'].includes(sandbox?.deletion_state ?? 'none') && !deletionRetryExhausted;
  const deletionComplete = sandbox?.deletion_state === 'deleted';
  const deletionBusyReason = deletionPending
    ? sandbox?.deletion_discard
      ? 'Wait for container deletion to finish. No S3 archive is being uploaded.'
      : 'Wait for the S3 archive and container deletion to finish.'
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
      ? notStoppedReason(sandbox, 'removing', serverNow)
      : idleBlocked;
  const inventoryBlocked =
    reviewBlocked ?? deletionBusyReason ?? (sandbox?.archived_at ? null : removalBlocked);
  const deleteBlocked =
    reviewBlocked ??
    deletionBusyReason ??
    (deletionComplete ? 'This sandbox has already been deleted.' : null) ??
    (sandbox?.state !== 'stopped' && sandbox?.state !== 'running'
      ? notStoppedReason(sandbox, 'deleting', serverNow)
      : null) ??
    idleBlocked ??
    (!sandbox?.container_id ? 'The retained container identity is unavailable.' : null);
  const controlsBlocked = reviewBlocked ?? deletionBusyReason;
  const discardBlocked = discardBlockedReason(
    sandbox,
    deleteBlocked,
    controlsBlocked,
    idleBlocked,
    serverNow,
  );

  return {
    stopBlocked,
    inventoryBlocked,
    deleteBlocked,
    discardBlocked,
    controlsBlocked,
    forceDelete,
    staleInventoryEntry,
    deletionComplete,
    deletionPending,
    deletionRetryExhausted,
  };
}

export function sandboxActionBlocked(
  kind: SandboxAction,
  state: ReturnType<typeof sandboxActionState>,
): string | null {
  return kind === 'stop'
    ? state.stopBlocked
    : kind === 'delete'
      ? state.deleteBlocked
      : kind === 'discard'
        ? state.discardBlocked
        : state.inventoryBlocked;
}

/** Submit only the exact identity and activity that the administrator reviewed. */
export function applySandboxAction(
  kind: SandboxAction,
  sandbox: AdminSandbox,
  signal?: AbortSignal,
  staleInventoryEntry = false,
): Promise<AdminSandboxDetail> {
  const reviewed = {
    generation: sandbox.generation!,
    container_id: sandbox.container_id,
    last_activity_at: sandbox.last_activity_at,
  };
  if (kind === 'stop') {
    return stopAdminSandbox(
      sandbox.id,
      { ...reviewed, container_id: sandbox.container_id! },
      signal,
    );
  }
  if (kind === 'delete' || kind === 'discard') {
    // A cold sandbox may have no container; every other deletion is blocked without one.
    return deleteAdminSandbox(
      sandbox.id,
      reviewed,
      signal,
      sandbox.state === 'running',
      kind === 'discard',
    );
  }
  if (kind === 'archive') {
    return staleInventoryEntry
      ? archiveAdminSandbox(sandbox.id, reviewed, signal, true)
      : archiveAdminSandbox(sandbox.id, reviewed, signal);
  }
  return restoreAdminSandbox(sandbox.id, reviewed, signal);
}
