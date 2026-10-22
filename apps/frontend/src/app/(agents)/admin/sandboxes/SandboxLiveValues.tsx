'use client';

import { useMemo, type ComponentProps } from 'react';
import { ago, dateTime } from '../runs/shared';
import {
  finiteMetric,
  formatBytes,
  formatCpu,
  formatDiskLimit,
  formatDuration,
  formatRate,
  sampleFreshness,
  STALE_RESOURCE_SECONDS,
  summarizeSandboxes,
  type SandboxObservation,
} from './present';
import { SandboxManagement } from './SandboxManagement';
import { BatchSandboxManagement } from './BatchSandboxManagement';
import { useSandboxClock } from './sandboxClock';

const STALE_HEARTBEAT_MS = 90_000;

function serverNow(observation: SandboxObservation, nowMs: number) {
  return (
    Date.parse(observation.serverObservedAt ?? '') + Math.max(0, nowMs - observation.observedAtMs)
  );
}

/** Count crossed deadlines in O(log N), without reducing the fleet on each tick. */
function expiredCount(deadlines: number[], nowMs: number) {
  let low = 0;
  let high = deadlines.length;
  while (low < high) {
    const middle = (low + high) >>> 1;
    if (deadlines[middle] < nowMs) low = middle + 1;
    else high = middle;
  }
  return low;
}

export function ResourceSampleAge({ observation }: { observation: SandboxObservation }) {
  const label = useSandboxClock((nowMs) => {
    const freshness = sampleFreshness(observation.sandbox, observation.observedAtMs, nowMs);
    return `${freshness.stale ? 'Stale sample' : 'Sample'} · ${formatDuration(freshness.ageSeconds)} old`;
  });
  return (
    <span
      className={`mt-1 block whitespace-nowrap ${label.startsWith('Stale') ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'}`}
    >
      {label}
    </span>
  );
}

export function SandboxAgeCell({
  observation,
  value,
  heartbeat = false,
}: {
  observation: SandboxObservation;
  value: string | null;
  heartbeat?: boolean;
}) {
  const display = useSandboxClock((nowMs) => {
    const now = serverNow(observation, nowMs);
    const time = Date.parse(value ?? '');
    const label = heartbeat && !Number.isFinite(time) ? 'Unknown' : ago(value, now);
    return (
      label + (heartbeat && Number.isFinite(time) && now - time > STALE_HEARTBEAT_MS ? '\\0' : '')
    );
  });
  const stale = display.endsWith('\\0');
  return (
    <td
      className="whitespace-nowrap px-3 py-3 text-xs text-gray-500 dark:text-gray-400"
      title={dateTime(value)}
    >
      {stale ? display.slice(0, -2) : display}
      {stale && <span className="mt-1 block text-amber-700 dark:text-amber-400">Stale</span>}
    </td>
  );
}

export function LiveSandboxManagement(
  props: Omit<ComponentProps<typeof SandboxManagement>, 'nowMs'>,
) {
  const nowMs = useSandboxClock();
  return <SandboxManagement {...props} nowMs={nowMs} />;
}

export function LiveBatchSandboxManagement(
  props: Omit<ComponentProps<typeof BatchSandboxManagement>, 'nowMs'>,
) {
  const nowMs = useSandboxClock((now) => (props.selected.length > 0 ? now : 0));
  return <BatchSandboxManagement {...props} nowMs={nowMs} />;
}

export function FleetSummary({ sandboxes }: { sandboxes: SandboxObservation[] }) {
  const running = useMemo(
    () => sandboxes.filter(({ sandbox }) => sandbox.state === 'running'),
    [sandboxes],
  );
  const deadlines = useMemo(
    () =>
      running
        .flatMap(({ sandbox, observedAtMs }) => {
          const usage = sandbox.resource_usage;
          const age = finiteMetric(usage?.age_seconds);
          return !usage || usage.stale || age === null
            ? []
            : [observedAtMs + (STALE_RESOURCE_SECONDS - age) * 1000];
        })
        .sort((left, right) => left - right),
    [running],
  );
  const readSummary = useMemo(() => {
    let held: { expired: number; summary: ReturnType<typeof summarizeSandboxes> } | undefined;
    return (nowMs: number) => {
      const expired = expiredCount(deadlines, nowMs);
      if (!held || held.expired !== expired) {
        held = { expired, summary: summarizeSandboxes(running, nowMs) };
      }
      return held.summary;
    };
  }, [running, deadlines]);
  const summary = useSandboxClock(readSummary);
  const cards = [
    { label: 'Running sandboxes', value: summary.sandboxCount },
    { label: 'Active turns', value: summary.activeTurns },
    { label: 'Sessions', value: summary.sessions },
    { label: 'Hosts', value: summary.hosts },
    {
      label: 'Measured CPU',
      value: formatCpu(summary.cpuCoverage > 0 ? summary.cpuCores : null),
      coverage: summary.cpuCoverage,
    },
    {
      label: 'Measured memory',
      value: formatBytes(summary.memoryCoverage > 0 ? summary.memoryBytes : null),
      coverage: summary.memoryCoverage,
    },
  ];
  return (
    <dl
      aria-label="Sandbox fleet summary"
      className="mt-5 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6"
    >
      {cards.map(({ label, value, coverage }) => (
        <div key={label} className="ui-card p-4">
          <dt className="text-xs text-muted">{label}</dt>
          <dd className="mt-1 text-lg font-semibold tabular-nums text-gray-900 dark:text-gray-100">
            {value}
          </dd>
          {coverage !== undefined && (
            <dd className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {coverage} / {summary.sandboxCount} sandboxes · fresh samples only
            </dd>
          )}
        </div>
      ))}
    </dl>
  );
}

export function ResourceCell({
  value,
  limit,
  formatted,
  label,
}: {
  value: number | null | undefined;
  limit: number | null | undefined;
  formatted: string;
  label: string;
}) {
  const amount = finiteMetric(value);
  const ceiling = finiteMetric(limit);
  return (
    <div>
      <span className="whitespace-nowrap font-medium tabular-nums text-gray-900 dark:text-gray-100">
        {formatted}
      </span>
      {amount !== null && ceiling !== null && ceiling > 0 && (
        <>
          <meter
            min={0}
            max={ceiling}
            value={Math.min(amount, ceiling)}
            aria-label={label}
            className="mt-1 block h-2 w-24 accent-blue-600"
          />
          <span className="mt-1 block tabular-nums text-gray-500 dark:text-gray-400">
            {Math.round((amount / ceiling) * 100)}% of limit
          </span>
        </>
      )}
    </div>
  );
}

function sandboxAge(createdAt: string | null, now: number) {
  const created = Date.parse(createdAt ?? '');
  return Number.isFinite(created)
    ? formatDuration(Math.max(0, now - created) / 1000)
    : 'Unavailable';
}

function terminalCount({ sandbox }: SandboxObservation, now: number) {
  const terminals = finiteMetric(sandbox.terminal_count);
  if (terminals === null) return 'Unknown';
  const observed = Date.parse(
    sandbox.terminal_count_observed_at ?? sandbox.host_last_heartbeat_at ?? '',
  );
  if (!Number.isFinite(observed)) return `${terminals} · Freshness unknown`;
  return `${terminals}${now - observed > STALE_HEARTBEAT_MS ? ' · Stale count' : ''}`;
}

export function SandboxDetails({
  id,
  observation,
}: {
  id: string;
  observation: SandboxObservation;
}) {
  const nowMs = useSandboxClock();
  const { sandbox, observedAtMs } = observation;
  const usage = sandbox.resource_usage;
  const freshness = sampleFreshness(sandbox, observedAtMs, nowMs);
  const observedNow = serverNow(observation, nowMs);
  const cpuWindow = finiteMetric(usage?.cpu_window_seconds);
  const stats = [
    [
      'Network received',
      `${formatBytes(usage?.net_rx_bytes)} total · ${formatRate(usage?.net_rx_bytes_per_second)}`,
    ],
    [
      'Network sent',
      `${formatBytes(usage?.net_tx_bytes)} total · ${formatRate(usage?.net_tx_bytes_per_second)}`,
    ],
    [
      'Disk read',
      `${formatBytes(usage?.io_read_bytes)} total · ${formatRate(usage?.io_read_bytes_per_second)}`,
    ],
    [
      'Disk written',
      `${formatBytes(usage?.io_write_bytes)} total · ${formatRate(usage?.io_write_bytes_per_second)}`,
    ],
    ['Processes', finiteMetric(usage?.pids) ?? 'Unavailable'],
    [
      'CPU measurement window',
      cpuWindow === null
        ? 'Unavailable'
        : `${cpuWindow.toLocaleString('en-US', { maximumFractionDigits: 3 })}s`,
    ],
    [
      'Sample age',
      usage
        ? `${formatDuration(freshness.ageSeconds)}${freshness.stale ? ' · Stale' : ' · Fresh'}`
        : 'Unavailable',
    ],
    ['Sampled at', dateTime(usage?.sampled_at)],
    ['Received at', dateTime(usage?.received_at)],
    ['Sandbox age', sandboxAge(sandbox.created_at, observedNow)],
    [
      'Sessions / capacity',
      `${sandbox.member_count} / ${finiteMetric(sandbox.resident_slot_capacity) ?? 'Unavailable'}`,
    ],
    ['Terminals', terminalCount(observation, observedNow)],
    ['Active operations', finiteMetric(sandbox.active_operation_count) ?? 'Unavailable'],
    ['Snapshot', sandbox.snapshot_state || 'Unavailable'],
    ['Idle timeout', dateTime(sandbox.idle_deadline)],
    ['Last activity', dateTime(sandbox.last_activity_at)],
    ['Execution profile', sandbox.execution_profile],
    [
      'Configured limits',
      `${sandbox.cpu_limit} CPU · ${sandbox.memory_limit} memory · ${formatDiskLimit(sandbox.disk_limit_mib)}`,
    ],
    ['Reported memory limit', formatBytes(usage?.memory_limit_bytes)],
    ['Container ID', sandbox.container_id ?? 'Unavailable'],
  ];
  return (
    <div
      id={id}
      role="region"
      aria-label={`Stats for ${sandbox.title ?? sandbox.origin_thread_id}`}
    >
      <p
        className={`mb-3 text-xs ${usage && freshness.stale ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'}`}
      >
        {usage
          ? `Container metrics from Docker stats${freshness.stale ? ' · stale values are excluded from fleet totals' : ''}. Network and disk I/O are cumulative counters, with rates from consecutive samples.`
          : 'Resource usage is unavailable until the host reports a container sample.'}
      </p>
      <dl className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-4">
        {stats.map(([label, value]) => (
          <div key={label}>
            <dt className="text-xs text-muted">{label}</dt>
            <dd className="mt-1 break-words text-xs tabular-nums text-gray-900 dark:text-gray-100">
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
