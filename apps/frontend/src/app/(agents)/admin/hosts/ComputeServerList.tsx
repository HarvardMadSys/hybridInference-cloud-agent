'use client';

import { useState } from 'react';

import type { ComputeAllocation, ComputeCatalog, ComputeServerSnapshot } from '@/lib/api/compute';

import { absoluteTime, inputClass, remainingTime } from './compute-ui';

type InventoryHost = ComputeServerSnapshot['hosts'][number];

export interface ComputeServerGroup {
  name: string;
  host?: { host: string };
  members: { host: string; status?: string }[];
}

function serverStatus(allocation: ComputeAllocation, hosts: InventoryHost[]): string {
  if (allocation.state === 'absent') return 'Disconnected';
  if (allocation.state === 'terminated') return 'Released';
  if (allocation.state !== 'ready') return allocation.state.replaceAll('_', ' ');
  if (hosts.length === 0) return 'Awaiting agent';
  if (hosts.every((host) => host.status === 'online' && host.allows_new_groups !== false)) {
    return 'Online';
  }
  if (hosts.some((host) => host.status === 'online' && host.allows_new_groups !== false)) {
    return 'Partly online';
  }
  if (hosts.some((host) => host.status === 'online')) return 'Not accepting new sandboxes';
  return hosts[0].status.replaceAll('_', ' ');
}

function State({ label }: { label: string }) {
  const ready = label === 'Online' || label === 'online';
  const inactive = ['Disconnected', 'Released', 'disabled'].includes(label);
  return (
    <span
      className={`inline-flex items-center gap-2 text-sm capitalize ${ready ? 'text-emerald-700 dark:text-emerald-400' : inactive ? 'text-gray-500 dark:text-gray-400' : 'text-amber-700 dark:text-amber-300'}`}
    >
      <span
        aria-hidden="true"
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${ready ? 'bg-emerald-500' : inactive ? 'bg-gray-400 dark:bg-gray-500' : 'bg-amber-500'}`}
      />
      {label === 'online' ? 'Online' : label}
    </span>
  );
}

function ServerRow({
  name,
  source,
  status,
  selected,
  sandboxes,
  lease,
  warning,
  onManage,
}: {
  name: string;
  source: string;
  status: string;
  selected: boolean;
  sandboxes: number | null;
  lease?: { expiresAt: string; nodes: number };
  warning?: string;
  onManage?: () => void;
}) {
  return (
    <li aria-label={name} className="flex items-start gap-3 px-5 py-5 sm:gap-4 sm:px-6">
      <svg
        aria-hidden="true"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        className="mt-0.5 h-5 w-5 shrink-0 text-gray-400 dark:text-gray-500"
      >
        <rect x="3" y="3" width="18" height="7" rx="2" />
        <rect x="3" y="14" width="18" height="7" rx="2" />
        <path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6" strokeLinecap="round" />
      </svg>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <button
            type="button"
            className="break-words text-left text-sm font-semibold text-gray-900 hover:underline disabled:cursor-default dark:text-gray-100"
            disabled={!onManage}
            onClick={onManage}
          >
            {name}
          </button>
          {selected && (
            <span className="rounded-full bg-crimson/10 px-2 py-0.5 text-[11px] font-medium text-crimson dark:text-crimson-light">
              Current target
            </span>
          )}
        </div>
        <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
          <State label={status} />
          <span aria-hidden="true">·</span>
          <span>{source}</span>
          {sandboxes !== null && (
            <>
              <span aria-hidden="true">·</span>
              <span>
                {sandboxes} {sandboxes === 1 ? 'sandbox' : 'sandboxes'}
              </span>
            </>
          )}
          {lease && (
            <>
              <span aria-hidden="true">·</span>
              <span title={absoluteTime(lease.expiresAt)}>
                {remainingTime(lease.expiresAt).replace(' remaining', ' left')}
              </span>
              {lease.nodes > 1 && <span>· {lease.nodes} nodes</span>}
            </>
          )}
        </div>
        {warning && (
          <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
            {warning}{' '}
            <button type="button" className="underline underline-offset-2" onClick={onManage}>
              Review
            </button>
          </p>
        )}
      </div>
      {onManage && (
        <button
          type="button"
          aria-label={`Manage ${name}`}
          title="Server details"
          className="-mr-2 rounded-lg px-2 text-xl leading-6 text-gray-400 dark:text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:hover:bg-white/5 dark:hover:text-gray-200"
          onClick={onManage}
        >
          ⋯
        </button>
      )}
    </li>
  );
}

export default function ComputeServerList({
  allocations,
  inventory,
  catalog,
  activeHost,
  onManage,
  onManageHosts,
  serverGroups,
}: {
  allocations: ComputeAllocation[];
  inventory: ComputeServerSnapshot | null;
  catalog: ComputeCatalog | null;
  activeHost: string | null;
  onManage: (id: string) => void;
  onManageHosts?: (hosts: string[], name: string) => void;
  serverGroups?: ComputeServerGroup[];
}) {
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState('');
  const linkedHosts = new Set(
    allocations.flatMap((allocation) => allocation.nodes.flatMap((node) => node.hosts)),
  );
  const existingHosts: InventoryHost[] =
    inventory?.hosts.filter((host) => !host.allocation_id && !linkedHosts.has(host.host)) ??
    (serverGroups ?? []).flatMap((server) =>
      server.members.map((member) => ({
        host: member.host,
        status: member.status ?? 'unknown',
        retained_groups: 0,
        allocation_id: null,
        compute_node_id: null,
      })),
    );
  // Preserve the existing physical-server grouping for manually enrolled hosts.
  // Retained groups belong to every member, while actions target the representative.
  const groupedIds = new Set<string>();
  const existingServers = (serverGroups ?? []).flatMap((server) => {
    const members = existingHosts.filter((host) =>
      server.members.some((member) => member.host === host.host),
    );
    if (!members.length) return [];
    members.forEach((host) => groupedIds.add(host.host));
    const representative = members.find((host) => host.host === server.host?.host) ?? members[0];
    return [
      {
        ...representative,
        name: server.name,
        members,
        status: server.host ? representative.status : 'No compatible agent',
        retained_groups: members.reduce((total, host) => total + host.retained_groups, 0),
      },
    ];
  });
  existingServers.push(
    ...existingHosts
      .filter((host) => !groupedIds.has(host.host))
      .map((host) => ({ ...host, name: host.host, members: [host] })),
  );
  const matches = (text: string) => text.toLowerCase().includes(search.toLowerCase());
  const shownAllocations = allocations.filter(
    (allocation) =>
      (!providerFilter || allocation.provider_id === providerFilter) &&
      matches(
        [
          allocation.name,
          allocation.external_id,
          ...allocation.nodes.flatMap((node) => [node.name, ...node.hosts]),
        ].join(' '),
      ),
  );
  const shownExisting = existingServers.filter(
    (host) =>
      (!providerFilter || providerFilter === 'unmanaged') &&
      matches([host.name, ...host.members.map((member) => member.host)].join(' ')),
  );

  return (
    <div className="rounded-2xl border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-900">
      {(allocations.length + existingServers.length > 5 || search || providerFilter) && (
        <div className="flex flex-wrap gap-2 border-b border-gray-100 p-4 dark:border-white/5">
          <input
            aria-label="Search servers"
            className={`${inputClass} sm:!w-64`}
            type="search"
            placeholder="Find a server…"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <select
            aria-label="Filter server source"
            className={`${inputClass} sm:!w-48`}
            value={providerFilter}
            onChange={(event) => setProviderFilter(event.target.value)}
          >
            <option value="">All sources</option>
            {catalog?.providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.label}
              </option>
            ))}
            <option value="unmanaged">Existing server</option>
          </select>
        </div>
      )}
      {!inventory && !serverGroups ? (
        <p className="p-8 text-center text-sm text-gray-500 dark:text-gray-400">Loading servers…</p>
      ) : shownAllocations.length + shownExisting.length === 0 ? (
        <p className="p-8 text-center text-sm text-gray-500 dark:text-gray-400">
          {search || providerFilter
            ? 'No matching servers.'
            : 'Add your first server to run sandboxes.'}
        </p>
      ) : (
        <ul aria-label="Server inventory" className="divide-y divide-gray-100 dark:divide-white/5">
          {shownAllocations.map((allocation) => {
            const hostIds = allocation.nodes.flatMap((node) => node.hosts);
            const hosts = (inventory?.hosts ?? []).filter(
              (host) => host.allocation_id === allocation.id || hostIds.includes(host.host),
            );
            const providerName =
              catalog?.providers.find((provider) => provider.id === allocation.provider_id)
                ?.label ?? allocation.provider_id;
            return (
              <ServerRow
                key={allocation.id}
                name={allocation.name}
                source={allocation.provider_id === 'existing_ssh' ? 'SSH' : providerName}
                status={serverStatus(allocation, hosts)}
                selected={
                  activeHost !== null &&
                  (hostIds.includes(activeHost) || hosts.some((host) => host.host === activeHost))
                }
                sandboxes={hosts.reduce((total, host) => total + host.retained_groups, 0)}
                lease={
                  allocation.expires_at
                    ? { expiresAt: allocation.expires_at, nodes: allocation.nodes.length }
                    : undefined
                }
                warning={allocation.blocked_reasons[0]?.message}
                onManage={() => onManage(allocation.id)}
              />
            );
          })}
          {shownExisting.map((host) => {
            const memberIds = host.members.map((member) => member.host);
            const manage = onManageHosts ? () => onManageHosts(memberIds, host.name) : undefined;
            return (
              <ServerRow
                key={host.host}
                name={host.name}
                source="Existing server"
                status={
                  host.status === 'online' && host.allows_new_groups === false
                    ? 'Not accepting new sandboxes'
                    : host.status
                }
                selected={host.members.some((member) => member.host === activeHost)}
                sandboxes={inventory ? host.retained_groups : null}
                onManage={manage}
              />
            );
          })}
        </ul>
      )}
    </div>
  );
}
