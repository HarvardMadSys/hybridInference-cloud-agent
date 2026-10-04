'use client';

import { useState } from 'react';

import type { ComputeAllocation, ComputeCatalog, ComputeServerSnapshot } from '@/lib/api/compute';

import { absoluteTime, buttonClass, inputClass, remainingTime } from './compute-ui';
import styles from './compute-servers.module.css';

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
    <div
      className={`${styles.frame} overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-900`}
    >
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
        <div className="overflow-x-auto">
          <table
            className={`${styles.table} w-full min-w-[680px] text-left text-sm`}
            aria-label="Server inventory"
          >
            <thead className="border-b border-gray-100 bg-gray-50/60 text-xs text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
              <tr>
                <th className="px-5 py-3 font-medium">Server</th>
                <th className="px-4 py-3 font-medium">Status</th>
                <th className="px-4 py-3 font-medium">Sandboxes</th>
                <th className="px-4 py-3 font-medium">Lease</th>
                <th className="px-5 py-3">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {shownAllocations.map((allocation) => {
                const hostIds = allocation.nodes.flatMap((node) => node.hosts);
                const hosts = (inventory?.hosts ?? []).filter(
                  (host) => host.allocation_id === allocation.id || hostIds.includes(host.host),
                );
                const selected =
                  activeHost !== null &&
                  (hostIds.includes(activeHost) || hosts.some((host) => host.host === activeHost));
                return (
                  <tr key={allocation.id} className="align-middle">
                    <th scope="row" className="px-5 py-5 font-normal">
                      <button
                        type="button"
                        className="text-left font-medium text-gray-900 hover:underline dark:text-gray-100"
                        onClick={() => onManage(allocation.id)}
                      >
                        {allocation.name}
                      </button>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                        <span>
                          {catalog?.providers.find(
                            (provider) => provider.id === allocation.provider_id,
                          )?.label ?? allocation.provider_id}
                        </span>
                        {allocation.nodes.length > 1 && (
                          <span>· {allocation.nodes.length} nodes</span>
                        )}
                        {selected && (
                          <span className="rounded bg-crimson/10 px-1.5 py-0.5 font-medium text-crimson dark:text-crimson-light">
                            Selected
                          </span>
                        )}
                      </div>
                    </th>
                    <td data-label="Status" className="px-4 py-5">
                      <State label={serverStatus(allocation, hosts)} />
                      {allocation.blocked_reasons.length > 0 && (
                        <button
                          type="button"
                          className="mt-1 block text-xs text-amber-700 underline dark:text-amber-300"
                          onClick={() => onManage(allocation.id)}
                        >
                          Needs attention
                        </button>
                      )}
                    </td>
                    <td
                      data-label="Sandboxes"
                      className="px-4 py-5 tabular-nums"
                      title="Retained sandbox groups, including paused sandboxes"
                    >
                      {hosts.reduce((count, host) => count + host.retained_groups, 0)}
                    </td>
                    <td data-label="Lease" className="px-4 py-5">
                      {allocation.expires_at ? (
                        <>
                          <span
                            className="whitespace-nowrap tabular-nums"
                            title={absoluteTime(allocation.expires_at)}
                          >
                            {remainingTime(allocation.expires_at).replace(' remaining', ' left')}
                          </span>
                          <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                            Auto-renew {allocation.policy.enabled ? 'on' : 'off'}
                          </span>
                        </>
                      ) : (
                        <span className="text-gray-500 dark:text-gray-400">
                          {allocation.provider_id === 'existing_ssh'
                            ? 'Not managed'
                            : 'Not reported'}
                        </span>
                      )}
                    </td>
                    <td className="px-5 py-5 text-right">
                      <button
                        type="button"
                        className={buttonClass}
                        aria-label={`Manage ${allocation.name}`}
                        onClick={() => onManage(allocation.id)}
                      >
                        Manage
                      </button>
                    </td>
                  </tr>
                );
              })}
              {shownExisting.map((host) => (
                <tr key={host.host} className="align-middle">
                  <th scope="row" className="px-5 py-5 font-normal">
                    <span className="font-medium text-gray-900 dark:text-gray-100">
                      {host.name}
                    </span>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                      <span>Existing server</span>
                      {host.members.some((member) => member.host === activeHost) && (
                        <span className="rounded bg-crimson/10 px-1.5 py-0.5 font-medium text-crimson dark:text-crimson-light">
                          Selected
                        </span>
                      )}
                    </div>
                  </th>
                  <td data-label="Status" className="px-4 py-5">
                    <State
                      label={
                        host.status === 'online' && host.allows_new_groups === false
                          ? 'Not accepting new sandboxes'
                          : host.status
                      }
                    />
                  </td>
                  <td
                    data-label="Sandboxes"
                    className="px-4 py-5 tabular-nums"
                    title="Retained sandbox groups, including paused sandboxes"
                  >
                    {inventory ? host.retained_groups : '—'}
                  </td>
                  <td data-label="Lease" className="px-4 py-5 text-gray-500 dark:text-gray-400">
                    Not managed
                  </td>
                  <td className="px-5 py-5 text-right">
                    {onManageHosts && (
                      <button
                        type="button"
                        className={buttonClass}
                        aria-label={`Manage ${host.name}`}
                        onClick={() =>
                          onManageHosts(
                            host.members.map((member) => member.host),
                            host.name,
                          )
                        }
                      >
                        Manage
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
