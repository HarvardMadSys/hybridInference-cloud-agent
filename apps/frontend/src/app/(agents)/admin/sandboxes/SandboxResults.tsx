'use client';

import Link from 'next/link';
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { conversationHref } from '@/lib/api/adminConversations';
import type { AdminSandboxState } from '@/lib/api/adminSandboxes';

import { BUTTON, dateTime, ErrorNotice } from '../runs/shared';
import type { SandboxInventorySnapshot } from './inventorySync';
import { finiteMetric, formatBytes, formatCpu, type SandboxObservation } from './present';
import { useSandboxInventory } from './useSandboxInventory';
import {
  FleetSummary,
  ResourceCell,
  ResourceSampleAge,
  SandboxAgeCell,
  SandboxDetails,
  LiveSandboxManagement,
  LiveBatchSandboxManagement,
} from './SandboxLiveValues';
import {
  defaultSortDirection,
  selectSandboxes,
  SORT_OPTIONS,
  type SandboxActivityFilter,
  type SandboxSort,
  type SandboxSortKey,
} from './inventory';

const STATE_OPTIONS: { value: AdminSandboxState | 'all'; label: string }[] = [
  { value: 'running', label: 'Running' },
  { value: 'all', label: 'All states' },
  { value: 'provisioning', label: 'Provisioning' },
  { value: 'starting', label: 'Starting' },
  { value: 'stopping', label: 'Stopping' },
  { value: 'stopped', label: 'Stopped' },
  { value: 'stopped_dirty', label: 'Stopped dirty' },
  { value: 'lost', label: 'Lost' },
  { value: 'cold', label: 'Cold (checkpoint only)' },
  { value: 'restoring', label: 'Restoring' },
];

export function SandboxResults({ onDenied }: { onDenied: () => void }) {
  const [snapshot, setSnapshot] = useState<SandboxInventorySnapshot | null>(null);
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
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [batchBusy, setBatchBusy] = useState(false);
  const batchBusyRef = useRef(false);
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
  const setBatchRunning = useCallback((busy: boolean) => {
    batchBusyRef.current = busy;
    setBatchBusy(busy);
  }, []);
  const completedBatch = useCallback((ids: string[]) => {
    setSelectedIds((current) => new Set([...current].filter((id) => !ids.includes(id))));
  }, []);

  useSandboxInventory({
    revision,
    stateFilter,
    includeArchived,
    denied,
    denyAccess,
    autoRefreshRef,
    batchBusyRef,
    setLoading,
    setError,
    setSnapshot,
  });

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
  const selectedSandboxes = useMemo(
    () => visibleSandboxes.filter(({ sandbox }) => selectedIds.has(sandbox.id)),
    [visibleSandboxes, selectedIds],
  );
  useEffect(() => {
    // Selection belongs to the visible inventory, including the search and host
    // filters. Hidden rows must never become accidental batch targets.
    const visibleIds = new Set(visibleSandboxes.map(({ sandbox }) => sandbox.id));
    setSelectedIds((current) => {
      const next = new Set([...current].filter((id) => visibleIds.has(id)));
      return next.size === current.size ? current : next;
    });
  }, [visibleSandboxes]);

  const selectSandbox = useCallback((id: string, checked: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  function selectAllVisible(checked: boolean) {
    setSelectedIds(
      checked ? new Set(visibleSandboxes.map(({ sandbox }) => sandbox.id)) : new Set(),
    );
  }
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
              disabled={batchBusy}
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
          <button
            type="button"
            className={BUTTON}
            disabled={loading || batchBusy}
            onClick={refresh}
          >
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
            disabled={batchBusy}
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
            disabled={batchBusy}
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
            disabled={batchBusy}
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
            disabled={batchBusy}
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
          disabled={batchBusy}
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
      {snapshot && <FleetSummary sandboxes={snapshot.sandboxes} />}
      <LiveBatchSandboxManagement
        selected={selectedSandboxes}
        onDenied={denyAccess}
        onChanged={stopped}
        onCompleted={completedBatch}
        onBusyChange={setBatchRunning}
      />
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
            onDenied={denyAccess}
            onChanged={stopped}
            selectedIds={selectedIds}
            selectionDisabled={batchBusy}
            onSelect={selectSandbox}
            onSelectAll={selectAllVisible}
          />
        ))}
    </>
  );
}

function SandboxesTable({
  sandboxes,
  sort,
  onSort,
  autoRefresh,
  onDenied,
  onChanged,
  selectedIds,
  selectionDisabled,
  onSelect,
  onSelectAll,
}: {
  sandboxes: SandboxObservation[];
  sort: SandboxSort;
  onSort: (key: SandboxSortKey) => void;
  autoRefresh: boolean;
  onDenied: () => void;
  onChanged: () => void;
  selectedIds: Set<string>;
  selectionDisabled: boolean;
  onSelect: (id: string, checked: boolean) => void;
  onSelectAll: (checked: boolean) => void;
}) {
  const allSelected = sandboxes.every(({ sandbox }) => selectedIds.has(sandbox.id));
  const someSelected = sandboxes.some(({ sandbox }) => selectedIds.has(sandbox.id));
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
            <th scope="col" className="px-3 py-3">
              <input
                type="checkbox"
                aria-label="Select all visible sandboxes"
                checked={allSelected}
                disabled={selectionDisabled}
                ref={(input) => {
                  if (input) input.indeterminate = someSelected && !allSelected;
                }}
                onChange={(event) => onSelectAll(event.target.checked)}
              />
            </th>
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
              onDenied={onDenied}
              onChanged={onChanged}
              selected={selectedIds.has(observation.sandbox.id)}
              selectionDisabled={selectionDisabled}
              onSelect={onSelect}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SandboxRowView({
  observation,
  autoRefresh,
  onDenied,
  onChanged,
  selected,
  selectionDisabled,
  onSelect,
}: {
  observation: SandboxObservation;
  autoRefresh: boolean;
  onDenied: () => void;
  onChanged: () => void;
  selected: boolean;
  selectionDisabled: boolean;
  onSelect: (id: string, checked: boolean) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const { sandbox } = observation;
  const usage = sandbox.resource_usage;
  const name = sandbox.title ?? sandbox.origin_thread_id;
  const parsedQuota = sandbox.cpu_limit.trim() ? Number(sandbox.cpu_limit) : NaN;
  const cpuQuota = finiteMetric(parsedQuota);
  const detailId = `sandbox-stats-${sandbox.id}`;
  return (
    <Fragment>
      <tr className="align-top hover:bg-gray-100 dark:hover:bg-gray-900">
        <td className="px-3 py-3">
          <input
            type="checkbox"
            aria-label={`Select sandbox ${sandbox.id}`}
            checked={selected}
            disabled={selectionDisabled}
            onChange={(event) => onSelect(sandbox.id, event.target.checked)}
          />
        </td>
        <th scope="row" className="max-w-xs px-3 py-3 font-normal">
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={detailId}
            onClick={() => setExpanded((value) => !value)}
            title={sandbox.origin_thread_id}
            className="ui-focus-ring block max-w-full truncate rounded text-left font-medium text-gray-900 hover:underline dark:text-gray-100"
          >
            {name}
          </button>
          <span
            className="mt-1 block truncate text-xs text-gray-500 dark:text-gray-400"
            title={sandbox.repo ?? 'Start from scratch'}
          >
            {sandbox.repo ?? 'Start from scratch'}
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
          {usage && <ResourceSampleAge observation={observation} />}
        </td>
        <td className="px-3 py-3 tabular-nums text-gray-700 dark:text-gray-200">
          {sandbox.member_count}
        </td>
        <SandboxAgeCell observation={observation} value={sandbox.last_activity_at} />
        <SandboxAgeCell
          observation={observation}
          value={sandbox.host_last_heartbeat_at}
          heartbeat
        />
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
          <td colSpan={10} className="bg-gray-50 px-4 py-4 dark:bg-gray-900/60">
            <div id={detailId}>
              <Link
                href={conversationHref(sandbox.origin_thread_id)}
                className={`${BUTTON} mb-4 inline-block`}
                aria-label={`View origin session for ${name}`}
              >
                View origin session
              </Link>
              <SandboxDetails id={`${detailId}-resources`} observation={observation} />
              <LiveSandboxManagement
                sandboxId={sandbox.id}
                autoRefresh={autoRefresh}
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

const SandboxRow = memo(SandboxRowView);
