'use client';

import Link from 'next/link';
import { Fragment, useCallback, useEffect, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { conversationHref } from '@/lib/api/adminConversations';
import { getAdminSandboxes } from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { accessDenied, ago, BUTTON, dateTime, ErrorNotice } from '../runs/shared';
import {
  finiteMetric,
  formatBytes,
  formatCpu,
  formatDiskLimit,
  formatDuration,
  formatRate,
  sampleFreshness,
  summarizeSandboxes,
  type SandboxObservation,
} from './present';
import { SandboxManagement } from './SandboxManagement';

const REFRESH_MS = 30_000;
const STALE_HEARTBEAT_MS = 90_000;

function AdminRequired() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Sandboxes across users are visible to admins only.
      </p>
    </div>
  );
}

export default function AdminSandboxesPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return <SandboxesView key={state.user?.id ?? 'admin'} />;
}

interface Snapshot {
  sandboxes: SandboxObservation[];
  observedAt: string;
  appliedAtMs: number;
}

function SandboxesView() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [revision, setRevision] = useState(0);
  const [stateFilter, setStateFilter] = useState<'running' | 'all'>('running');
  const [nowMs, setNowMs] = useState(() => performance.now());
  const denyAccess = useCallback(() => {
    setSnapshot(null);
    setDenied(true);
  }, []);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);
  const stopped = useCallback(() => {
    // Keep pending and confirmed stops visible while the host applies them.
    setStateFilter('all');
    setRevision((value) => value + 1);
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setNowMs(performance.now()), 1_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (denied) return;
    let disposed = false;
    let accessRevoked = false;
    let request: AbortController | null = null;

    async function load() {
      if (request || accessRevoked || disposed) return;
      const controller = new AbortController();
      request = controller;
      setLoading(true);
      setError(null);
      try {
        // Each refresh traverses the whole fleet, without a creation-time
        // window. Replace the snapshot only after all pages succeeded.
        const sandboxes = new Map<string, SandboxObservation>();
        const cursors = new Set<string>();
        let cursor: string | undefined;
        let observedAt = '';
        let appliedAtMs = 0;
        do {
          const page = await getAdminSandboxes(
            {
              limit: 200,
              ...(stateFilter === 'all' ? { state: 'all' } : {}),
              ...(cursor ? { cursor } : {}),
            },
            controller.signal,
          );
          if (controller.signal.aborted || disposed) return;
          const pageObservedAtMs = performance.now();
          for (const sandbox of page.sandboxes)
            sandboxes.set(sandbox.id, {
              sandbox,
              observedAtMs: pageObservedAtMs,
              serverObservedAt: page.observed_at,
            });
          if (!observedAt) {
            // Use the server's clock and let the oldest page keep ageing
            // through pagination; browser wall-clock skew cannot hide staleness.
            observedAt = page.observed_at;
            appliedAtMs = pageObservedAtMs;
          }
          cursor = page.next_cursor ?? undefined;
          if (cursor && cursors.has(cursor))
            throw new Error('Unable to load the complete sandbox list.');
          if (cursor) cursors.add(cursor);
        } while (cursor);
        setSnapshot({ sandboxes: [...sandboxes.values()], observedAt, appliedAtMs });
      } catch (e) {
        if (controller.signal.aborted || disposed) return;
        if (accessDenied(e)) {
          accessRevoked = true;
          setSnapshot(null);
          setDenied(true);
        } else setError(getErrorMessage(e));
      } finally {
        if (!controller.signal.aborted && !disposed) setLoading(false);
        request = null;
      }
    }

    void load();
    const timer = setInterval(() => void load(), REFRESH_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
      request?.abort();
    };
  }, [revision, stateFilter, denied]);

  if (denied) return <AdminRequired />;
  return (
    <div className="min-w-0 flex-1">
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="sandboxes" />
        <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-50">
            {stateFilter === 'running' ? 'Running sandboxes' : 'All sandboxes'}{' '}
            {snapshot && (
              <span className="ml-2 text-sm font-normal tabular-nums text-gray-500 dark:text-gray-400">
                ({snapshot.sandboxes.length})
              </span>
            )}
          </h2>
          <div className="flex items-center gap-3">
            <label className="text-sm text-gray-500 dark:text-gray-400">
              State
              <select
                aria-label="Sandbox state"
                value={stateFilter}
                className="ml-2 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
                onChange={(event) => {
                  setSnapshot(null);
                  setLoading(true);
                  setStateFilter(event.target.value as 'running' | 'all');
                }}
              >
                <option value="running">Running</option>
                <option value="all">All states</option>
              </select>
            </label>
            <button type="button" className={BUTTON} disabled={loading} onClick={refresh}>
              Refresh
            </button>
          </div>
        </div>
        <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
          Includes idle containers retained between turns. Choose All states to inspect stopped or
          lost sandboxes. Refreshes every 30 seconds.
        </p>
        {snapshot && (
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Last refreshed: {dateTime(snapshot.observedAt)}
          </p>
        )}
        {error && (
          <ErrorNotice>
            {snapshot && 'Refresh failed. '}
            {error}
          </ErrorNotice>
        )}
        {loading && (
          <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
            {snapshot ? 'Refreshing' : 'Loading'}{' '}
            {stateFilter === 'running' ? 'running sandboxes…' : 'sandboxes…'}
          </p>
        )}
        {snapshot && (
          <FleetSummary
            sandboxes={snapshot.sandboxes.filter(({ sandbox }) => sandbox.state === 'running')}
            nowMs={nowMs}
          />
        )}
        {snapshot &&
          (snapshot.sandboxes.length === 0 ? (
            <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
              {stateFilter === 'running' ? 'No running sandboxes.' : 'No sandboxes.'}
            </p>
          ) : (
            <SandboxesTable
              sandboxes={snapshot.sandboxes}
              now={Date.parse(snapshot.observedAt) + Math.max(0, nowMs - snapshot.appliedAtMs)}
              nowMs={nowMs}
              onDenied={denyAccess}
              onChanged={stopped}
            />
          ))}
      </section>
    </div>
  );
}

function FleetSummary({ sandboxes, nowMs }: { sandboxes: SandboxObservation[]; nowMs: number }) {
  const summary = summarizeSandboxes(sandboxes, nowMs);
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
        <div
          key={label}
          className="rounded-xl border border-gray-200 bg-white p-3 dark:border-white/10 dark:bg-gray-900"
        >
          <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
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

function SandboxesTable({
  sandboxes,
  now,
  nowMs,
  onDenied,
  onChanged,
}: {
  sandboxes: SandboxObservation[];
  now: number;
  nowMs: number;
  onDenied: () => void;
  onChanged: () => void;
}) {
  return (
    <div className="mt-4 overflow-x-auto">
      <table className="w-full min-w-[950px] text-left text-sm">
        <caption className="sr-only">Sandboxes across users and hosts</caption>
        <thead className="text-xs text-gray-500 dark:text-gray-400">
          <tr className="border-b border-gray-100 dark:border-white/5">
            {[
              'Sandbox / session',
              'Owner / host',
              'Activity / agent',
              'CPU',
              'Memory',
              'Sessions',
              'Last activity',
              'Host heartbeat',
              'Stats',
            ].map((label) => (
              <th key={label} scope="col" className="px-3 py-2 font-normal">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {sandboxes.map((observation) => (
            <SandboxRow
              key={observation.sandbox.id}
              observation={observation}
              now={now}
              nowMs={nowMs}
              onDenied={onDenied}
              onChanged={onChanged}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SandboxRow({
  observation,
  now,
  nowMs,
  onDenied,
  onChanged,
}: {
  observation: SandboxObservation;
  now: number;
  nowMs: number;
  onDenied: () => void;
  onChanged: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { sandbox, observedAtMs } = observation;
  const usage = sandbox.resource_usage;
  const freshness = sampleFreshness(sandbox, observedAtMs, nowMs);
  const heartbeat = Date.parse(sandbox.host_last_heartbeat_at ?? '');
  const stale = Number.isFinite(heartbeat) && now - heartbeat > STALE_HEARTBEAT_MS;
  const name = sandbox.title ?? sandbox.origin_thread_id;
  const parsedQuota = sandbox.cpu_limit.trim() ? Number(sandbox.cpu_limit) : NaN;
  const cpuQuota = finiteMetric(parsedQuota);
  const detailId = `sandbox-stats-${sandbox.id}`;
  return (
    <Fragment>
      <tr className="align-top hover:bg-gray-100 dark:hover:bg-gray-900">
        <th scope="row" className="max-w-xs px-3 py-3 font-normal">
          <Link
            href={conversationHref(sandbox.origin_thread_id)}
            title={sandbox.origin_thread_id}
            className="block truncate font-medium text-gray-900 hover:underline dark:text-gray-100"
          >
            {sandbox.title ?? sandbox.origin_thread_id}
          </Link>
          <span
            className="mt-1 block truncate text-xs text-gray-500 dark:text-gray-400"
            title={sandbox.repo}
          >
            {sandbox.repo}
          </span>
          <span
            className="mt-1 block truncate font-mono text-xs text-gray-500 dark:text-gray-400"
            title={`Container: ${sandbox.container_name}${sandbox.container_id ? ` (${sandbox.container_id})` : ''}`}
          >
            {sandbox.id}
          </span>
          <span
            className="mt-1 block truncate font-mono text-xs text-gray-500 dark:text-gray-400"
            title={sandbox.container_id ?? undefined}
          >
            {sandbox.container_name}
          </span>
        </th>
        <td className="max-w-40 px-3 py-3 text-xs text-gray-500 dark:text-gray-400">
          <span className="block truncate font-mono" title={sandbox.user_id}>
            {sandbox.user_id}
          </span>
          <Link
            href="/admin/hosts"
            className="mt-1 block text-gray-700 hover:underline dark:text-gray-200"
          >
            {sandbox.host}
          </Link>
        </td>
        <td className="whitespace-nowrap px-3 py-3 text-xs">
          <span
            className={
              sandbox.active_attempt_count > 0
                ? 'text-blue-700 dark:text-blue-400'
                : 'text-gray-500 dark:text-gray-400'
            }
          >
            {sandbox.state !== 'running'
              ? sandbox.state.replaceAll('_', ' ')
              : sandbox.active_attempt_count > 0
                ? 'Executing'
                : 'Retained'}
          </span>
          {sandbox.active_attempt_count > 0 && (
            <span className="mt-1 block tabular-nums text-gray-500 dark:text-gray-400">
              {sandbox.active_attempt_count} active{' '}
              {sandbox.active_attempt_count === 1 ? 'turn' : 'turns'}
            </span>
          )}
          <span className="mt-1 block text-gray-500 dark:text-gray-400">{sandbox.runtime_id}</span>
          {sandbox.runtime_version && <span className="mt-1 block">{sandbox.runtime_version}</span>}
        </td>
        <td className="px-3 py-3 text-xs">
          <ResourceCell
            value={usage?.cpu_cores}
            limit={cpuQuota}
            formatted={formatCpu(usage?.cpu_cores)}
            label={`CPU quota for ${name}`}
          />
          <span className="mt-1 block whitespace-nowrap text-gray-500 dark:text-gray-400">
            {sandbox.cpu_limit} CPU quota
          </span>
        </td>
        <td className="px-3 py-3 text-xs">
          <ResourceCell
            value={usage?.memory_bytes}
            limit={usage?.memory_limit_bytes}
            formatted={formatBytes(usage?.memory_bytes)}
            label={`Memory limit for ${name}`}
          />
          <span className="mt-1 block whitespace-nowrap text-gray-500 dark:text-gray-400">
            {finiteMetric(usage?.memory_limit_bytes) !== null
              ? `${formatBytes(usage?.memory_limit_bytes)} reported limit`
              : `${sandbox.memory_limit} configured`}
          </span>
          {usage && (
            <span
              className={`mt-1 block whitespace-nowrap ${freshness.stale ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'}`}
            >
              {freshness.stale ? 'Stale sample' : 'Sample'} · {formatDuration(freshness.ageSeconds)}{' '}
              old
            </span>
          )}
        </td>
        <td className="px-3 py-3 tabular-nums text-gray-700 dark:text-gray-200">
          {sandbox.member_count}
        </td>
        <td
          className="whitespace-nowrap px-3 py-3 text-xs text-gray-500 dark:text-gray-400"
          title={dateTime(sandbox.last_activity_at)}
        >
          {ago(sandbox.last_activity_at, now)}
        </td>
        <td
          className="whitespace-nowrap px-3 py-3 text-xs text-gray-500 dark:text-gray-400"
          title={dateTime(sandbox.host_last_heartbeat_at)}
        >
          {Number.isFinite(heartbeat) ? ago(sandbox.host_last_heartbeat_at, now) : 'Unknown'}
          {stale && <span className="mt-1 block text-amber-700 dark:text-amber-400">Stale</span>}
        </td>
        <td className="px-3 py-3">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailId}
            aria-label={`${expanded ? 'Hide' : 'Show'} details for ${name}`}
            className={BUTTON}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? 'Hide' : 'Details'}
          </button>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={9} className="bg-gray-50 px-4 py-4 dark:bg-gray-900/60">
            <div id={detailId}>
              <SandboxDetails
                id={`${detailId}-resources`}
                observation={observation}
                now={now}
                nowMs={nowMs}
              />
              <SandboxManagement
                sandboxId={sandbox.id}
                nowMs={nowMs}
                onDenied={onDenied}
                onChanged={onChanged}
              />
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

function ResourceCell({
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

function SandboxDetails({
  id,
  observation,
  now,
  nowMs,
}: {
  id: string;
  observation: SandboxObservation;
  now: number;
  nowMs: number;
}) {
  const { sandbox, observedAtMs } = observation;
  const usage = sandbox.resource_usage;
  const freshness = sampleFreshness(sandbox, observedAtMs, nowMs);
  const serverNow = observation.serverObservedAt
    ? Date.parse(observation.serverObservedAt) + Math.max(0, nowMs - observedAtMs)
    : now;
  const terminalObservedAt = Date.parse(
    sandbox.terminal_count_observed_at ?? sandbox.host_last_heartbeat_at ?? '',
  );
  const terminalStale =
    Number.isFinite(terminalObservedAt) && serverNow - terminalObservedAt > STALE_HEARTBEAT_MS;
  const terminals = finiteMetric(sandbox.terminal_count);
  const created = Date.parse(sandbox.created_at);
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
    [
      'Sandbox age',
      Number.isFinite(created)
        ? formatDuration(Math.max(0, serverNow - created) / 1000)
        : 'Unavailable',
    ],
    [
      'Sessions / capacity',
      `${sandbox.member_count} / ${finiteMetric(sandbox.resident_slot_capacity) ?? 'Unavailable'}`,
    ],
    [
      'Terminals',
      terminals === null
        ? 'Unknown'
        : `${terminals}${terminalStale ? ' · Stale count' : Number.isFinite(terminalObservedAt) ? '' : ' · Freshness unknown'}`,
    ],
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
            <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
            <dd className="mt-1 break-words text-xs tabular-nums text-gray-900 dark:text-gray-100">
              {value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
