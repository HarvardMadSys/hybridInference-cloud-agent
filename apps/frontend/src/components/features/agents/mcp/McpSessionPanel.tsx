'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  decideMcpInvocation,
  getMcpThreadBindings,
  listMcpInvocations,
  updateMcpThreadBinding,
  type McpBindingApi,
  type McpBindingRequest,
  type McpCapabilityApi,
  type McpCapabilityKind,
  type McpInvocationApi,
  type McpInvocationStatus,
  type McpInvocationsApi,
  type McpPolicy,
} from '@/lib/api/mcp';

import { McpInvocationCard, connectionName, invocationAction } from './McpInvocationCard';

import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from '../agentPush';

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

/** New servers ask before each call; existing explicit selections retain their default. */
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

/** Keep connection prefixes distinct without renaming any existing unique prefix. */
function nextBindingAlias(preferred: string, connectionId: number, reserved: Set<string>): string {
  const base = /^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$/.test(preferred)
    ? preferred
    : `mcp${connectionId}`;
  let alias = base;
  let counter = 1;
  while (reserved.has(alias)) {
    const suffix = `-${connectionId}${counter > 1 ? `-${counter}` : ''}`;
    alias = base.slice(0, 40 - suffix.length) + suffix;
    counter += 1;
  }
  return alias;
}

/** Existing ambiguous selections can be repaired by saving from this panel. */
function uniqueBindingAliases(bindings: McpBindingApi[]): McpBindingApi[] {
  const reserved = new Set(bindings.map((binding) => binding.alias));
  const seen = new Set<string>();
  return bindings.map((binding) => {
    if (!seen.has(binding.alias)) {
      seen.add(binding.alias);
      return binding;
    }
    const alias = nextBindingAlias(binding.alias, binding.connection_id, reserved);
    reserved.add(alias);
    return { ...binding, alias };
  });
}

const INVOCATION_PROGRESS: Record<McpInvocationStatus, number> = {
  pending: 0,
  approved: 1,
  dispatching: 2,
  denied: 3,
  expired: 3,
  completed: 3,
  failed: 3,
  outcome_unknown: 3,
};

/** A poll started before a decision must not put its approval button back. */
function mergeInvocations(
  current: McpInvocationApi[],
  incoming: McpInvocationApi[],
  snapshot = false,
  historyLimit = 50,
) {
  const calls = new Map(
    current.filter((call) => !snapshot || call.status !== 'pending').map((call) => [call.id, call]),
  );
  for (const call of incoming) {
    const previous = calls.get(call.id);
    if (!previous || INVOCATION_PROGRESS[call.status] >= INVOCATION_PROGRESS[previous.status]) {
      calls.set(call.id, call);
    }
  }
  const sorted = [...calls.values()].sort((a, b) => b.id - a.id);
  return [
    ...sorted.filter((call) => call.status === 'pending'),
    ...sorted.filter((call) => call.status !== 'pending').slice(0, historyLimit),
  ];
}

async function readInvocationPages(
  jobId: string,
  pendingOnly: boolean,
  pages = Infinity,
): Promise<McpInvocationsApi> {
  const invocations: McpInvocationApi[] = [];
  let beforeId: number | undefined;
  let response: McpInvocationsApi = { invocations: [] };
  for (let page = 0; page < pages; page += 1) {
    response = await listMcpInvocations({
      jobId,
      ...(pendingOnly ? { pendingOnly: true } : {}),
      ...(beforeId === undefined ? {} : { beforeId }),
    });
    invocations.push(...response.invocations);
    const cursor = response.next_before_id;
    if (!response.has_more || cursor == null || (beforeId !== undefined && cursor >= beforeId)) {
      break;
    }
    beforeId = cursor;
  }
  return { ...response, invocations };
}

async function readInvocations(jobId: string, historyPages = 1) {
  // Pending calls have their own complete scan: recent history must never
  // hide an old request that still blocks the agent.
  const [recent, pending] = await Promise.all([
    readInvocationPages(jobId, false, historyPages),
    readInvocationPages(jobId, true),
  ]);
  return {
    invocations: [
      ...recent.invocations.filter((call) => call.status !== 'pending'),
      ...pending.invocations,
    ],
    hasMore: Boolean(recent.has_more && recent.next_before_id != null),
  };
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

/** Connected servers are available automatically; this panel manages overrides and approvals. */
interface McpSessionPanelProps {
  jobId: string;
  threadId: string;
  active: boolean;
  approvalsOnly?: boolean;
}

export function McpSessionPanel(props: McpSessionPanelProps) {
  // Reset both drafts and in-flight decision state before another job renders.
  return <McpSessionPanelContent key={`${props.threadId}:${props.jobId}`} {...props} />;
}

function McpSessionPanelContent({
  jobId,
  threadId,
  active,
  approvalsOnly = false,
}: McpSessionPanelProps) {
  const [bindings, setBindings] = useState<McpBindingApi[]>([]);
  const [selections, setSelections] = useState<Record<number, Selection>>({});
  const [invocations, setInvocations] = useState<McpInvocationApi[]>([]);
  const reviewedInvocations = useRef(new Set<number>());
  const historyPages = useRef(1);
  const historyReadVersion = useRef(0);
  const historyAppliedVersion = useRef(0);
  const [historyHasMore, setHistoryHasMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const historyOperation = useRef(false);
  const pending = invocations.filter((call) => call.status === 'pending');
  const decided = invocations
    .filter(
      (call) =>
        call.status !== 'pending' && (!approvalsOnly || reviewedInvocations.current.has(call.id)),
    )
    .slice(0, approvalsOnly ? 1 : historyPages.current * 50);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [decidingIds, setDecidingIds] = useState(new Set<number>());
  const decisionOperations = useRef(new Set<number>());
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const selectionOperation = useRef(false);
  // Calls approved with "Always allow" whose rule is not saved yet. Saving a
  // server's rules expires every call still waiting on it, the approved one
  // included, so each rule waits until nothing on its server is in flight.
  const alwaysAllow = useRef(new Map<number, McpInvocationApi>());
  // Each save reads the rules and writes them back; one at a time, so two
  // rules for the same server cannot overwrite each other.
  const alwaysAllowSaves = useRef(Promise.resolve());

  const applyInvocationSnapshot = useCallback(
    (response: { invocations: McpInvocationApi[]; hasMore: boolean }, version: number) => {
      if (version < historyAppliedVersion.current) return;
      historyAppliedVersion.current = version;
      setInvocations((current) =>
        mergeInvocations(current, response.invocations, true, historyPages.current * 50),
      );
      setHistoryHasMore(response.hasMore);
    },
    [],
  );

  const load = useCallback(async () => {
    if (selectionOperation.current) return;
    selectionOperation.current = true;
    setLoading(true);
    setError(null);
    const historyVersion = ++historyReadVersion.current;
    try {
      const [bindingResponse, invocationResponse] = await Promise.all([
        approvalsOnly ? Promise.resolve({ bindings: [] }) : getMcpThreadBindings(threadId),
        readInvocations(jobId, historyPages.current),
      ]);
      const uniqueBindings = uniqueBindingAliases(bindingResponse.bindings);
      setBindings(uniqueBindings);
      setSelections(draftSelections(uniqueBindings));
      if (
        uniqueBindings.some(
          (binding, index) => binding.alias !== bindingResponse.bindings[index].alias,
        )
      ) {
        setNotice(
          'Duplicate connection aliases were adjusted. Save permissions to apply these names.',
        );
      }
      applyInvocationSnapshot(invocationResponse, historyVersion);
      setLoaded(true);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not load MCP settings.');
    } finally {
      selectionOperation.current = false;
      setLoading(false);
    }
  }, [jobId, threadId, approvalsOnly, applyInvocationSnapshot]);

  useEffect(() => {
    if (!active) return;
    void load();
  }, [active, load]);

  // A held call has a deadline, and the agent is blocked on the answer: a
  // panel that only refreshes when the user clicks would show a stale queue
  // for exactly as long as the call is waiting.
  // While the owner's change stream flows, each change to this job's calls is
  // pushed and read at once, and the clock is only the backstop.
  const pushLive = useAgentPushLive();
  useEffect(() => {
    if (!active) return;
    let disposed = false;
    let loading = false;
    let again = false;
    const refresh = async () => {
      if (loading) {
        again = true;
        return;
      }
      loading = true;
      try {
        // Follow approved calls through dispatch and completion as well.
        const historyVersion = ++historyReadVersion.current;
        const response = await readInvocations(jobId, historyPages.current);
        if (!disposed) {
          applyInvocationSnapshot(response, historyVersion);
        }
      } catch {
        // Keep the last known state through transient polling failures.
      } finally {
        loading = false;
        if (again && !disposed) {
          again = false;
          void refresh();
        }
      }
    };
    const timer = setInterval(refresh, pushLive ? PUSH_FALLBACK_POLL_MS : 3000);
    const unsubscribe = onAgentRecordsChanged((change) => {
      if (change.resync || change.mcp.includes(jobId)) void refresh();
    });
    return () => {
      disposed = true;
      clearInterval(timer);
      unsubscribe();
    };
  }, [active, jobId, pushLive, applyInvocationSnapshot]);

  async function loadOlderRequests() {
    if (!historyHasMore || historyOperation.current) return;
    historyOperation.current = true;
    setLoadingHistory(true);
    setError(null);
    const pages = historyPages.current + 1;
    historyPages.current = pages;
    const historyVersion = ++historyReadVersion.current;
    try {
      const response = await readInvocations(jobId, pages);
      applyInvocationSnapshot(response, historyVersion);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Could not load older tool requests.');
    } finally {
      historyOperation.current = false;
      setLoadingHistory(false);
    }
  }

  function setPolicy(
    connectionId: number,
    kind: McpCapabilityKind,
    name: string,
    policy: McpPolicy,
  ) {
    if (selectionOperation.current) return;
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

  async function persist(binding: McpBindingApi, message: string) {
    if (selectionOperation.current) return;
    selectionOperation.current = true;
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const payload: McpBindingRequest = {
        connection_id: binding.connection_id,
        alias: binding.alias,
        enabled: binding.enabled,
        use_defaults: binding.selection_pending,
        tools: binding.tools,
        resources: binding.resources,
        prompts: binding.prompts,
      };
      const response = await updateMcpThreadBinding(threadId, payload);
      setBindings(response.bindings);
      setSelections(draftSelections(response.bindings));
      setNotice(message);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not save server settings.');
    } finally {
      selectionOperation.current = false;
      setSaving(false);
    }
  }

  function toggle(binding: McpBindingApi) {
    const enabled = !binding.enabled;
    const hasAllowedTools = Object.values(selectionOf(binding)).some((rules) =>
      Object.values(rules).some((policy) => policy !== 'deny'),
    );
    // A previously empty selection becomes the standard ask-before-call mode.
    // Existing custom permissions survive switching a server off and on.
    const next =
      enabled && !hasAllowedTools && !binding.selection_pending
        ? { ...binding, enabled, selection_pending: true, tools: {}, resources: {}, prompts: {} }
        : { ...binding, enabled };
    void persist(
      next,
      enabled
        ? `${binding.connection_label} will be available with your next message.`
        : `${binding.connection_label} is off for this conversation.`,
    );
  }

  function savePermissions(binding: McpBindingApi, reset = false) {
    const selection = selections[binding.connection_id] ?? draftSelection(binding);
    const next = reset
      ? { ...binding, selection_pending: true, tools: {}, resources: {}, prompts: {} }
      : { ...binding, ...selection };
    void persist(
      next,
      reset
        ? 'Permissions reset. Each tool call will ask for approval.'
        : 'Permissions saved. Restrictions apply immediately; newly available tools appear with your next message.',
    );
  }

  async function decide(invocation: McpInvocationApi, approve: boolean, always = false) {
    if (decisionOperations.current.has(invocation.id)) return;
    decisionOperations.current.add(invocation.id);
    reviewedInvocations.current.add(invocation.id);
    setDecidingIds(new Set(decisionOperations.current));
    setError(null);
    setNotice(null);
    try {
      const result = await decideMcpInvocation(invocation.id, approve);
      if (always && result.status === 'approved')
        alwaysAllow.current.set(invocation.id, invocation);
      setInvocations((current) =>
        mergeInvocations(current, [result], false, historyPages.current * 50),
      );
    } catch (cause: unknown) {
      // A lost race (the call expired or another tab decided it) is reported
      // where the card was, and the next refresh shows what actually happened.
      setError(cause instanceof Error ? cause.message : 'Could not record the decision.');
    } finally {
      decisionOperations.current.delete(invocation.id);
      setDecidingIds(new Set(decisionOperations.current));
    }
  }

  /** Stop asking for this capability in this conversation. */
  const allowFromNowOn = useCallback(
    async (invocation: McpInvocationApi) => {
      const key = selectionKey(invocation.capability_kind ?? 'tool');
      const name = invocation.capability_name;
      try {
        // Read the rules fresh: the panel may not have loaded them, and a
        // stale copy would undo a change made since.
        const { bindings: current } = await getMcpThreadBindings(threadId);
        const binding = current.find((item) => item.connection_id === invocation.connection_id);
        if (!binding) {
          throw new Error(`${connectionName(invocation)} is no longer in this conversation.`);
        }
        const payload: McpBindingRequest = {
          connection_id: binding.connection_id,
          alias: binding.alias,
          enabled: binding.enabled,
          use_defaults: binding.selection_pending,
          tools: binding.tools,
          resources: binding.resources,
          prompts: binding.prompts,
        };
        payload[key] = { ...binding[key], [name]: 'allow' };
        const response = await updateMcpThreadBinding(threadId, payload);
        setBindings(response.bindings);
        // Keep unsaved edits to other capabilities; only this one changed.
        setSelections((drafts) => {
          const draft = drafts[invocation.connection_id];
          if (!draft) return drafts;
          return {
            ...drafts,
            [invocation.connection_id]: { ...draft, [key]: { ...draft[key], [name]: 'allow' } },
          };
        });
        setNotice(
          `${connectionName(invocation)} can now ${invocationAction(invocation)} without asking in this conversation.`,
        );
      } catch (cause: unknown) {
        setError(cause instanceof Error ? cause.message : 'Could not save the permission.');
      }
    },
    [threadId],
  );

  useEffect(() => {
    for (const [id, invocation] of alwaysAllow.current) {
      const inFlight = invocations.some(
        (call) =>
          call.connection_id === invocation.connection_id &&
          (call.status === 'pending' || call.status === 'approved'),
      );
      if (inFlight) continue;
      alwaysAllow.current.delete(id);
      alwaysAllowSaves.current = alwaysAllowSaves.current.then(() => allowFromNowOn(invocation));
    }
  }, [invocations, allowFromNowOn]);

  if (!active && !loaded) {
    if (approvalsOnly) return null;
    return (
      <p className="text-sm text-gray-500 dark:text-gray-400">Open this panel to load tools.</p>
    );
  }

  return (
    <div>
      {approvalsOnly && error ? (
        <p role="alert" className="mb-3 text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}
      {approvalsOnly && notice ? (
        <p role="status" className="mb-3 text-sm text-emerald-700 dark:text-emerald-400">
          {notice}
        </p>
      ) : null}
      {pending.length || decided.length || (!approvalsOnly && historyHasMore) ? (
        <section
          aria-labelledby="mcp-approvals"
          className={approvalsOnly ? 'mb-4 max-h-80 overflow-y-auto' : undefined}
        >
          <h3
            id="mcp-approvals"
            className="text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500"
          >
            Tool approvals
          </h3>
          {pending.map((invocation) => (
            <McpInvocationCard
              key={invocation.id}
              invocation={invocation}
              busy={decidingIds.has(invocation.id)}
              onDecision={(approve) => void decide(invocation, approve)}
              onAlwaysAllow={() => void decide(invocation, true, true)}
            />
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
          {!approvalsOnly && historyHasMore ? (
            <button
              type="button"
              onClick={() => void loadOlderRequests()}
              disabled={loadingHistory}
              className="mt-3 text-sm text-gray-600 hover:underline disabled:opacity-60 dark:text-gray-400"
            >
              {loadingHistory ? 'Loading older requests…' : 'Load older requests'}
            </button>
          ) : null}
        </section>
      ) : null}

      {!approvalsOnly ? (
        <fieldset
          aria-labelledby="mcp-selection"
          className="mt-6 min-w-0"
          disabled={loading || saving}
        >
          <div className="flex items-center justify-between gap-3">
            <h3
              id="mcp-selection"
              className="text-sm font-semibold text-gray-900 dark:text-gray-100"
            >
              MCP servers
            </h3>
            <div className="flex gap-3 text-xs text-gray-500 dark:text-gray-400">
              <Link href="/integrations" className="hover:underline">
                Manage servers
              </Link>
              <button
                type="button"
                onClick={() => void load()}
                disabled={loading}
                className="hover:underline disabled:opacity-60"
              >
                {loading ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>
          </div>
          <p className="mt-2 text-xs leading-5 text-gray-500 dark:text-gray-400">
            Connected servers are available automatically. Your agent chooses tools as needed and
            asks before calling them, unless you allow a tool below.
          </p>
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
          {loading && !loaded ? (
            <p role="status" className="mt-3 text-sm text-gray-500 dark:text-gray-400">
              Loading servers…
            </p>
          ) : null}
          {loaded && bindings.length === 0 ? (
            <div className="mt-4 rounded-xl border border-dashed border-gray-200 p-5 text-center dark:border-white/10">
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Connect a server to give your agent access to your tools and data.
              </p>
              <Link
                href="/integrations"
                className="mt-3 inline-block text-sm font-medium text-gray-900 underline dark:text-gray-100"
              >
                Add MCP server
              </Link>
            </div>
          ) : null}
          {bindings.map((binding) => {
            const selection = selections[binding.connection_id] ?? draftSelection(binding);
            return (
              <div
                key={binding.connection_id}
                className="mt-3 rounded-xl border border-gray-200 p-3 dark:border-white/10"
              >
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="font-medium text-gray-900 dark:text-gray-50">
                      {binding.connection_label}
                    </p>
                    <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                      {!binding.enabled
                        ? 'Off for this conversation'
                        : !binding.available
                          ? 'Needs attention'
                          : `${binding.capabilities.filter((item) => item.kind === 'tool').length} tools · Enabled`}
                    </p>
                  </div>
                  <button
                    type="button"
                    role="switch"
                    aria-label={`Use ${binding.connection_label} in this conversation`}
                    aria-checked={binding.enabled}
                    onClick={() => toggle(binding)}
                    className={`relative h-6 w-11 shrink-0 rounded-full transition-colors ${binding.enabled ? 'bg-gray-900 dark:bg-gray-100' : 'bg-gray-200 dark:bg-white/20'}`}
                  >
                    <span
                      className={`absolute top-0.5 h-5 w-5 rounded-full bg-white shadow-sm transition-transform dark:bg-gray-900 ${binding.enabled ? 'left-0.5 translate-x-5' : 'left-0.5'}`}
                    />
                  </button>
                </div>
                {!binding.available ? (
                  <p className="mt-2 rounded-lg bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300">
                    {binding.error ?? 'This server is currently unavailable.'}{' '}
                    <Link href="/integrations" className="underline">
                      Manage connection
                    </Link>
                  </p>
                ) : null}
                {binding.capabilities.length ? (
                  <details className="mt-3">
                    <summary className="w-fit cursor-pointer text-xs text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100">
                      Tools and permissions
                    </summary>
                    <CapabilityList
                      capabilities={binding.capabilities}
                      selection={selection}
                      onPolicy={(kind, name, policy) =>
                        setPolicy(binding.connection_id, kind, name, policy)
                      }
                    />
                    <div className="mt-3 flex flex-wrap gap-3">
                      <button
                        type="button"
                        onClick={() => savePermissions(binding)}
                        className="rounded-lg bg-gray-900 px-3 py-1.5 text-xs font-medium text-white dark:bg-gray-100 dark:text-gray-900"
                      >
                        Save permissions
                      </button>
                      <button
                        type="button"
                        onClick={() => savePermissions(binding, true)}
                        className="text-xs text-gray-500 hover:underline dark:text-gray-400"
                      >
                        Reset to ask before each call
                      </button>
                    </div>
                  </details>
                ) : binding.available ? (
                  <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                    No tools discovered yet.{' '}
                    <Link href="/integrations" className="underline">
                      Refresh the connection
                    </Link>
                  </p>
                ) : null}
              </div>
            );
          })}
        </fieldset>
      ) : null}
    </div>
  );
}
