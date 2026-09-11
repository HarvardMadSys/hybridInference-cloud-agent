'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  decideMcpInvocation,
  getMcpConnectionCapabilities,
  getMcpThreadBindings,
  listMcpConnections,
  listMcpInvocations,
  replaceMcpThreadBindings,
  type McpBindingApi,
  type McpBindingRequest,
  type McpCapabilityApi,
  type McpCapabilityKind,
  type McpConnectionApi,
  type McpInvocationApi,
  type McpPolicy,
} from '@/lib/api/mcp';

/** The three answers a capability can carry, in escalating order. */
const POLICIES: Array<{ value: McpPolicy; label: string }> = [
  { value: 'deny', label: 'Deny' },
  { value: 'confirm', label: 'Confirm' },
  { value: 'allow', label: 'Allow' },
];

/** How each capability surface is titled, in the order the panel shows them. */
const CAPABILITY_GROUPS: Array<{ kind: McpCapabilityKind; label: string }> = [
  { kind: 'tool', label: 'Tools' },
  { kind: 'prompt', label: 'Prompts' },
  { kind: 'resource', label: 'Resources' },
  { kind: 'resource_template', label: 'Resource templates' },
];

/** One connection's edited selection, held locally until it is saved. */
interface Selection {
  tools: Record<string, McpPolicy>;
  resources: Record<string, McpPolicy>;
  prompts: Record<string, McpPolicy>;
}

/** Which of the three selection maps a capability kind is stored in. */
function selectionKey(kind: McpCapabilityKind): keyof Selection {
  if (kind === 'tool') return 'tools';
  if (kind === 'prompt') return 'prompts';
  // Resource templates share the resource map: the backend's binding has one
  // dictionary per surface, and a template is addressed like a resource.
  return 'resources';
}

/**
 * What a capability means when the user has said nothing about it.
 *
 * A binding the platform mapped on the user's behalf defaults to confirm, so a
 * pre-existing session keeps working while its owner is asked once. Everything
 * else is denied until enabled, which is the whole point of a per-capability
 * selection: a tool the server adds later must not be reachable by default.
 */
function defaultPolicy(binding: McpBindingApi): McpPolicy {
  return binding.selection_pending ? 'confirm' : 'deny';
}

/** The stored selection, typed for the panel's local editing state. */
function selectionOf(binding: McpBindingApi): Selection {
  return { tools: binding.tools, resources: binding.resources, prompts: binding.prompts };
}

/** Policies for every capability currently discovered on a binding. */
function draftSelection(binding: McpBindingApi): Selection {
  const stored = selectionOf(binding);
  const fallback = defaultPolicy(binding);
  const next: Selection = { tools: {}, resources: {}, prompts: {} };
  for (const capability of binding.capabilities) {
    const key = selectionKey(capability.kind);
    next[key][capability.name] = stored[key][capability.name] ?? fallback;
  }
  return next;
}

/** The same draft, keyed by connection id, for the panel's editing state. */
function draftSelections(bindings: McpBindingApi[]): Record<number, Selection> {
  return Object.fromEntries(
    bindings.map((binding) => [binding.connection_id, draftSelection(binding)]),
  );
}

/** A short, human phrase for a held call's arguments. */
function formatArguments(invocation: McpInvocationApi): string {
  if (!invocation.arguments) return 'The arguments are not readable on this deployment.';
  return JSON.stringify(invocation.arguments, null, 2);
}

function PolicyControl({
  name,
  value,
  onChange,
}: {
  name: string;
  value: McpPolicy;
  onChange: (policy: McpPolicy) => void;
}) {
  return (
    <div role="radiogroup" aria-label={`Policy for ${name}`} className="flex shrink-0 gap-1">
      {POLICIES.map((policy) => (
        <button
          key={policy.value}
          type="button"
          role="radio"
          aria-checked={value === policy.value}
          onClick={() => onChange(policy.value)}
          className={`rounded-md px-2 py-1 text-xs ${
            value === policy.value
              ? 'bg-gray-900 dark:bg-gray-100 font-medium text-white dark:text-gray-900'
              : 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-white/15'
          }`}
        >
          {policy.label}
        </button>
      ))}
    </div>
  );
}

function CapabilityList({
  capabilities,
  selection,
  onPolicy,
}: {
  capabilities: McpCapabilityApi[];
  selection: Selection;
  onPolicy: (kind: McpCapabilityKind, name: string, policy: McpPolicy) => void;
}) {
  return (
    <>
      {CAPABILITY_GROUPS.map((group) => {
        const items = capabilities.filter((capability) => capability.kind === group.kind);
        if (items.length === 0) return null;
        const key = selectionKey(group.kind);
        return (
          <div key={group.kind} className="mt-3">
            <h4 className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500">
              {group.label}
            </h4>
            {items.map((capability) => (
              <div
                key={capability.name}
                className="mt-2 flex flex-col gap-2 border-t border-gray-100 dark:border-white/5 pt-2 first:border-t-0 sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <p className="truncate font-mono text-xs text-gray-800 dark:text-gray-200">
                    {capability.name}
                  </p>
                  {capability.description ? (
                    <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                      {capability.description}
                    </p>
                  ) : null}
                </div>
                <PolicyControl
                  name={capability.name}
                  value={selection[key][capability.name] ?? 'deny'}
                  onChange={(policy) => onPolicy(capability.kind, capability.name, policy)}
                />
              </div>
            ))}
          </div>
        );
      })}
    </>
  );
}

/**
 * What one conversation may use, and the calls waiting on its owner.
 *
 * Both live in one panel because they are the same decision seen twice: the
 * selection says what may run unattended, and the approval card is the case
 * the selection reserved for a person. Splitting them would let a user approve
 * a call from a screen that does not show the rule it was held under.
 *
 * Attaching a connection is part of the selection rather than of the
 * Integrations page: adding a server to the account says what is *available*,
 * and letting a conversation call it is the separate decision this panel
 * takes. Only the PUT writes a binding, so a connection the conversation does
 * not already have is attached by adding its capabilities to the same draft
 * and saving once.
 */
export function McpSessionPanel({
  jobId,
  threadId,
  active,
}: {
  jobId: string;
  threadId: string;
  active: boolean;
}) {
  const [bindings, setBindings] = useState<McpBindingApi[]>([]);
  const [connections, setConnections] = useState<McpConnectionApi[]>([]);
  const [selections, setSelections] = useState<Record<number, Selection>>({});
  const [pending, setPending] = useState<McpInvocationApi[]>([]);
  const [decided, setDecided] = useState<McpInvocationApi[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [attaching, setAttaching] = useState<number | null>(null);
  const [decidingId, setDecidingId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [bindingResponse, invocationResponse, connectionResponse] = await Promise.all([
        getMcpThreadBindings(threadId),
        listMcpInvocations({ jobId, pendingOnly: true }),
        listMcpConnections(),
      ]);
      setBindings(bindingResponse.bindings);
      setSelections(draftSelections(bindingResponse.bindings));
      setPending(invocationResponse.invocations);
      setConnections(connectionResponse.connections);
      setLoaded(true);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not load MCP settings.');
    } finally {
      setLoading(false);
    }
  }, [jobId, threadId]);

  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  // A held call has a deadline, and the agent is blocked on the answer: a
  // panel that only refreshes when the user clicks would show a stale queue
  // for exactly as long as the call is waiting.
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      listMcpInvocations({ jobId, pendingOnly: true })
        .then((response) => setPending(response.invocations))
        .catch(() => {
          // A background refresh that fails leaves the last good queue on
          // screen; the next one reports the failure through the main load.
        });
    }, 10000);
    return () => clearInterval(timer);
  }, [active, jobId]);

  function setPolicy(
    connectionId: number,
    kind: McpCapabilityKind,
    name: string,
    policy: McpPolicy,
  ) {
    setSelections((current) => {
      const selection = current[connectionId];
      if (!selection) return current;
      const key = selectionKey(kind);
      const next: Selection = {
        ...selection,
        [key]: { ...selection[key], [name]: policy },
      };
      return { ...current, [connectionId]: next };
    });
  }

  /**
   * Add a connection to the conversation's draft.
   *
   * Its capabilities come from the stored snapshot rather than from a binding,
   * because a connection the conversation has never had has no binding yet.
   * The draft defaults every capability to deny: attaching a server is not the
   * same as letting this session call it.
   */
  async function attach(connection: McpConnectionApi) {
    setAttaching(connection.id);
    setError(null);
    setNotice(null);
    try {
      const capabilities = await getMcpConnectionCapabilities(connection.id);
      const draft: McpBindingApi = {
        connection_id: connection.id,
        connection_label: connection.label,
        alias: connection.template ?? `mcp${connection.id}`,
        enabled: false,
        selection_pending: false,
        available: connection.status !== 'disabled' && connection.status !== 'error',
        error: connection.error_detail,
        capabilities,
        tools: {},
        resources: {},
        prompts: {},
        config_revision: connection.config_revision,
        authorization_epoch: connection.authorization_epoch,
        capability_revision: connection.capability_revision,
        policy_revision: 1,
      };
      setBindings((current) => [...current, draft]);
      setSelections((current) => ({ ...current, [connection.id]: draftSelection(draft) }));
      setNotice(`${connection.label} added. Choose what it may do, then save the selection.`);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not read that connection.');
    } finally {
      setAttaching(null);
    }
  }

  async function save() {
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: McpBindingRequest[] = bindings.map((binding) => {
        const selection = selections[binding.connection_id] ?? draftSelection(binding);
        const enabled = Object.values(selection).some((rules) =>
          Object.values(rules).some((policy) => policy !== 'deny'),
        );
        return {
          connection_id: binding.connection_id,
          alias: binding.alias,
          // The backend disables a binding whose selection is empty, so the
          // checkbox and the rules can never disagree.
          enabled,
          tools: selection.tools,
          resources: selection.resources,
          prompts: selection.prompts,
        };
      });
      const response = await replaceMcpThreadBindings(threadId, payload);
      setBindings(response.bindings);
      setSelections(draftSelections(response.bindings));
      setNotice('Selection saved. It applies to the next tool call in this conversation.');
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not save the selection.');
    } finally {
      setSaving(false);
    }
  }

  async function decide(invocation: McpInvocationApi, approve: boolean) {
    setDecidingId(invocation.id);
    setError(null);
    try {
      const result = await decideMcpInvocation(invocation.id, approve);
      setPending((current) => current.filter((item) => item.id !== invocation.id));
      setDecided((current) => [result, ...current.filter((item) => item.id !== result.id)]);
    } catch (cause: unknown) {
      // A lost race (the call expired or another tab decided it) is reported
      // where the card was, and the next refresh shows what actually happened.
      setError(cause instanceof Error ? cause.message : 'Could not record the decision.');
    } finally {
      setDecidingId(null);
    }
  }

  const totalCapabilities = useMemo(
    () => bindings.reduce((sum, binding) => sum + binding.capabilities.length, 0),
    [bindings],
  );
  /** Connections the user has that this conversation is not bound to yet. */
  const attachable = useMemo(
    () =>
      connections.filter((item) => !bindings.some((binding) => binding.connection_id === item.id)),
    [bindings, connections],
  );

  if (!active && !loaded) {
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">Open this panel to load tools.</p>
    );
  }

  return (
    <div>
      {pending.length || decided.length ? (
        <section aria-labelledby="mcp-approvals">
          <h3
            id="mcp-approvals"
            className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500"
          >
            Waiting for your approval
          </h3>
          {pending.map((invocation) => (
            <div
              key={invocation.id}
              className="mt-3 rounded-xl border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 p-3"
            >
              <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
                {invocation.connection_label ?? `Connection ${invocation.connection_id}`} wants to
                call {invocation.capability_name}
              </p>
              {invocation.expires_at ? (
                <p className="mt-0.5 text-xs text-amber-800 dark:text-amber-300">
                  Expires {invocation.expires_at}
                </p>
              ) : null}
              <pre className="mt-2 max-h-48 overflow-auto rounded-lg bg-white/70 dark:bg-black/20 p-2 font-mono text-xs text-gray-800 dark:text-gray-200">
                {formatArguments(invocation)}
              </pre>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={decidingId === invocation.id}
                  onClick={() => void decide(invocation, true)}
                  className="rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={decidingId === invocation.id}
                  onClick={() => void decide(invocation, false)}
                  className="rounded-lg border border-gray-300 dark:border-white/20 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  Deny
                </button>
              </div>
            </div>
          ))}
          {decided.map((invocation) => (
            <div
              key={invocation.id}
              className="mt-3 rounded-xl border border-gray-200 dark:border-white/10 p-3"
            >
              <p className="text-sm text-gray-800 dark:text-gray-200">
                {invocation.capability_name} · {invocation.status}
              </p>
              {invocation.guidance ? (
                <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {invocation.guidance}
                </p>
              ) : null}
            </div>
          ))}
        </section>
      ) : null}

      <section aria-labelledby="mcp-selection" className="mt-6">
        <div className="flex items-center justify-between gap-3">
          <h3
            id="mcp-selection"
            className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500"
          >
            Connections for this conversation
          </h3>
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="text-xs text-gray-500 dark:text-gray-400 hover:underline disabled:opacity-60"
          >
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>

        {notice ? (
          <p role="status" className="mt-3 text-sm text-emerald-700 dark:text-emerald-400">
            {notice}
          </p>
        ) : null}
        {error ? (
          <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        ) : null}

        {bindings.length === 0 ? (
          <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">
            {attachable.length
              ? 'This conversation has no MCP connections yet. Attach one below.'
              : 'This conversation has no MCP connections. Add one on the Integrations page, then attach it here.'}
          </p>
        ) : null}

        {bindings.map((binding) => {
          const selection = selections[binding.connection_id] ?? draftSelection(binding);
          return (
            <div
              key={binding.connection_id}
              className="mt-3 rounded-xl border border-gray-200 dark:border-white/10 p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <p className="font-medium text-gray-900 dark:text-gray-50">
                    {binding.connection_label}
                  </p>
                  <p className="text-xs text-gray-500 dark:text-gray-400">
                    Alias {binding.alias} ·{' '}
                    {binding.enabled ? 'attached' : 'not attached to this conversation'}
                  </p>
                </div>
                {binding.selection_pending ? (
                  <span className="rounded-full bg-amber-100 dark:bg-amber-500/15 px-2.5 py-1 text-xs font-medium text-amber-800 dark:text-amber-300">
                    Choose what this session may use
                  </span>
                ) : null}
              </div>

              {!binding.available ? (
                <p className="mt-2 rounded-lg bg-red-50 dark:bg-red-500/10 px-3 py-2 text-sm text-red-700 dark:text-red-400">
                  {binding.error ?? 'This connection is not usable right now.'}
                </p>
              ) : null}

              {binding.capabilities.length === 0 ? (
                <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
                  Nothing discovered yet. Run a test on the Integrations page to read what this
                  server offers.
                </p>
              ) : (
                <CapabilityList
                  capabilities={binding.capabilities}
                  selection={selection}
                  onPolicy={(kind, name, policy) =>
                    setPolicy(binding.connection_id, kind, name, policy)
                  }
                />
              )}
            </div>
          );
        })}

        {attachable.length ? (
          <div className="mt-4 border-t border-gray-100 dark:border-white/5 pt-3">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              Attach one of your other connections to this conversation. Everything it offers starts
              denied.
            </p>
            <div className="mt-2 flex flex-wrap gap-2">
              {attachable.map((connection) => (
                <button
                  key={connection.id}
                  type="button"
                  disabled={attaching === connection.id}
                  onClick={() => void attach(connection)}
                  className="rounded-lg border border-gray-200 dark:border-white/10 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-50 dark:hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {attaching === connection.id ? 'Reading…' : `Attach ${connection.label}`}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {bindings.length && totalCapabilities ? (
          <button
            type="button"
            disabled={saving}
            onClick={() => void save()}
            className="mt-4 rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {saving ? 'Saving…' : 'Save selection'}
          </button>
        ) : null}
      </section>
    </div>
  );
}
