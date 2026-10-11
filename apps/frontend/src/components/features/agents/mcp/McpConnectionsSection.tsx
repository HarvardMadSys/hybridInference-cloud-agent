'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { McpAddDialog, type McpAddMode } from './McpAddDialog';
import {
  authorizeMcpConnection,
  disconnectMcpConnection,
  listMcpConnections,
  testMcpConnection,
  validateMcpAuthorizationUrl,
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

const BUTTON = 'ui-button ui-button-secondary min-h-11';

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
  const [clientIdFor, setClientIdFor] = useState<{
    id: number;
    message: string;
    redirectUri?: string | null;
  } | null>(null);
  const [clientId, setClientId] = useState('');
  const [clientSecret, setClientSecret] = useState('');
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

  async function authorize(
    connection: McpConnectionApi,
    suppliedClientId?: string,
    suppliedClientSecret?: string,
  ) {
    setBusy(new Set([connection.id]));
    setError(null);
    setNotice(null);
    try {
      // The user explicitly chose Sign in after discovering that this URL
      // needs OAuth. Do not change static-token connections on their behalf.
      if (connection.auth_kind === 'none') await enableMcpOAuth(connection);
      const result = suppliedClientSecret
        ? await authorizeMcpConnection(connection.id, suppliedClientId, suppliedClientSecret)
        : await authorizeMcpConnection(connection.id, suppliedClientId);
      if (result.authorization_url) {
        setClientSecret('');
        window.location.assign(validateMcpAuthorizationUrl(result.authorization_url));
        return;
      }
      if (result.needs_client_id) {
        setClientIdFor({
          id: connection.id,
          message: result.message,
          redirectUri: result.redirect_uri,
        });
        setClientId(suppliedClientId ?? '');
        setClientSecret('');
      } else {
        setClientIdFor(null);
        setClientSecret('');
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
      connections: [
        ...connections,
        ...(current?.connections ?? []).filter(
          (existing) => !connections.some((connection) => connection.id === existing.id),
        ),
      ],
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
      if (clientIdFor?.id === connection.id) {
        setClientIdFor(null);
        setClientSecret('');
      }
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
    <section aria-labelledby="mcp-servers-heading" lang="en">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1 basis-64">
          <h2 id="mcp-servers-heading" className="text-base font-semibold text-foreground">
            MCP servers
          </h2>
          <p className="mt-1 text-sm leading-6 text-muted">
            Connect once. Enabled servers are available automatically in your conversations.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
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
            className="ui-button ui-button-primary min-h-11"
          >
            Add server
          </button>
        </div>
      </div>
      {notice ? (
        <p
          role="status"
          className="mt-4 break-words rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800 dark:border-emerald-500/25 dark:bg-emerald-500/10 dark:text-emerald-300"
        >
          {notice}
        </p>
      ) : null}
      {error ? (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
        >
          <span className="min-w-0 break-words">{error}</span>
          <button
            type="button"
            disabled={busy.size > 0}
            onClick={() => {
              setError(null);
              void load().catch((cause: unknown) =>
                setError(cause instanceof Error ? cause.message : 'Could not load your servers.'),
              );
            }}
            className="ui-button ui-focus-ring min-h-11 hover:bg-red-100 dark:hover:bg-red-500/10"
          >
            Refresh
          </button>
        </div>
      ) : null}
      <div className="mt-5 overflow-hidden rounded-xl border border-border bg-surface-muted/40">
        {loading && !data ? (
          <p role="status" className="p-5 text-sm text-muted">
            Loading servers…
          </p>
        ) : null}
        {!loading && !connections.length ? (
          <div className="px-5 py-10 text-center">
            <span
              aria-hidden="true"
              className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-border bg-surface text-accent"
            >
              <svg
                className="h-6 w-6"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.5"
              >
                <rect x="3" y="3" width="18" height="7" rx="2" />
                <rect x="3" y="14" width="18" height="7" rx="2" />
                <path d="M7 6.5h.01M7 17.5h.01M11 6.5h6M11 17.5h6" strokeLinecap="round" />
              </svg>
            </span>
            <p className="text-sm font-semibold text-foreground">
              Your tools, in every conversation
            </p>
            <p className="mx-auto mt-2 max-w-sm text-sm leading-6 text-muted">
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
            <div key={connection.id} className="border-t border-border p-4 first:border-t-0 sm:p-5">
              <div className="flex flex-col items-start justify-between gap-3 lg:flex-row">
                <div className="min-w-0 max-w-full flex-1">
                  <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
                    <span
                      aria-hidden="true"
                      className={`h-2 w-2 shrink-0 rounded-full ${working ? 'animate-pulse bg-amber-400' : active ? 'bg-emerald-500' : signIn ? 'bg-amber-500' : 'bg-gray-400 dark:bg-gray-500'}`}
                    />
                    <h3 className="min-w-0 break-words text-sm font-semibold text-foreground">
                      {connection.label}
                    </h3>
                    <span
                      className={`rounded-full px-2.5 py-1 text-xs font-medium ${active ? 'bg-emerald-50 text-status-success dark:bg-emerald-500/10' : signIn || working ? 'bg-amber-50 text-status-warning dark:bg-amber-500/10' : 'bg-surface-hover text-muted'}`}
                    >
                      {state}
                    </span>
                  </div>
                  <p className="mt-2 break-all text-xs leading-5 text-muted">
                    {active
                      ? capabilitySummary(connection)
                      : (connection.url ?? connection.argv.join(' '))}
                  </p>
                </div>
                <div className="flex shrink-0 flex-wrap items-center gap-2">
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
                          ? connection.template === 'github'
                            ? 'Sign in with GitHub'
                            : 'Sign in'
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
                      className="ui-focus-ring relative h-11 w-12 shrink-0 rounded-lg disabled:cursor-not-allowed disabled:opacity-50"
                    >
                      <span
                        aria-hidden="true"
                        className={`absolute left-0.5 top-1/2 h-6 w-11 -translate-y-1/2 rounded-full transition-colors motion-reduce:transition-none ${enabled ? 'bg-accent' : 'bg-gray-200 dark:bg-white/20'}`}
                      >
                        <span
                          className={`absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform motion-reduce:transition-none dark:bg-white ${enabled ? 'translate-x-5' : ''}`}
                        />
                      </span>
                    </button>
                  ) : null}
                </div>
              </div>
              {connection.error_detail && !working ? (
                <p className="mt-2 break-words text-xs leading-5 text-status-error">
                  {connection.error_detail}
                </p>
              ) : null}
              {clientIdFor?.id === connection.id ? (
                <form
                  className="mt-4 rounded-xl border border-border bg-surface p-4"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void authorize(connection, clientId.trim(), clientSecret || undefined);
                  }}
                >
                  <p className="break-words text-sm leading-6 text-muted">{clientIdFor.message}</p>
                  {clientIdFor.redirectUri ? (
                    <label className="mt-3 block text-sm font-medium text-foreground">
                      Register this OAuth redirect URI with your provider
                      <input
                        aria-label="OAuth redirect URI"
                        readOnly
                        value={clientIdFor.redirectUri}
                        className="ui-input mt-2 min-h-11 w-full font-mono text-xs"
                      />
                    </label>
                  ) : null}
                  <label
                    className="mt-3 block text-sm font-medium text-foreground"
                    htmlFor={`mcp-client-id-${connection.id}`}
                  >
                    OAuth client ID
                  </label>
                  <div className="mt-2">
                    <input
                      id={`mcp-client-id-${connection.id}`}
                      value={clientId}
                      autoComplete="off"
                      disabled={working}
                      onChange={(event) => setClientId(event.target.value)}
                      className="ui-input min-h-11 w-full"
                    />
                    <label className="mt-3 block text-sm font-medium text-foreground">
                      OAuth client secret
                      <input
                        type="password"
                        autoComplete="off"
                        value={clientSecret}
                        onChange={(event) => setClientSecret(event.target.value)}
                        disabled={working}
                        className="ui-input mt-2 min-h-11 w-full"
                      />
                    </label>
                    <p className="mt-2 text-xs text-muted">
                      Required for Slack and Google Drive. Leave blank if your provider uses a
                      public client.
                    </p>
                    <button
                      type="submit"
                      disabled={!clientId.trim() || busy.size > 0}
                      className={`mt-3 ${BUTTON}`}
                    >
                      Continue
                    </button>
                  </div>
                </form>
              ) : null}
              <details className="group mt-2 text-xs text-muted">
                <summary className="ui-focus-ring flex min-h-11 w-fit cursor-pointer items-center gap-2 rounded-lg px-1 font-medium hover:text-foreground">
                  <svg
                    aria-hidden="true"
                    className="h-3.5 w-3.5 transition-transform group-open:rotate-90 motion-reduce:transition-none"
                    viewBox="0 0 20 20"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.5"
                  >
                    <path d="m7 5 5 5-5 5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  Server details
                </summary>
                <dl className="mt-1 grid gap-4 rounded-lg border border-border bg-surface p-4 sm:grid-cols-2">
                  <div className="min-w-0 sm:col-span-2">
                    <dt className="font-medium">
                      {connection.transport === 'stdio' ? 'Command' : 'URL'}
                    </dt>
                    <dd className="mt-1 break-all font-mono">
                      {connection.url ?? connection.argv.join(' ')}
                    </dd>
                  </div>
                  <div>
                    <dt className="font-medium">Connection</dt>
                    <dd className="mt-1 break-words">
                      {connection.transport} · {connection.location}
                    </dd>
                  </div>
                  {connection.server_name ? (
                    <div>
                      <dt className="font-medium">Server</dt>
                      <dd className="mt-1 break-words">
                        {connection.server_name} {connection.server_version}
                      </dd>
                    </div>
                  ) : null}
                  {connection.protocol_version ? (
                    <div>
                      <dt className="font-medium">Protocol</dt>
                      <dd className="mt-1 break-words">{connection.protocol_version}</dd>
                    </div>
                  ) : null}
                  {connection.has_credential ? (
                    <div>
                      <dt className="font-medium">Credential stored</dt>
                      <dd className="mt-1 break-words">
                        {connection.account_label ?? connection.secret_hint}
                      </dd>
                    </div>
                  ) : null}
                </dl>
                {connection.status !== 'disabled' ? (
                  <div className="mt-3 flex flex-wrap items-center gap-3">
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
                      className="ui-button min-h-11 text-status-error hover:bg-red-50 disabled:opacity-50 dark:hover:bg-red-500/10"
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
    </section>
  );
}
