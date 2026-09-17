'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { McpAddDialog, type McpAddMode } from './McpAddDialog';
import {
  authorizeMcpConnection,
  disconnectMcpConnection,
  listMcpConnections,
  testMcpConnection,
  enableMcpOAuth,
  setMcpConnectionEnabled,
  type McpConnectionApi,
  type McpConnectionsApi,
} from '@/lib/api/mcp';

interface CapabilityCounts {
  tool_count: number;
  resource_count: number;
  prompt_count: number;
}

export function capabilitySummary(counts: CapabilityCounts): string {
  const parts: string[] = [];
  if (counts.tool_count)
    parts.push(`${counts.tool_count} ${counts.tool_count === 1 ? 'tool' : 'tools'}`);
  if (counts.resource_count)
    parts.push(
      `${counts.resource_count} ${counts.resource_count === 1 ? 'resource' : 'resources'}`,
    );
  if (counts.prompt_count)
    parts.push(`${counts.prompt_count} ${counts.prompt_count === 1 ? 'prompt' : 'prompts'}`);
  return parts.join(' · ') || 'No tools discovered';
}

export interface McpAuthorizationResult {
  ok: boolean;
  connectionId: string | null;
  message: string | null;
}

function needsSignIn(connection: McpConnectionApi) {
  return (
    connection.status !== 'disabled' &&
    ((connection.auth_kind === 'oauth' && !connection.has_credential) ||
      connection.error_kind === 'needs_authorization' ||
      connection.error_kind === 'client_registration_required')
  );
}

const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5';

/** A server list with one next action, automatic discovery, and optional details. */
export function McpConnectionsSection({
  authorizationResult,
}: {
  authorizationResult?: McpAuthorizationResult;
}) {
  const [data, setData] = useState<McpConnectionsApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [adding, setAdding] = useState<McpAddMode | null>(null);
  const [clientIdFor, setClientIdFor] = useState<{ id: number; message: string } | null>(null);
  const [clientId, setClientId] = useState('');
  const authorizationNoticeChecked = useRef(false);

  const load = useCallback(async () => {
    const response = await listMcpConnections();
    setData(response);
    return response;
  }, []);

  const check = useCallback(
    async (connections: McpConnectionApi[]) => {
      setBusy(new Set(connections.map((connection) => connection.id)));
      setError(null);
      setNotice(null);
      try {
        const results = await Promise.allSettled(
          connections.map(async (connection) => {
            const result = await testMcpConnection(connection.id);
            return { connection, result };
          }),
        );
        let authorizationNeeded: McpConnectionApi | null = null;
        const failures: string[] = [];
        const connected: string[] = [];
        for (let index = 0; index < results.length; index++) {
          const outcome = results[index];
          if (outcome.status === 'rejected') {
            failures.push(
              `${connections[index].label}: ${outcome.reason instanceof Error ? outcome.reason.message : 'Could not connect. Try again.'}`,
            );
          } else if (outcome.value.result.ok) {
            connected.push(
              `${outcome.value.connection.label} connected. ${capabilitySummary(outcome.value.result)}.`,
            );
          } else {
            if (
              connections.length === 1 &&
              outcome.value.connection.auth_kind === 'none' &&
              outcome.value.result.error_kind === 'needs_authorization'
            ) {
              authorizationNeeded = outcome.value.connection;
            }
            failures.push(`${outcome.value.connection.label}: ${outcome.value.result.message}`);
          }
        }
        if (connected.length) setNotice(connected.join(' '));
        if (failures.length) setError(failures.join(' '));
        await load();
        return authorizationNeeded;
      } catch (cause: unknown) {
        setError(cause instanceof Error ? cause.message : 'Could not refresh your servers.');
      } finally {
        setBusy(new Set());
      }
    },
    [load],
  );

  useEffect(() => {
    let cancelled = false;
    void load()
      .then(async (response) => {
        if (cancelled || !authorizationResult || authorizationNoticeChecked.current) return;
        authorizationNoticeChecked.current = true;
        if (!authorizationResult.ok) {
          setError(authorizationResult.message || 'Sign-in did not complete. Try again.');
          return;
        }
        const connection = response.connections.find(
          (item) => String(item.id) === authorizationResult.connectionId && item.has_credential,
        );
        if (connection) await check([connection]);
      })
      .catch((cause: unknown) => {
        if (!cancelled)
          setError(cause instanceof Error ? cause.message : 'Could not load your servers.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load, check, authorizationResult]);

  async function authorize(connection: McpConnectionApi, suppliedClientId?: string) {
    setBusy(new Set([connection.id]));
    setError(null);
    setNotice(null);
    try {
      // The user explicitly chose Sign in after discovering that this URL
      // needs OAuth. Do not change static-token connections on their behalf.
      if (connection.auth_kind === 'none') await enableMcpOAuth(connection);
      const result = await authorizeMcpConnection(connection.id, suppliedClientId);
      if (result.authorization_url) {
        window.location.assign(result.authorization_url);
        return;
      }
      if (result.needs_client_id) {
        setClientIdFor({ id: connection.id, message: result.message });
        setClientId(suppliedClientId ?? '');
      } else {
        setClientIdFor(null);
        setNotice(result.message || `${connection.label} signed in.`);
      }
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not start sign-in.');
      // A successful auth-kind update may precede a failed discovery request.
      // Refresh its revision so a retry does not use the stale config fence.
      await load().catch(() => undefined);
    } finally {
      setBusy(new Set());
    }
  }

  async function added(connections: McpConnectionApi[]) {
    setAdding(null);
    setData((current) => ({
      connections: [...connections, ...(current?.connections ?? [])],
      templates: current?.templates ?? [],
    }));
    if (
      connections.length === 1 &&
      connections[0].auth_kind === 'oauth' &&
      !connections[0].has_credential
    ) {
      await authorize(connections[0]);
    } else {
      const authorizationNeeded = await check(connections);
      if (authorizationNeeded) await authorize(authorizationNeeded);
    }
  }

  async function disconnect(connection: McpConnectionApi) {
    if (
      !globalThis.confirm(
        `Disconnect ${connection.label}? Agents can no longer use it, and its stored credential is cleared.`,
      )
    )
      return;
    setBusy(new Set([connection.id]));
    setError(null);
    setNotice(null);
    try {
      await disconnectMcpConnection(connection.id);
      if (clientIdFor?.id === connection.id) setClientIdFor(null);
      setNotice(`${connection.label} disconnected.`);
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not disconnect the server.');
    } finally {
      setBusy(new Set());
    }
  }

  async function toggle(connection: McpConnectionApi) {
    setBusy(new Set([connection.id]));
    setError(null);
    setNotice(null);
    const enabled = connection.enabled === false;
    try {
      await setMcpConnectionEnabled(connection.id, enabled);
      await load();
      setNotice(
        enabled
          ? `${connection.label} is enabled. Available tools will appear with your next message.`
          : `${connection.label} is off in all conversations. Your connection is saved.`,
      );
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not update this server.');
    } finally {
      setBusy(new Set());
    }
  }

  const connections = data?.connections ?? [];
  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-gray-900 dark:text-gray-100">MCP servers</h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Connect once. Enabled servers are available automatically in your conversations.
          </p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            disabled={loading || busy.size > 0}
            onClick={() => setAdding('json')}
            className={BUTTON}
          >
            Add with JSON
          </button>
          <button
            type="button"
            disabled={loading || busy.size > 0}
            onClick={() => setAdding('servers')}
            className="rounded-lg bg-gray-900 px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
          >
            Add server
          </button>
        </div>
      </div>
      {notice ? (
        <p
          role="status"
          className="mt-4 rounded-lg bg-emerald-50 px-3 py-2 text-sm text-emerald-800 dark:bg-emerald-500/10 dark:text-emerald-300"
        >
          {notice}
        </p>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="mt-4 rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-400"
        >
          {error}
          <button
            type="button"
            disabled={busy.size > 0}
            onClick={() => {
              setError(null);
              void load().catch((cause: unknown) =>
                setError(cause instanceof Error ? cause.message : 'Could not load your servers.'),
              );
            }}
            className="ml-3 font-medium underline"
          >
            Refresh
          </button>
        </div>
      ) : null}
      <div className="mt-5 overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
        {loading && !data ? (
          <p role="status" className="p-5 text-sm text-gray-500 dark:text-gray-400">
            Loading servers…
          </p>
        ) : null}
        {!loading && !connections.length ? (
          <div className="px-5 py-8 text-center">
            <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
              Your tools, in every conversation
            </p>
            <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
              Browse MCP servers or paste an MCP JSON configuration to get started.
            </p>
          </div>
        ) : null}
        {connections.map((connection) => {
          const working = busy.has(connection.id);
          const signIn = needsSignIn(connection) && connection.auth_kind !== 'static';
          const enabled = connection.enabled !== false;
          const active = enabled && connection.status === 'active' && !signIn;
          const state = working
            ? 'Connecting…'
            : !enabled
              ? 'Off'
              : signIn
                ? 'Sign-in required'
                : active
                  ? 'Connected'
                  : connection.status === 'disabled'
                    ? 'Disconnected'
                    : connection.status === 'draft'
                      ? 'Ready to connect'
                      : 'Connection failed';
          return (
            <div
              key={connection.id}
              className="border-t border-gray-100 p-4 first:border-t-0 dark:border-white/5 sm:p-5"
            >
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2.5">
                    <span
                      aria-hidden="true"
                      className={`h-2 w-2 rounded-full ${working ? 'animate-pulse bg-amber-400' : active ? 'bg-emerald-500' : signIn ? 'bg-amber-500' : 'bg-gray-400 dark:bg-gray-500'}`}
                    />
                    <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                      {connection.label}
                    </h3>
                    <span className="text-xs text-gray-500 dark:text-gray-400">{state}</span>
                  </div>
                  <p className="mt-1.5 text-xs text-gray-500 dark:text-gray-400">
                    {active
                      ? capabilitySummary(connection)
                      : (connection.url ?? connection.argv.join(' '))}
                  </p>
                </div>
                <div className="flex gap-2">
                  {enabled && connection.status !== 'disabled' ? (
                    <button
                      type="button"
                      disabled={busy.size > 0}
                      onClick={() => void (signIn ? authorize(connection) : check([connection]))}
                      className={BUTTON}
                    >
                      {working
                        ? 'Connecting…'
                        : signIn
                          ? 'Sign in'
                          : active
                            ? 'Refresh tools'
                            : 'Retry connection'}
                    </button>
                  ) : null}
                  {connection.status !== 'disabled' ? (
                    <button
                      type="button"
                      role="switch"
                      aria-label={`Enable ${connection.label}`}
                      aria-checked={enabled}
                      disabled={busy.size > 0}
                      onClick={() => void toggle(connection)}
                      className={`relative h-6 w-11 shrink-0 self-center rounded-full transition-colors disabled:opacity-50 ${enabled ? 'bg-gray-900 dark:bg-gray-100' : 'bg-gray-200 dark:bg-white/20'}`}
                    >
                      <span
                        className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform dark:bg-gray-900 ${enabled ? 'translate-x-5' : ''}`}
                      />
                    </button>
                  ) : null}
                </div>
              </div>
              {connection.error_detail && !working ? (
                <p className="mt-2 text-xs text-red-600 dark:text-red-400">
                  {connection.error_detail}
                </p>
              ) : null}
              {clientIdFor?.id === connection.id ? (
                <form
                  className="mt-3 rounded-lg border border-gray-200 p-3 dark:border-white/10"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void authorize(connection, clientId.trim());
                  }}
                >
                  <p className="text-sm text-gray-600 dark:text-gray-300">{clientIdFor.message}</p>
                  <label
                    className="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300"
                    htmlFor={`mcp-client-id-${connection.id}`}
                  >
                    OAuth client ID
                  </label>
                  <div className="mt-1 flex gap-2">
                    <input
                      id={`mcp-client-id-${connection.id}`}
                      value={clientId}
                      onChange={(event) => setClientId(event.target.value)}
                      className="min-w-0 flex-1 rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-white/10 dark:text-gray-100"
                    />
                    <button
                      type="submit"
                      disabled={!clientId.trim() || busy.size > 0}
                      className={BUTTON}
                    >
                      Continue
                    </button>
                  </div>
                </form>
              ) : null}
              <details className="mt-3 text-xs text-gray-500 dark:text-gray-400">
                <summary className="w-fit cursor-pointer hover:text-gray-900 dark:hover:text-gray-100">
                  Server details
                </summary>
                <dl className="mt-3 space-y-2">
                  <div>
                    <dt className="font-medium">
                      {connection.transport === 'stdio' ? 'Command' : 'URL'}
                    </dt>
                    <dd className="mt-1 break-all font-mono">
                      {connection.url ?? connection.argv.join(' ')}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-medium">Connection</dt>
                    <dd className="mt-1">
                      {connection.transport} · {connection.location}
                    </dd>
                  </div>
                  {connection.server_name ? (
                    <div>
                      <dt className="font-medium">Server</dt>
                      <dd>
                        {connection.server_name} {connection.server_version}
                      </dd>
                    </div>
                  ) : null}
                  {connection.protocol_version ? (
                    <div>
                      <dt className="font-medium">Protocol</dt>
                      <dd>{connection.protocol_version}</dd>
                    </div>
                  ) : null}
                  {connection.has_credential ? (
                    <div>
                      <dt className="font-medium">Credential stored</dt>
                      <dd>{connection.account_label ?? connection.secret_hint}</dd>
                    </div>
                  ) : null}
                </dl>
                {connection.status !== 'disabled' ? (
                  <div className="mt-3 flex gap-3">
                    {connection.auth_kind === 'oauth' && connection.has_credential ? (
                      <button
                        type="button"
                        disabled={busy.size > 0}
                        onClick={() => void authorize(connection)}
                        className={BUTTON}
                      >
                        Sign in again
                      </button>
                    ) : null}
                    <button
                      type="button"
                      disabled={busy.size > 0}
                      onClick={() => void disconnect(connection)}
                      className="text-red-600 disabled:opacity-50 dark:text-red-400"
                    >
                      Disconnect
                    </button>
                  </div>
                ) : null}
              </details>
            </div>
          );
        })}
      </div>
      {adding ? (
        <McpAddDialog
          templates={data?.templates ?? []}
          initialMode={adding}
          onClose={() => setAdding(null)}
          onAdded={(connections) => void added(connections)}
        />
      ) : null}
    </div>
  );
}
