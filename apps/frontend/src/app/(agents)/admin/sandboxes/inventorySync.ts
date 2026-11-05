import { getAdminSandboxes, type AdminSandboxesPage } from '@/lib/api/adminSandboxes';

import type { SandboxObservation } from './present';

export interface SandboxInventorySnapshot {
  sandboxes: SandboxObservation[];
  observedAt: string;
  appliedAtMs: number;
  changeCursor?: string;
}

const DELTA_PAGE_BUDGET = 10;

interface Accumulator {
  rows: Map<string, SandboxObservation>;
  observedAt: string;
  appliedAtMs: number;
  snapshotAnchor?: number;
  deltaPages: number;
}

function collectPage(current: Accumulator, page: AdminSandboxesPage) {
  const receivedAt = performance.now();
  if (page.mode !== 'delta' && current.snapshotAnchor === undefined) {
    current.rows = new Map();
    current.snapshotAnchor = receivedAt;
    current.observedAt = page.observed_at;
    current.appliedAtMs = receivedAt;
  }
  applyPage(current.rows, page, page.mode === 'snapshot' ? current.snapshotAnchor! : receivedAt);
  if (page.mode === 'delta') {
    current.observedAt = page.observed_at;
    current.appliedAtMs = receivedAt;
    current.deltaPages++;
  }
}

function accumulator(previous: SandboxInventorySnapshot | null): Accumulator {
  return {
    rows: new Map(previous?.sandboxes.map((row) => [row.sandbox.id, row])),
    observedAt: previous?.observedAt ?? '',
    appliedAtMs: previous?.appliedAtMs ?? 0,
    deltaPages: 0,
  };
}

function snapshotContinuation(page: AdminSandboxesPage) {
  return page.mode === 'snapshot' ? page.change_cursor : null;
}

function applyPage(
  rows: Map<string, SandboxObservation>,
  page: AdminSandboxesPage,
  observedAtMs: number,
) {
  for (const id of page.deleted_ids ?? []) rows.delete(id);
  for (const sandbox of page.sandboxes)
    rows.set(sandbox.id, { sandbox, observedAtMs, serverObservedAt: page.observed_at });
  const hosts = new Map(page.hosts?.map((host) => [host.host, host.last_heartbeat_at]));
  if (!hosts.size) return;
  for (const [id, observation] of rows) {
    const heartbeat = hosts.get(observation.sandbox.host);
    if (heartbeat === undefined || heartbeat === observation.sandbox.host_last_heartbeat_at)
      continue;
    // Host-only changes must not make an old resource sample appear fresh.
    rows.set(id, {
      ...observation,
      sandbox: { ...observation.sandbox, host_last_heartbeat_at: heartbeat },
    });
  }
}

export async function readInventory(
  filters: Parameters<typeof getAdminSandboxes>[0],
  previous: SandboxInventorySnapshot | null,
  signal: AbortSignal,
): Promise<SandboxInventorySnapshot> {
  let cursor = previous?.changeCursor;
  let current = accumulator(previous);
  let resets = 0;
  const seen = new Set<string>();
  for (;;) {
    const page = await getAdminSandboxes(
      { ...filters, sync: true, ...(cursor ? { cursor } : {}) },
      signal,
    );
    signal.throwIfAborted();
    if (page.reset_required) {
      if (resets++) throw new Error('Unable to load the complete sandbox list. Please retry.');
      cursor = undefined;
      current = accumulator(null);
      seen.clear();
      continue;
    }
    collectPage(current, page);
    const next = page.next_cursor ?? undefined;
    if (next && seen.has(next)) throw new Error('Unable to load the complete sandbox list.');
    if (next) seen.add(next);
    if (next && current.deltaPages < DELTA_PAGE_BUDGET) {
      cursor = next;
      continue;
    }
    // Catch changes made while the immutable initial pages were in flight.
    const catchup = snapshotContinuation(page);
    if (!next && catchup) {
      cursor = catchup;
      continue;
    }
    return {
      sandboxes: [...current.rows.values()],
      observedAt: current.observedAt,
      appliedAtMs: current.appliedAtMs,
      changeCursor: next ?? page.change_cursor ?? undefined,
    };
  }
}
