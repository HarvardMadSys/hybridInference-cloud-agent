'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { McpAddDialog } from './McpAddDialog';
import {
  authorizeMcpConnection,
  disconnectMcpConnection,
  listMcpConnections,
  testMcpConnection,
  type McpConnectionApi,
  type McpConnectionStatus,
  type McpConnectionsApi,
} from '@/lib/api/mcp';

/** How each lifecycle state reads to its owner. */
const STATUS_PILL: Record<McpConnectionStatus, { label: string; className: string }> = {
  draft: {
    label: 'Not tested',
    className:
      'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400 ring-1 ring-inset ring-gray-200/70 dark:ring-white/10',
  },
  active: {
    label: 'Active',
    className:
      'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-1 ring-inset ring-emerald-100 dark:ring-emerald-500/25',
  },
  error: {
    label: 'Error',
    className:
      'bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400 ring-1 ring-inset ring-red-100 dark:ring-red-500/25',
  },
  disabled: {
    label: 'Off',
    className:
      'bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400 ring-1 ring-inset ring-gray-200/70 dark:ring-white/10',
  },
};

const TRANSPORT_LABEL: Record<McpConnectionApi['transport'], string> = {
  'streamable-http': 'Streamable HTTP',
  'http-sse': 'HTTP + SSE',
  stdio: 'Local process',
};

const LOCATION_LABEL: Record<McpConnectionApi['location'], string> = {
  remote: 'Remote server',
  'managed-worker': 'Managed worker',
  'user-connector': 'Your connector',
};

/**
 * How the platform reaches one connection, in the terms the user chose it by.
 *
 * The launch vector is shown rather than a shell string for a local process:
 * what runs is the argv the platform will execute, and rendering it as
 * something copy-pasteable would misrepresent where it runs.
 */
function targetSummary(connection: McpConnectionApi): string {
  if (connection.transport === 'stdio') {
    return connection.argv.length ? connection.argv.join(' ') : 'No command recorded';
  }
  return connection.url ?? 'No address recorded';
}

/** Counts the interface reads together, from a connection or a test result. */
interface CapabilityCounts {
  tool_count: number;
  resource_count: number;
  prompt_count: number;
}

/** A count phrase that omits what the server did not advertise. */
export function capabilitySummary(counts: CapabilityCounts): string {
  const parts: string[] = [];
  if (counts.tool_count) {
    parts.push(`${counts.tool_count} ${counts.tool_count === 1 ? 'tool' : 'tools'}`);
  }
  if (counts.resource_count) {
    parts.push(
      `${counts.resource_count} ${counts.resource_count === 1 ? 'resource' : 'resources'}`,
    );
  }
  if (counts.prompt_count) {
    parts.push(`${counts.prompt_count} ${counts.prompt_count === 1 ? 'prompt' : 'prompts'}`);
  }
  return parts.length ? parts.join(' · ') : 'Nothing discovered yet';
}

/** The browser return from an MCP authorization handled by the control plane. */
export interface McpAuthorizationResult {
  ok: boolean;
  connectionId: string | null;
  message: string | null;
}

function StatusPill({ status }: { status: McpConnectionStatus }) {
  const pill = STATUS_PILL[status];
  return (
    <span className={`rounded-full px-2.5 py-1 text-xs font-medium ${pill.className}`}>
      {pill.label}
    </span>
  );
}

/**
 * The MCP connections a user has added, with the actions that change them.
 *
 * It owns its own loading state rather than taking one from the page: the
 * Integrations view also tracks source-control providers, and a single error
 * banner for both would report a failing GitHub call as a failing MCP server.
 */
export function McpConnectionsSection({
  authorizationResult,
}: {
  authorizationResult?: McpAuthorizationResult;
}) {
  const [data, setData] = useState<McpConnectionsApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  /** The connection whose authorize call asked for a client id. */
  const [clientIdFor, setClientIdFor] = useState<{ id: number; message: string } | null>(null);
  const [clientId, setClientId] = useState('');
  const authorizationNoticeChecked = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await listMcpConnections();
      setData(response);
      if (authorizationResult && !authorizationNoticeChecked.current) {
        authorizationNoticeChecked.current = true;
        if (authorizationResult.ok) {
          const connection = response.connections.find(
            (item) => String(item.id) === authorizationResult.connectionId && item.has_credential,
          );
          if (connection) {
            setNotice(`${connection.label} authorized. Run a test to discover what it offers.`);
          }
        } else {
          setError(
            authorizationResult.message
              ? `MCP authorization did not complete: ${authorizationResult.message}`
              : 'MCP authorization did not complete. Try authorizing the connection again.',
          );
        }
      }
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not load MCP connections.');
    } finally {
      setLoading(false);
    }
  }, [authorizationResult]);

  useEffect(() => {
    void load();
  }, [load]);

  async function test(connection: McpConnectionApi) {
    setBusyId(connection.id);
    setError(null);
    setNotice(null);
    try {
      const result = await testMcpConnection(connection.id);
      const serverName = result.server_name ?? 'an unnamed server';
      const serverVersion = result.server_version ? ` ${result.server_version}` : '';
      setNotice(
        result.ok
          ? `${connection.label} answered as ${serverName}${serverVersion}. ${capabilitySummary(result)}`
          : `${connection.label} failed: ${result.message}`,
      );
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not test the connection.');
    } finally {
      setBusyId(null);
    }
  }

  async function authorize(connection: McpConnectionApi, suppliedClientId?: string) {
    setBusyId(connection.id);
    setError(null);
    setNotice(null);
    try {
      const result = await authorizeMcpConnection(connection.id, suppliedClientId);
      if (result.authorization_url) {
        // Same tab: the provider redirects back to this deployment, and a
        // popup would leave the original page showing a stale connection list.
        window.location.assign(result.authorization_url);
        return;
      }
      if (result.needs_client_id) {
        setClientIdFor({ id: connection.id, message: result.message });
        setClientId(suppliedClientId ?? '');
        return;
      }
      setClientIdFor(null);
      setNotice(result.message || `${connection.label} authorized.`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not start authorization.');
    } finally {
      setBusyId(null);
    }
  }

  async function disconnect(connection: McpConnectionApi) {
    if (
      !globalThis.confirm(
        `Disconnect ${connection.label}? Agents can no longer use it, and its stored credential is cleared.`,
      )
    ) {
      return;
    }
    setBusyId(connection.id);
    setError(null);
    setNotice(null);
    try {
      await disconnectMcpConnection(connection.id);
      setNotice(`${connection.label} disconnected.`);
      if (clientIdFor?.id === connection.id) setClientIdFor(null);
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not disconnect the connection.');
    } finally {
      setBusyId(null);
    }
  }

  const connections = data?.connections ?? [];

  return (
    <div className="mt-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-gray-500 dark:text-gray-400">
          Connect MCP servers with your own credentials to give your agents more tools.
        </p>
        <button
          type="button"
          onClick={() => setAdding(true)}
          className="inline-flex items-center gap-1.5 rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
        >
          Add connection
        </button>
      </div>

      {notice ? (
        <div
          role="status"
          className="mt-4 rounded-xl border border-emerald-200 dark:border-emerald-500/25 bg-emerald-50 dark:bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800 dark:text-emerald-300"
        >
          {notice}
        </div>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="mt-4 flex items-center justify-between gap-4 rounded-xl border border-red-200 dark:border-red-500/25 bg-red-50 dark:bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-400"
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="shrink-0 font-medium hover:underline"
          >
            Try again
          </button>
        </div>
      ) : null}

      <div className="mt-4 rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 shadow-sm">
        {loading && !data ? (
          <p role="status" className="py-5 text-sm text-gray-500 dark:text-gray-400">
            Loading MCP connections…
          </p>
        ) : null}
        {!loading && connections.length === 0 ? (
          <p className="py-5 text-sm text-gray-500 dark:text-gray-400">
            No MCP servers connected yet. Add one from a template, by pasting a configuration, or by
            filling in the address yourself.
          </p>
        ) : null}
        {connections.map((connection) => (
          <div
            key={connection.id}
            className="flex flex-col gap-3 border-t border-gray-100 dark:border-white/5 py-5 first:border-t-0 sm:flex-row sm:items-start"
          >
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="font-medium text-gray-900 dark:text-gray-50">{connection.label}</h3>
                <StatusPill status={connection.status} />
                {connection.template ? (
                  <span className="rounded-full bg-gray-100 dark:bg-white/5 px-2 py-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                    {connection.template}
                  </span>
                ) : null}
              </div>
              <p className="mt-1 break-all text-sm text-gray-500 dark:text-gray-400">
                {TRANSPORT_LABEL[connection.transport]} · {LOCATION_LABEL[connection.location]} ·{' '}
                {targetSummary(connection)}
              </p>
              <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                {connection.server_name
                  ? `${connection.server_name} ${connection.server_version ?? ''}`.trim()
                  : 'Server not identified'}
                {connection.protocol_version ? ` · protocol ${connection.protocol_version}` : ''}
                {' · '}
                {capabilitySummary(connection)}
              </p>
              {connection.has_credential ? (
                <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
                  Credential stored
                  {connection.secret_hint ? ` (${connection.secret_hint})` : ''}
                  {connection.account_label ? ` for ${connection.account_label}` : ''}
                  {connection.expires_at ? ` · expires ${connection.expires_at}` : ''}
                </p>
              ) : connection.auth_kind === 'oauth' ? (
                <p className="mt-1 text-sm text-amber-700 dark:text-amber-400">
                  Not authorized yet.
                </p>
              ) : null}
              {connection.error_detail ? (
                <p className="mt-1 text-sm text-red-600 dark:text-red-400">
                  {connection.error_kind !== 'none' ? `${connection.error_kind}: ` : ''}
                  {connection.error_detail}
                </p>
              ) : null}

              {clientIdFor?.id === connection.id ? (
                <form
                  className="mt-3 rounded-xl border border-gray-200 dark:border-white/10 p-3"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void authorize(connection, clientId.trim());
                  }}
                >
                  <p className="text-sm text-gray-700 dark:text-gray-300">{clientIdFor.message}</p>
                  <label
                    className="mt-2 block text-xs font-medium text-gray-600 dark:text-gray-400"
                    htmlFor={`mcp-client-id-${connection.id}`}
                  >
                    OAuth client id
                  </label>
                  <div className="mt-1 flex gap-2">
                    <input
                      id={`mcp-client-id-${connection.id}`}
                      value={clientId}
                      onChange={(event) => setClientId(event.target.value)}
                      className="min-w-0 flex-1 rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2.5 py-1.5 text-sm text-gray-900 dark:text-gray-100"
                    />
                    <button
                      type="submit"
                      disabled={!clientId.trim() || busyId === connection.id}
                      className="rounded-lg border border-gray-200 dark:border-white/10 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 disabled:cursor-not-allowed disabled:opacity-60"
                    >
                      Continue
                    </button>
                  </div>
                  <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                    The client id is registered with the server by you. The platform still performs
                    discovery, PKCE and the redirect.
                  </p>
                </form>
              ) : null}
            </div>

            <div className="flex shrink-0 flex-wrap gap-2 sm:justify-end">
              <button
                type="button"
                disabled={busyId === connection.id}
                onClick={() => void test(connection)}
                className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {busyId === connection.id ? 'Working…' : 'Test'}
              </button>
              {connection.auth_kind === 'oauth' ? (
                <button
                  type="button"
                  disabled={busyId === connection.id}
                  onClick={() => void authorize(connection)}
                  className="rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 shadow-sm hover:bg-gray-50 dark:hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {connection.has_credential ? 'Reauthorize' : 'Authorize'}
                </button>
              ) : null}
              <button
                type="button"
                disabled={busyId === connection.id}
                onClick={() => void disconnect(connection)}
                className="rounded-lg border border-gray-200 dark:border-white/10 px-3 py-1.5 text-sm font-medium text-red-600 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-60"
              >
                Disconnect
              </button>
            </div>
          </div>
        ))}
      </div>

      {adding ? (
        <McpAddDialog
          templates={data?.templates ?? []}
          onClose={() => setAdding(false)}
          onAdded={(label) => {
            setAdding(false);
            setNotice(`${label} added. Run a test to discover what it offers.`);
            void load();
          }}
        />
      ) : null}
    </div>
  );
}
