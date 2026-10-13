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

export type SandboxAction = 'stop' | 'archive' | 'restore' | 'delete' | 'discard';

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
  const deletionPending = ['pending', 'archived'].includes(sandbox?.deletion_state ?? 'none');
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

  return {
    stopBlocked,
    inventoryBlocked,
    deleteBlocked,
    controlsBlocked,
    forceDelete,
    staleInventoryEntry,
    deletionComplete,
    deletionPending,
  };
}

export function sandboxActionBlocked(
  kind: SandboxAction,
  state: ReturnType<typeof sandboxActionState>,
): string | null {
  return kind === 'stop'
    ? state.stopBlocked
    : kind === 'delete' || kind === 'discard'
      ? state.deleteBlocked
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
    return deleteAdminSandbox(
      sandbox.id,
      { ...reviewed, container_id: sandbox.container_id! },
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
