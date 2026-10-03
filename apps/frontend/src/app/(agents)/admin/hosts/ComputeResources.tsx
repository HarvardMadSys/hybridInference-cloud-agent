'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  actOnComputeOperation,
  getComputeConnections,
  getComputeOperations,
  getComputeProviders,
  getComputeServers,
  resolveComputeOperation,
  validateComputeConnection,
  type ComputeAllocation,
  type ComputeCatalog,
  type ComputeConnection,
  type ComputeOperation,
  type ComputeServerSnapshot,
} from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import AddComputeServer from './AddComputeServer';
import ComputeConnectionForm from './ComputeConnectionForm';
import ComputeResourcePanel from './ComputeResourcePanel';
import {
  absoluteTime,
  buttonClass,
  ErrorNotice,
  Field,
  inputClass,
  OperationDetails,
  Panel,
  primaryClass,
  Reasons,
  remainingTime,
} from './compute-ui';

type OpenPanel =
  | { kind: 'add' }
  | { kind: 'connections' }
  | { kind: 'connection'; id?: string }
  | { kind: 'resource'; id: string }
  | { kind: 'operation'; id: string }
  | null;

const FINISHED = new Set(['succeeded', 'cancelled', 'failed']);

export default function ComputeResources({
  onHostsChanged,
  onInventory,
}: {
  onHostsChanged: () => void;
  onInventory?: (inventory: ComputeServerSnapshot) => void;
}) {
  const [catalog, setCatalog] = useState<ComputeCatalog | null>(null);
  const [connections, setConnections] = useState<ComputeConnection[]>([]);
  const [distribution, setDistribution] = useState<ComputeServerSnapshot | null>(null);
  const [allocations, setAllocations] = useState<ComputeAllocation[]>([]);
  const [operations, setOperations] = useState<ComputeOperation[]>([]);
  const [addConnectionOpen, setAddConnectionOpen] = useState(false);
  const [panel, setPanel] = useState<OpenPanel>(null);
  const [error, setError] = useState<string | null>(null);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [resolveExternalId, setResolveExternalId] = useState('');
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState('');
  const [showAllOperations, setShowAllOperations] = useState(false);
  const generation = useRef(0);
  const mounted = useRef(true);
  const previousOperations = useRef<ComputeOperation[]>([]);
  const hostsChanged = useRef(onHostsChanged);
  hostsChanged.current = onHostsChanged;
  const inventoryChanged = useRef(onInventory);
  inventoryChanged.current = onInventory;

  const refresh = useCallback(async () => {
    const current = ++generation.current;
    // Catalog failure must not conceal a continuing operation or existing Host controls.
    const result = await Promise.allSettled([
      getComputeProviders(),
      getComputeConnections(),
      getComputeServers(),
      getComputeOperations(),
    ]);
    if (!mounted.current || current !== generation.current) return;
    if (
      result.some(
        (item) =>
          item.status === 'rejected' &&
          item.reason instanceof APIError &&
          [401, 403].includes(item.reason.statusCode ?? 0),
      )
    ) {
      setDenied(true);
      setAddConnectionOpen(false);
      setPanel(null);
      setCatalog(null);
      setConnections([]);
      setAllocations([]);
      setDistribution(null);
      setOperations([]);
      return;
    }
    const [providers, accountConnections, servers, ops] = result;
    if (providers.status === 'fulfilled') setCatalog(providers.value);
    if (accountConnections.status === 'fulfilled')
      setConnections(accountConnections.value.connections);
    if (servers.status === 'fulfilled') {
      setAllocations(servers.value.allocations);
      setDistribution(servers.value);
      inventoryChanged.current?.(servers.value);
    }
    if (ops.status === 'fulfilled') {
      if (
        ops.value.operations.some(
          (next) =>
            FINISHED.has(next.state) &&
            previousOperations.current.some(
              (before) => before.id === next.id && before.state !== next.state,
            ),
        )
      ) {
        hostsChanged.current();
      }
      previousOperations.current = ops.value.operations;
      setOperations(ops.value.operations);
    }
    const failed = result.find((item) => item.status === 'rejected');
    setError(failed?.status === 'rejected' ? getErrorMessage(failed.reason) : null);
    if (!failed) setReadAt(new Date().toISOString());
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10_000);
    return () => {
      mounted.current = false;
      generation.current += 1;
      clearInterval(timer);
    };
  }, [refresh]);

  const receiveOperation = (operation: ComputeOperation, submitted = true) => {
    generation.current += 1;
    setOperations((current) => [operation, ...current.filter((item) => item.id !== operation.id)]);
    if (submitted) setPanel({ kind: 'operation', id: operation.id });
    void refresh();
  };
  const receiveConnection = (connection: ComputeConnection) => {
    generation.current += 1;
    setConnections((current) => [
      connection,
      ...current.filter((item) => item.id !== connection.id),
    ]);
    setPanel({ kind: 'connections' });
    void refresh();
  };
  const currentOperation =
    panel?.kind === 'operation' ? operations.find((item) => item.id === panel.id) : undefined;
  const currentAllocation =
    panel?.kind === 'resource' ? allocations.find((item) => item.id === panel.id) : undefined;
  const pending = operations.filter((item) => !FINISHED.has(item.state));
  const shownOperations = showAllOperations ? operations : pending;

  if (denied)
    return (
      <p className="mt-6" role="alert">
        Resource management access was revoked.
      </p>
    );

  return (
    <section
      className="mt-8 space-y-4 rounded-2xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900"
      aria-label="Compute resources"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="font-semibold">Compute resources</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Connect servers, manage leases, and track deployment. Select the global running location
            below once a Host is ready.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            className={buttonClass}
            type="button"
            onClick={() => setPanel({ kind: 'connections' })}
            disabled={!catalog}
          >
            Connections
          </button>
          <button
            className={primaryClass}
            type="button"
            onClick={() => setPanel({ kind: 'add' })}
            disabled={!catalog?.providers.length || !catalog.secret_store_configured}
          >
            Add server
          </button>
        </div>
      </div>
      {!catalog && !error && <p className="text-sm">Loading resource management…</p>}
      {catalog && !catalog.secret_store_configured && (
        <p className="text-sm text-amber-800 dark:text-amber-300">
          Configure the control plane secret store before adding connections or servers.
        </p>
      )}
      {error && (
        <div>
          <ErrorNotice error={error} />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Last complete read: {absoluteTime(readAt)}. Previous observations may be stale.
          </p>
          <button className={buttonClass} type="button" onClick={() => void refresh()}>
            Refresh resource state
          </button>
        </div>
      )}
      {distribution && distribution.hosts.some((host) => host.retained_groups > 0) && (
        <div className="rounded-lg bg-gray-50 p-3 text-sm dark:bg-white/5">
          <h3 className="font-medium">Existing sandbox locations</h3>
          <ul className="mt-1 space-y-1">
            {distribution.hosts
              .filter((host) => host.retained_groups > 0)
              .map((host) => (
                <li key={host.host}>
                  {host.host}: {host.retained_groups} retained sandbox groups · {host.status}
                  {distribution.active_host === host.host ? ' · current global target' : ''}
                </li>
              ))}
          </ul>
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            These groups remain on their owning Hosts, including paused groups. Recorded at{' '}
            {absoluteTime(distribution.observed_at)}.
          </p>
        </div>
      )}
      {allocations.length > 0 && (
        <>
          <div className="flex flex-wrap gap-2">
            <input
              aria-label="Search compute resources"
              className={`${inputClass} sm:!w-64`}
              type="search"
              placeholder="Search resources or nodes"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <select
              aria-label="Filter resource provider"
              className={`${inputClass} sm:!w-48`}
              value={providerFilter}
              onChange={(event) => setProviderFilter(event.target.value)}
            >
              <option value="">All providers</option>
              {catalog?.providers.map((provider) => (
                <option key={provider.id} value={provider.id}>
                  {provider.label}
                </option>
              ))}
            </select>
          </div>
          <ul className="divide-y divide-gray-100 dark:divide-white/10">
            {allocations
              .filter(
                (item) =>
                  (!providerFilter || item.provider_id === providerFilter) &&
                  `${item.name} ${item.external_id} ${item.nodes.map((node) => node.name).join(' ')}`
                    .toLowerCase()
                    .includes(search.toLowerCase()),
              )
              .map((allocation) => (
                <li
                  key={allocation.id}
                  className="flex flex-wrap items-start justify-between gap-3 py-3"
                >
                  <div className="min-w-0 text-sm">
                    <p className="font-medium">
                      {allocation.name}{' '}
                      <span className="font-normal text-gray-500 dark:text-gray-400">
                        ·{' '}
                        {catalog?.providers.find((item) => item.id === allocation.provider_id)
                          ?.label ?? allocation.provider_id}
                      </span>
                    </p>
                    <p>
                      Resource:{' '}
                      {allocation.state === 'absent'
                        ? 'Deregistered (machine still exists)'
                        : allocation.state}{' '}
                      · {allocation.nodes.length} nodes
                    </p>
                    {allocation.nodes.map((node) => (
                      <p key={node.id} className="text-xs text-gray-500 dark:text-gray-400">
                        {node.name}: {node.hosts.join(', ') || 'Awaiting Host enrollment'}
                      </p>
                    ))}
                    {allocation.expires_at ? (
                      <p>
                        Expiry: {absoluteTime(allocation.expires_at)} (
                        {remainingTime(allocation.expires_at)}) · Auto-renewal{' '}
                        {allocation.policy.enabled ? 'enabled' : 'disabled'}
                      </p>
                    ) : (
                      <p>Lease not managed</p>
                    )}
                    <p className="text-xs text-gray-500 dark:text-gray-400">
                      Provider observed: {absoluteTime(allocation.observed_at)}
                    </p>
                    <Reasons reasons={allocation.blocked_reasons} />
                  </div>
                  <button
                    type="button"
                    className={buttonClass}
                    onClick={() => setPanel({ kind: 'resource', id: allocation.id })}
                  >
                    Manage {allocation.name}
                  </button>
                </li>
              ))}
          </ul>
        </>
      )}
      {catalog && allocations.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Existing Hosts remain in the server inventory below. Add a resource to manage its
          provisioning and lease.
        </p>
      )}
      <div className="border-t border-gray-200 pt-3 dark:border-white/10">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold">Operations ({pending.length} active)</h3>
          <button
            type="button"
            className="text-sm underline"
            onClick={() => setShowAllOperations(!showAllOperations)}
          >
            {showAllOperations ? 'Show active operations' : 'Show recent operations'}
          </button>
        </div>
        {shownOperations.length === 0 && (
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
            No {showAllOperations ? 'recent' : 'active'} operations.
          </p>
        )}
        <ul className="mt-2 space-y-2">
          {shownOperations.map((operation) => (
            <li key={operation.id}>
              <button
                type="button"
                className="text-left text-sm underline underline-offset-2"
                onClick={() => setPanel({ kind: 'operation', id: operation.id })}
              >
                {operation.kind} ·{' '}
                {allocations.find((item) => item.id === operation.allocation_id)?.name ??
                  'Resource acquisition'}{' '}
                · {operation.state} · {operation.step}
              </button>
            </li>
          ))}
        </ul>
      </div>

      {panel?.kind === 'add' && catalog && (
        <Panel title="Add server" onClose={() => setPanel(null)}>
          <AddComputeServer
            catalog={catalog}
            connections={connections}
            operations={operations}
            onOperation={receiveOperation}
            onConnection={() => setAddConnectionOpen(true)}
          />
        </Panel>
      )}
      {addConnectionOpen && catalog && (
        <Panel title="Add connection" onClose={() => setAddConnectionOpen(false)}>
          <ComputeConnectionForm
            catalog={catalog}
            onSaved={(connection) => {
              generation.current += 1;
              setConnections((current) => [
                connection,
                ...current.filter((item) => item.id !== connection.id),
              ]);
              setAddConnectionOpen(false);
              void refresh();
            }}
          />
        </Panel>
      )}
      {panel?.kind === 'connection' && catalog && (
        <Panel
          title={panel.id ? 'Edit connection' : 'Add connection'}
          onClose={() => setPanel({ kind: 'connections' })}
        >
          <ComputeConnectionForm
            key={panel.id ?? 'new'}
            catalog={catalog}
            connection={connections.find((item) => item.id === panel.id)}
            onSaved={receiveConnection}
          />
        </Panel>
      )}
      {panel?.kind === 'connections' && catalog && (
        <Panel title="Connections" onClose={() => setPanel(null)}>
          <div className="space-y-4">
            {connections.map((connection) => (
              <div
                key={connection.id}
                className="space-y-2 border-b border-gray-200 pb-3 text-sm dark:border-white/10"
              >
                <p className="font-medium">
                  {connection.name} · {connection.status}
                </p>
                <p>{connection.masked_summary}</p>
                <Reasons reasons={connection.blocked_reasons} />
                <div className="flex gap-2">
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={!catalog.secret_store_configured}
                    onClick={() => setPanel({ kind: 'connection', id: connection.id })}
                  >
                    Edit {connection.name}
                  </button>
                  <button
                    type="button"
                    className={buttonClass}
                    disabled={busy === connection.id || !catalog.secret_store_configured}
                    onClick={async () => {
                      setBusy(connection.id);
                      setError(null);
                      try {
                        receiveConnection(
                          (await validateComputeConnection(connection.id)).connection,
                        );
                      } catch (error) {
                        setError(getErrorMessage(error));
                      } finally {
                        setBusy(null);
                      }
                    }}
                  >
                    Validate {connection.name}
                  </button>
                </div>
              </div>
            ))}
            {!connections.length && <p className="text-sm">No account connections configured.</p>}
            <button
              type="button"
              className={primaryClass}
              disabled={!catalog.secret_store_configured}
              onClick={() => setPanel({ kind: 'connection' })}
            >
              Add connection
            </button>
            <ErrorNotice error={error} />
          </div>
        </Panel>
      )}
      {currentAllocation && (
        <Panel title={currentAllocation.name} onClose={() => setPanel(null)}>
          <ComputeResourcePanel
            key={currentAllocation.id}
            allocation={currentAllocation}
            onOperation={receiveOperation}
            onSaved={(allocation) => {
              generation.current += 1;
              setAllocations((current) =>
                current.map((item) => (item.id === allocation.id ? allocation : item)),
              );
              void refresh();
            }}
          />
        </Panel>
      )}
      {currentOperation && (
        <Panel title="Resource operation" onClose={() => setPanel(null)}>
          <OperationDetails operation={currentOperation} />
          {currentOperation.allowed_actions.includes('resolve') && (
            <form
              className="mt-4 space-y-3"
              onSubmit={async (event) => {
                event.preventDefault();
                setBusy(currentOperation.id);
                setError(null);
                try {
                  receiveOperation(
                    (await resolveComputeOperation(currentOperation.id, resolveExternalId))
                      .operation,
                  );
                  setResolveExternalId('');
                } catch (error) {
                  setError(getErrorMessage(error));
                } finally {
                  setBusy(null);
                }
              }}
            >
              <p className="text-sm">
                Check the provider portal for the resource created by this request. Enter its
                identity to reconcile this existing operation; this does not request another
                machine.
              </p>
              <Field label="Existing experiment UUID">
                <input
                  className={inputClass}
                  required
                  value={resolveExternalId}
                  onChange={(event) => setResolveExternalId(event.target.value)}
                />
              </Field>
              <button className={buttonClass} type="submit" disabled={busy === currentOperation.id}>
                Adopt this experiment and continue
              </button>
            </form>
          )}
          <p className="mt-3 break-all text-xs text-gray-500 dark:text-gray-400">
            Request: {currentOperation.request_id}
          </p>
          <div className="mt-4 flex gap-2">
            {(['retry', 'cancel'] as const)
              .filter((action) => currentOperation.allowed_actions.includes(action))
              .map((action) => (
                <button
                  key={action}
                  className={buttonClass}
                  type="button"
                  disabled={busy === currentOperation.id}
                  onClick={async () => {
                    setBusy(currentOperation.id);
                    setError(null);
                    try {
                      receiveOperation(
                        (await actOnComputeOperation(currentOperation.id, action)).operation,
                      );
                    } catch (error) {
                      setError(getErrorMessage(error));
                    } finally {
                      setBusy(null);
                    }
                  }}
                >
                  {action === 'retry' ? 'Retry operation' : 'Cancel operation'}
                </button>
              ))}
          </div>
          <ErrorNotice error={error} />
        </Panel>
      )}
    </section>
  );
}
