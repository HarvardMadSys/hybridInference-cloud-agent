import type { AdminSandbox } from '@/lib/api/adminSandboxes';

import { finiteMetric, type SandboxObservation } from './present';

export type SandboxSortKey =
  | 'id'
  | 'name'
  | 'owner'
  | 'host'
  | 'state'
  | 'runtime'
  | 'activity'
  | 'cpu'
  | 'memory'
  | 'sessions'
  | 'last_activity'
  | 'heartbeat'
  | 'created';

export interface SandboxSort {
  key: SandboxSortKey;
  direction: 'asc' | 'desc';
}

export type SandboxActivityFilter = 'all' | 'executing' | 'idle';

export const SORT_OPTIONS: { key: SandboxSortKey; label: string }[] = [
  { key: 'id', label: 'Sandbox ID' },
  { key: 'name', label: 'Sandbox / session' },
  { key: 'owner', label: 'Owner' },
  { key: 'host', label: 'Host' },
  { key: 'state', label: 'State' },
  { key: 'runtime', label: 'Agent' },
  { key: 'activity', label: 'Active turns' },
  { key: 'cpu', label: 'CPU' },
  { key: 'memory', label: 'Memory' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'last_activity', label: 'Last activity' },
  { key: 'heartbeat', label: 'Host heartbeat' },
  { key: 'created', label: 'Created' },
];

export function defaultSortDirection(key: SandboxSortKey): SandboxSort['direction'] {
  return [
    'activity',
    'cpu',
    'memory',
    'sessions',
    'last_activity',
    'heartbeat',
    'created',
  ].includes(key)
    ? 'desc'
    : 'asc';
}

function dateValue(value: string | null): number | null {
  const parsed = Date.parse(value ?? '');
  return Number.isFinite(parsed) ? parsed : null;
}

const sortValues: Record<SandboxSortKey, (sandbox: AdminSandbox) => string | number | null> = {
  id: (sandbox) => sandbox.id,
  name: (sandbox) => sandbox.title ?? sandbox.origin_thread_id,
  owner: (sandbox) => sandbox.user_id,
  host: (sandbox) => sandbox.host,
  state: (sandbox) => sandbox.state,
  runtime: (sandbox) => sandbox.runtime_id,
  activity: (sandbox) => finiteMetric(sandbox.active_attempt_count),
  cpu: (sandbox) => finiteMetric(sandbox.resource_usage?.cpu_cores),
  memory: (sandbox) => finiteMetric(sandbox.resource_usage?.memory_bytes),
  sessions: (sandbox) => finiteMetric(sandbox.member_count),
  last_activity: (sandbox) => dateValue(sandbox.last_activity_at),
  heartbeat: (sandbox) => dateValue(sandbox.host_last_heartbeat_at),
  created: (sandbox) => dateValue(sandbox.created_at),
};

/** Sort the complete inventory by raw values; unavailable readings always come last. */
export function selectSandboxes(
  observations: SandboxObservation[],
  filters: { search: string; host: string; activity: SandboxActivityFilter },
  sort: SandboxSort,
): SandboxObservation[] {
  const terms = filters.search.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const filtered = observations.filter(({ sandbox }) => {
    if (filters.host && sandbox.host !== filters.host) return false;
    if (filters.activity === 'executing' && sandbox.active_attempt_count === 0) return false;
    if (
      filters.activity === 'idle' &&
      (sandbox.state !== 'running' ||
        sandbox.active_attempt_count > 0 ||
        (sandbox.active_operation_count ?? 0) > 0 ||
        ['pending', 'snapshotting', 'importing'].includes(sandbox.snapshot_state ?? ''))
    )
      return false;
    const searchable = [
      sandbox.id,
      sandbox.origin_thread_id,
      sandbox.title,
      sandbox.repo,
      sandbox.user_id,
      sandbox.host,
      sandbox.container_id,
      sandbox.container_name,
      sandbox.runtime_id,
      sandbox.runtime_version,
      sandbox.state,
    ]
      .filter(Boolean)
      .join(' ')
      .toLocaleLowerCase();
    return terms.every((term) => searchable.includes(term));
  });
  return filtered.sort((left, right) => {
    const a = sortValues[sort.key](left.sandbox);
    const b = sortValues[sort.key](right.sandbox);
    if (a === null && b !== null) return 1;
    if (b === null && a !== null) return -1;
    const comparison =
      a === null || b === null
        ? 0
        : typeof a === 'number' && typeof b === 'number'
          ? a - b
          : String(a).localeCompare(String(b), 'en', { numeric: true, sensitivity: 'base' });
    return (
      comparison * (sort.direction === 'asc' ? 1 : -1) ||
      left.sandbox.id.localeCompare(right.sandbox.id)
    );
  });
}
