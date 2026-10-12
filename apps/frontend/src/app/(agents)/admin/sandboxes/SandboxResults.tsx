'use client';

import Link from 'next/link';
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { conversationHref } from '@/lib/api/adminConversations';
import { getAdminSandboxes, type AdminSandboxState } from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

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
import {
  defaultSortDirection,
  selectSandboxes,
  SORT_OPTIONS,
  type SandboxActivityFilter,
  type SandboxSort,
  type SandboxSortKey,
} from './inventory';

const REFRESH_MS = 30_000;
const STALE_HEARTBEAT_MS = 90_000;
const STATE_OPTIONS: { value: AdminSandboxState | 'all'; label: string }[] = [
  { value: 'running', label: 'Running' },
  { value: 'all', label: 'All states' },
  { value: 'provisioning', label: 'Provisioning' },
  { value: 'starting', label: 'Starting' },
  { value: 'stopping', label: 'Stopping' },
  { value: 'stopped', label: 'Stopped' },
  { value: 'stopped_dirty', label: 'Stopped dirty' },
  { value: 'lost', label: 'Lost' },
];

interface Snapshot {
  sandboxes: SandboxObservation[];
  observedAt: string;
  appliedAtMs: number;
}

export function SandboxResults({ onDenied }: { onDenied: () => void }) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [revision, setRevision] = useState(0);
  const [stateFilter, setStateFilter] = useState<AdminSandboxState | 'all'>('running');
  const [search, setSearch] = useState('');
  const [hostFilter, setHostFilter] = useState('');
  const [activityFilter, setActivityFilter] = useState<SandboxActivityFilter>('all');
  const [sort, setSort] = useState<SandboxSort>({ key: 'id', direction: 'asc' });
  const [autoRefresh, setAutoRefresh] = useState(true);
  const autoRefreshRef = useRef(true);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [nowMs, setNowMs] = useState(() => performance.now());
  const denyAccess = useCallback(() => {
    setSnapshot(null);
    setDenied(true);
    onDenied();
  }, [onDenied]);
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
              ...(stateFilter !== 'running' ? { state: stateFilter } : {}),
              ...(includeArchived ? { include_archived: true } : {}),
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
          denyAccess();
        } else setError(getErrorMessage(e));
      } finally {
        if (!controller.signal.aborted && !disposed) setLoading(false);
        request = null;
      }
    }

    void load();
    const timer = setInterval(() => {
      if (autoRefreshRef.current) void load();
    }, REFRESH_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
      request?.abort();
    };
  }, [revision, stateFilter, includeArchived, denied, denyAccess]);

  const hosts = useMemo(
    () => [...new Set(snapshot?.sandboxes.map(({ sandbox }) => sandbox.host) ?? [])].sort(),
    [snapshot],
  );
  const visibleSandboxes = useMemo(
    () =>
      selectSandboxes(
        snapshot?.sandboxes ?? [],
        { search, host: hostFilter, activity: activityFilter },
        sort,
      ),
    [snapshot, search, hostFilter, activityFilter, sort],
  );
  const hasFilters = search !== '' || hostFilter !== '' || activityFilter !== 'all';
  const stateLabel = STATE_OPTIONS.find(({ value }) => value === stateFilter)?.label ?? stateFilter;

  function sortBy(key: SandboxSortKey) {
    setSort((current) => ({
      key,
      direction:
        current.key === key
          ? current.direction === 'asc'
            ? 'desc'
            : 'asc'
          : defaultSortDirection(key),
    }));
  }

  if (denied) return null;
  return (
    <>
      <div className="mt-8 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-50">
          {stateFilter === 'all' ? 'All sandboxes' : `${stateLabel} sandboxes`}{' '}
          {snapshot && (
            <span className="ml-2 text-sm font-normal tabular-nums text-gray-500 dark:text-gray-400">
              ({snapshot.sandboxes.length})
            </span>
          )}
        </h2>
        <div className="flex flex-wrap items-center gap-3">
          <label className="text-sm text-gray-500 dark:text-gray-400">
            State
            <select
              aria-label="Sandbox state"
              value={stateFilter}
              className="ui-input ml-2 min-h-11"
              onChange={(event) => {
                setSnapshot(null);
                setLoading(true);
                setStateFilter(event.target.value as AdminSandboxState | 'all');
              }}
            >
              {STATE_OPTIONS.map(({ value, label }) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2 text-sm text-gray-500 dark:text-gray-400">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(event) => {
                autoRefreshRef.current = event.target.checked;
                setAutoRefresh(event.target.checked);
              }}
            />
            Auto-refresh
          </label>
          <button type="button" className={BUTTON} disabled={loading} onClick={refresh}>
            Refresh
          </button>
        </div>
      </div>
      <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
        Includes idle containers retained between turns. Choose All states to inspect stopped or
        lost sandboxes. {autoRefresh ? 'Refreshes every 30 seconds.' : 'Automatic refresh paused.'}
      </p>
      <div className="mt-5 flex flex-wrap items-end gap-3">
        <label className="min-w-52 flex-1 text-xs text-gray-500 dark:text-gray-400">
          Search sandboxes
          <input
            type="search"
            value={search}
            placeholder="Session, repository, owner, host or container…"
            onChange={(event) => setSearch(event.target.value)}
            className="mt-1 block w-full rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
          />
        </label>
        <label className="text-xs text-gray-500 dark:text-gray-400">
          Host
          <select
            aria-label="Sandbox host"
            value={hostFilter}
            onChange={(event) => setHostFilter(event.target.value)}
            className="mt-1 block rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
          >
            <option value="">All hosts</option>
            {hostFilter && !hosts.includes(hostFilter) && (
              <option value={hostFilter}>{hostFilter}</option>
            )}
            {hosts.map((host) => (
              <option key={host} value={host}>
                {host}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-gray-500 dark:text-gray-400">
          Activity
          <select
            aria-label="Sandbox activity"
            value={activityFilter}
            onChange={(event) => setActivityFilter(event.target.value as SandboxActivityFilter)}
            className="mt-1 block rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
          >
            <option value="all">All activity</option>
            <option value="executing">Executing</option>
            <option value="idle">Idle</option>
          </select>
        </label>
        <label className="text-xs text-gray-500 dark:text-gray-400">
          Sort by
          <select
            aria-label="Sort sandboxes"
            value={sort.key}
            onChange={(event) => {
              const key = event.target.value as SandboxSortKey;
              setSort({ key, direction: defaultSortDirection(key) });
            }}
            className="mt-1 block rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"
          >
            {SORT_OPTIONS.map(({ key, label }) => (
              <option key={key} value={key}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <button
          type="button"
          className={BUTTON}
          aria-label={`Sort ${sort.direction === 'asc' ? 'descending' : 'ascending'}`}
          onClick={() => sortBy(sort.key)}
        >
          {sort.direction === 'asc' ? '↑ Ascending' : '↓ Descending'}
        </button>
        {hasFilters && (
          <button
            type="button"
            className={BUTTON}
            onClick={() => {
              setSearch('');
              setHostFilter('');
              setActivityFilter('all');
            }}
          >
            Clear filters
          </button>
        )}
      </div>
      <label className="mt-3 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
        <input
          type="checkbox"
          checked={includeArchived}
          onChange={(event) => {
            setSnapshot(null);
            setLoading(true);
            setIncludeArchived(event.target.checked);
          }}
        />
        Include removed sandboxes
      </label>
      {snapshot && (
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Last refreshed: {dateTime(snapshot.observedAt)}
        </p>
      )}
      {snapshot && (
        <p role="status" className="mt-4 text-xs text-gray-500 dark:text-gray-400">
          Showing {visibleSandboxes.length} of {snapshot.sandboxes.length} sandboxes.
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
        ) : visibleSandboxes.length === 0 ? (
          <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
            No sandboxes match your filters.
          </p>
        ) : (
          <SandboxesTable
            sandboxes={visibleSandboxes}
            sort={sort}
            onSort={sortBy}
            autoRefresh={autoRefresh}
            now={Date.parse(snapshot.observedAt) + Math.max(0, nowMs - snapshot.appliedAtMs)}
            nowMs={nowMs}
            onDenied={denyAccess}
            onChanged={stopped}
          />
        ))}
    </>
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

function SandboxesTable({
  sandboxes,
  sort,
  onSort,
  autoRefresh,
  now,
  nowMs,
  onDenied,
  onChanged,
}: {
  sandboxes: SandboxObservation[];
  sort: SandboxSort;
  onSort: (key: SandboxSortKey) => void;
  autoRefresh: boolean;
  now: number;
  nowMs: number;
  onDenied: () => void;
  onChanged: () => void;
}) {
  const columns: { label: string; key?: SandboxSortKey }[] = [
    { label: 'Sandbox / session', key: 'name' },
    { label: 'Owner / host', key: 'owner' },
    { label: 'Activity / agent', key: 'activity' },
    { label: 'CPU', key: 'cpu' },
    { label: 'Memory', key: 'memory' },
    { label: 'Sessions', key: 'sessions' },
    { label: 'Last activity', key: 'last_activity' },
    { label: 'Host heartbeat', key: 'heartbeat' },
    { label: 'Stats' },
  ];
  return (
    <div
      role="region"
      aria-label="Sandboxes table"
      tabIndex={0}
      className="ui-card ui-focus-ring mt-4 overflow-x-auto"
    >
      <table className="w-full min-w-[950px] text-left text-sm">
        <caption className="sr-only">Sandboxes across users and hosts</caption>
        <thead className="bg-surface-muted text-xs text-muted">
          <tr className="border-b border-border">
            {columns.map(({ label, key }) => (
              <th
                key={label}
                scope="col"
                aria-sort={
                  key && sort.key === key
                    ? sort.direction === 'asc'
                      ? 'ascending'
                      : 'descending'
                    : undefined
                }
                className="px-3 py-3 font-medium"
              >
                {key ? (
                  <button
                    type="button"
                    aria-label={`Sort by ${label}`}
                    onClick={() => onSort(key)}
                    className="flex items-center gap-1 rounded py-1 text-left hover:text-gray-900 focus-visible:outline focus-visible:outline-2 dark:hover:text-gray-100"
                  >
                    {label}
                    <span aria-hidden="true" className="text-gray-400 dark:text-gray-500">
                      {sort.key === key ? (sort.direction === 'asc' ? '↑' : '↓') : '↕'}
                    </span>
                  </button>
                ) : (
                  label
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {sandboxes.map((observation) => (
            <SandboxRow
              key={observation.sandbox.id}
              observation={observation}
              autoRefresh={autoRefresh}
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
  autoRefresh,
  now,
  nowMs,
  onDenied,
  onChanged,
}: {
  observation: SandboxObservation;
  autoRefresh: boolean;
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
          {sandbox.archived_at && (
            <span className="mt-1 block text-xs text-amber-700 dark:text-amber-400">
              Removed from inventory
            </span>
          )}
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
          <div className="flex flex-col items-start gap-2">
            <Link
              href={`/admin/sandboxes/${encodeURIComponent(sandbox.id)}`}
              className={BUTTON}
              aria-label={`View trajectory for ${name}`}
            >
              Trajectory
            </Link>
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
          </div>
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
                autoRefresh={autoRefresh}
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
