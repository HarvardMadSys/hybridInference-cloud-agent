'use client';

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';

import {
  actOnComputeOperation,
  getComputeConnections,
  getComputeOperations,
  getComputeProviders,
  getComputeServers,
  resolveComputeOperation,
  type ComputeAllocation,
  type ComputeCatalog,
  type ComputeConnection,
  type ComputeOperation,
  type ComputeServerSnapshot,
} from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import AddComputeServer from './AddComputeServer';
import ServerAccess from './ServerAccess';
import ServerActions from './ServerActions';
import ComputeResourcePanel from './ComputeResourcePanel';
import ComputeServerList, { type ComputeServerGroup } from './ComputeServerList';
import {
  absoluteTime,
  buttonClass,
  ErrorNotice,
  Field,
  inputClass,
  OperationDetails,
  Panel,
  primaryClass,
} from './compute-ui';

type OpenPanel =
  | { kind: 'add' }
  | { kind: 'operations' }
  | { kind: 'resource'; id: string }
  | { kind: 'host'; hosts: string[]; name: string }
  | { kind: 'operation'; id: string }
  | null;

const FINISHED = new Set(['succeeded', 'cancelled', 'failed']);

export default function ComputeResources({
  onHostsChanged,
  onInventory,
  placementControl,
  renderHostControls,
  onHostControlsClosed,
  eligibleHosts = [],
  onUseHost,
  settingBusy = false,
  hostNotice,
  activeHost,
  serverGroups,
}: {
  onHostsChanged: () => void;
  onInventory?: (inventory: ComputeServerSnapshot) => void;
  placementControl?: ReactNode;
  renderHostControls?: (hosts: string[]) => ReactNode;
  onHostControlsClosed?: () => void;
  eligibleHosts?: string[];
  onUseHost?: (host: string) => void;
  settingBusy?: boolean;
  hostNotice?: string | null;
  activeHost?: string | null;
  serverGroups?: ComputeServerGroup[];
}) {
  const [catalog, setCatalog] = useState<ComputeCatalog | null>(null);
  const [connections, setConnections] = useState<ComputeConnection[]>([]);
  const [distribution, setDistribution] = useState<ComputeServerSnapshot | null>(null);
  const [allocations, setAllocations] = useState<ComputeAllocation[]>([]);
  const [operations, setOperations] = useState<ComputeOperation[]>([]);
  const [panel, setPanel] = useState<OpenPanel>(null);
  const [serverTab, setServerTab] = useState<
    'overview' | 'lease' | 'activity' | 'access' | 'retire'
  >('overview');
  const [operationOrigin, setOperationOrigin] = useState<OpenPanel>({ kind: 'operations' });
  const [error, setError] = useState<string | null>(null);
  const [readAt, setReadAt] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [resolveExternalId, setResolveExternalId] = useState('');
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
    if (submitted) {
      if (panel?.kind !== 'operation') {
        setOperationOrigin(panel?.kind === 'resource' ? panel : { kind: 'operations' });
      }
      setPanel({ kind: 'operation', id: operation.id });
    }
    void refresh();
  };
  const saveConnectionInList = (connection: ComputeConnection) => {
    generation.current += 1;
    setConnections((current) => [
      connection,
      ...current.filter((item) => item.id !== connection.id),
    ]);
    void refresh();
  };
  function openResource(id: string) {
    onHostControlsClosed?.();
    setServerTab('overview');
    setPanel({ kind: 'resource', id });
  }
  function closeServer() {
    setPanel(null);
    onHostControlsClosed?.();
  }
  function openOperation(id: string) {
    setOperationOrigin(panel);
    setPanel({ kind: 'operation', id });
  }
  const currentOperation =
    panel?.kind === 'operation' ? operations.find((item) => item.id === panel.id) : undefined;
  const currentAllocation =
    panel?.kind === 'resource' ? allocations.find((item) => item.id === panel.id) : undefined;
  const selectedHost = activeHost === undefined ? (distribution?.active_host ?? null) : activeHost;
  const hostFeedback = hostNotice ? (
    <p role="status" className="mb-4 rounded-lg bg-gray-50 px-3 py-2 text-sm dark:bg-white/5">
      {hostNotice}
    </p>
  ) : null;
  function placementControls(hosts: string[]) {
    if (selectedHost && hosts.includes(selectedHost))
      return (
        <p className="rounded-lg bg-crimson/5 px-3 py-2 text-sm text-crimson dark:text-crimson-light">
          Default location for new sandboxes
        </p>
      );
    const ready = [...new Set(hosts)].filter((host) => eligibleHosts.includes(host));
    return (
      onUseHost &&
      ready.map((host) => (
        <button
          key={host}
          type="button"
          className={buttonClass}
          disabled={settingBusy}
          onClick={() => onUseHost(host)}
        >
          Use for new sandboxes{ready.length > 1 ? ` · ${host}` : ''}
        </button>
      ))
    );
  }
  const operationAllocation = allocations.find(
    (item) => item.id === currentOperation?.allocation_id,
  );
  const pending = operations.filter((item) => !FINISHED.has(item.state));
  const shownOperations = showAllOperations ? operations : pending;

  const resourceActions = [];
  if (catalog)
    resourceActions.push({ label: 'Access settings', onSelect: () => setServerTab('access') });
  if (currentAllocation?.allowed_actions.includes('retire')) {
    resourceActions.push({
      label:
        currentAllocation.provider_id === 'existing_ssh' ? 'Disconnect server' : 'Release server',
      onSelect: () => setServerTab('retire'),
    });
  }

  if (denied)
    return (
      <p className="mt-6" role="alert">
        Resource management access was revoked.
      </p>
    );

  return (
    <section className="mt-8" aria-label="Servers">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">Servers</h2>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="rounded-lg px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-white/5"
            type="button"
            onClick={() => setPanel({ kind: 'operations' })}
          >
            Activity
            {pending.length > 0 && (
              <span className="ml-2 rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-800 dark:bg-amber-500/15 dark:text-amber-300">
                {pending.length} active
              </span>
            )}
          </button>
          <button
            className={primaryClass}
            type="button"
            onClick={() => setPanel({ kind: 'add' })}
            aria-label="Add server"
            disabled={!catalog?.providers.length || !catalog.secret_store_configured}
          >
            + Add server
          </button>
        </div>
      </div>
      {placementControl}
      {catalog && !catalog.secret_store_configured && (
        <p className="mb-3 text-sm text-amber-800 dark:text-amber-300">
          Configure the control plane secret store to add servers.
        </p>
      )}
      {error && (
        <div className="mb-4 rounded-xl border border-amber-200 p-4 dark:border-amber-500/25">
          <ErrorNotice error={error} />
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Last updated {absoluteTime(readAt)}. Displayed data may be stale.
          </p>
          <button className="mt-2 text-sm underline" type="button" onClick={() => void refresh()}>
            Refresh resource state
          </button>
        </div>
      )}
      <ComputeServerList
        allocations={allocations}
        serverGroups={serverGroups}
        inventory={distribution}
        catalog={catalog}
        activeHost={selectedHost}
        onManage={openResource}
        onManageHosts={(hosts, name) => {
          onHostControlsClosed?.();
          setPanel({ kind: 'host', hosts, name });
        }}
      />
      {panel?.kind === 'operations' && (
        <Panel title="Server activity" onClose={() => setPanel(null)}>
          <div className="mb-4 flex items-center justify-between gap-3 text-sm">
            <span className="text-gray-500 dark:text-gray-400">{pending.length} active</span>
            <button
              type="button"
              className="underline underline-offset-4"
              onClick={() => setShowAllOperations(!showAllOperations)}
            >
              {showAllOperations ? 'Show active operations' : 'Show recent operations'}
            </button>
          </div>
          {shownOperations.length === 0 && (
            <p className="py-6 text-center text-sm text-gray-500 dark:text-gray-400">
              No {showAllOperations ? 'recent' : 'active'} operations.
            </p>
          )}
          <ul className="divide-y divide-gray-100 dark:divide-white/10">
            {shownOperations.map((operation) => (
              <li key={operation.id}>
                <button
                  type="button"
                  className="w-full rounded-lg px-3 py-4 text-left text-sm hover:bg-gray-50 dark:hover:bg-white/5"
                  onClick={() => openOperation(operation.id)}
                >
                  {operation.kind} ·{' '}
                  {allocations.find((item) => item.id === operation.allocation_id)?.name ??
                    'New server'}{' '}
                  · {operation.state}
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {operation.message || operation.step}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {panel?.kind === 'add' && catalog && (
        <Panel title="Add server" onClose={() => setPanel(null)}>
          <AddComputeServer
            catalog={catalog}
            connections={connections}
            onOperation={receiveOperation}
            onConnectionSaved={saveConnectionInList}
          />
        </Panel>
      )}
      {panel?.kind === 'host' && (
        <Panel title={panel.name} onClose={closeServer}>
          {hostFeedback}
          {placementControls(panel.hosts)}
          {renderHostControls?.(panel.hosts)}
        </Panel>
      )}
      {currentAllocation && (
        <Panel
          title={currentAllocation.name}
          onClose={closeServer}
          actions={<ServerActions items={resourceActions} />}
        >
          {hostFeedback}
          <nav
            className="mb-5 flex gap-1 border-b border-gray-200 dark:border-white/10"
            aria-label="Server sections"
          >
            {(['overview', 'lease', 'activity'] as const)
              .filter(
                (tab) =>
                  tab !== 'lease' ||
                  currentAllocation.allowed_actions.some((action) =>
                    ['policy', 'extend'].includes(action),
                  ),
              )
              .map((tab) => (
                <button
                  key={tab}
                  type="button"
                  aria-pressed={serverTab === tab}
                  className={`border-b-2 px-4 py-2 text-sm ${serverTab === tab ? 'border-crimson font-medium text-crimson dark:text-crimson-light' : 'border-transparent text-gray-500 dark:text-gray-400'}`}
                  onClick={() => setServerTab(tab)}
                >
                  {tab === 'overview' ? 'Overview' : tab === 'lease' ? 'Lease' : 'Activity'}
                </button>
              ))}
          </nav>
          <div hidden={serverTab !== 'overview'} className="space-y-5">
            {placementControls(currentAllocation.nodes.flatMap((node) => node.hosts))}
            {currentAllocation.nodes.some((node) => node.hosts.length) &&
              renderHostControls?.(currentAllocation.nodes.flatMap((node) => node.hosts))}
            <ComputeResourcePanel
              section="details"
              allocation={currentAllocation}
              onSaved={() => undefined}
              onOperation={receiveOperation}
            />
            {catalog && (
              <button type="button" className={buttonClass} onClick={() => setServerTab('access')}>
                Access settings
              </button>
            )}
          </div>
          {(serverTab === 'access' || serverTab === 'retire') && (
            <button
              type="button"
              className="mb-4 text-sm text-gray-500 hover:underline dark:text-gray-400"
              onClick={() => setServerTab('overview')}
            >
              ← Overview
            </button>
          )}
          <div hidden={serverTab !== 'access'}>
            {catalog && (
              <ServerAccess
                key={currentAllocation.id}
                allocation={currentAllocation}
                allocations={allocations}
                catalog={catalog}
                connections={connections}
                onSaved={saveConnectionInList}
              />
            )}
          </div>
          <div hidden={serverTab !== 'retire'}>
            <ComputeResourcePanel
              section="retire"
              allocation={currentAllocation}
              onSaved={() => undefined}
              onOperation={receiveOperation}
            />
          </div>
          <div hidden={serverTab !== 'lease'}>
            <ComputeResourcePanel
              key={currentAllocation.id}
              section="lease"
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
          </div>
          <div hidden={serverTab !== 'activity'}>
            {operations.filter((operation) => operation.allocation_id === currentAllocation.id)
              .length === 0 && (
              <p className="py-6 text-sm text-gray-500 dark:text-gray-400">
                No operations for this server.
              </p>
            )}
            {operations
              .filter((operation) => operation.allocation_id === currentAllocation.id)
              .map((operation) => (
                <button
                  key={operation.id}
                  type="button"
                  className="block w-full border-b border-gray-100 py-4 text-left text-sm dark:border-white/5"
                  onClick={() => openOperation(operation.id)}
                >
                  {operation.kind} · {operation.state}
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {operation.message}
                  </span>
                </button>
              ))}
          </div>
        </Panel>
      )}
      {currentOperation && (
        <Panel
          title="Resource operation"
          onClose={closeServer}
          onBack={() => setPanel(operationOrigin ?? { kind: 'operations' })}
        >
          {operationAllocation && (
            <button
              type="button"
              className={`${buttonClass} mb-4`}
              onClick={() => openResource(operationAllocation.id)}
            >
              View server
            </button>
          )}
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
