// Generic MCP connection API (per-user MCP v1).
//
// A module of its own rather than more of `agents.ts`: three unrelated screens
// read this contract — the Integrations page, a conversation's tool panel, and
// the approval card — and none of them is about creating a job.
//
// Two properties of the backend shape the types below.
//
// 1. **Secrets are write-only.** There is no field here that can carry a
//    credential back: `secret_hint` is two characters and asterisks, and a
//    connection only reports `has_credential`. The types keep it that way so a
//    later edit cannot quietly start rendering a token.
// 2. **Every state change is fenced by a revision.** Editing a connection or
//    deciding a call takes the revision that was read, and a 409 means another
//    tab moved it first. The request types therefore carry their epoch fields
//    as required, not optional, and callers take them from the read response.

import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** How the platform speaks to one server. */
export type McpTransport = 'streamable-http' | 'http-sse' | 'stdio';

/** Where a stdio server actually runs. */
export type McpLocation = 'remote' | 'managed-worker' | 'user-connector';

/** How a connection authenticates to its server. */
export type McpAuthKind = 'none' | 'static' | 'oauth';

/** Lifecycle state in the owner's terms. */
export type McpConnectionStatus = 'draft' | 'active' | 'error' | 'disabled';

/**
 * Why a connection is not usable, at the granularity a user can act on.
 *
 * `none` is the healthy value rather than an absence, matching the backend,
 * which sends the field on every view.
 */
export type McpConnectionErrorKind =
  | 'none'
  | 'unreachable'
  | 'blocked_target'
  | 'needs_authorization'
  | 'client_registration_required'
  | 'launch_failed'
  | 'protocol_incompatible'
  | 'partial_capabilities';

/** Which of the protocol's three surfaces a capability is on. */
export type McpCapabilityKind = 'tool' | 'resource' | 'resource_template' | 'prompt';

/** What happens when an agent calls one capability on one connection. */
export type McpPolicy = 'deny' | 'confirm' | 'allow';

/** The persisted state of a call that may need a decision. */
export type McpInvocationStatus =
  | 'pending'
  | 'approved'
  | 'denied'
  | 'expired'
  | 'dispatching'
  | 'completed'
  | 'failed'
  | 'outcome_unknown';

/** A preset that fills in a well-known server. Never a gate on adding one. */
export interface McpTemplateApi {
  name: string;
  label: string;
  description: string;
  url: string;
  transport: McpTransport;
  auth_kind: McpAuthKind;
  notes: string[];
}

/** One connection as its owner sees it. No field can hold a secret. */
export interface McpConnectionApi {
  id: number;
  label: string;
  template: string | null;
  transport: McpTransport;
  location: McpLocation;
  url: string | null;
  /** The launch vector, emptied for a remote connection. */
  argv: string[];
  auth_kind: McpAuthKind;
  status: McpConnectionStatus;
  error_kind: McpConnectionErrorKind;
  error_detail: string | null;
  account_label: string | null;
  connector_id: number | null;
  has_credential: boolean;
  secret_hint: string | null;
  expires_at: string | null;
  config_revision: number;
  authorization_epoch: number;
  capability_revision: number;
  server_name: string | null;
  server_version: string | null;
  protocol_version: string | null;
  tool_count: number;
  resource_count: number;
  prompt_count: number;
  support: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface McpConnectionsApi {
  connections: McpConnectionApi[];
  templates: McpTemplateApi[];
}

/** One discovered capability, shared by the test result and a binding. */
export interface McpCapabilityApi {
  kind: McpCapabilityKind;
  name: string;
  description: string;
  input_schema: unknown;
  annotations: Record<string, unknown>;
}

/** What a staged diagnostic found. */
export interface McpTestResultApi {
  ok: boolean;
  error_kind: McpConnectionErrorKind;
  message: string;
  protocol_version: string | null;
  server_name: string | null;
  server_version: string | null;
  tool_count: number;
  resource_count: number;
  prompt_count: number;
  support: Record<string, unknown>;
  capabilities: McpCapabilityApi[];
}

/** One secret an imported configuration carries, already masked by the server. */
export interface McpImportSecretApi {
  name: string;
  /** Where the value would be injected: a header name or an env var. */
  target: string;
  /** Masked by the backend; never the value itself. */
  value: string;
}

/**
 * One parsed server, before anything is stored.
 *
 * This is what the confirmation step renders: the command as an argument
 * vector rather than a shell string, and every secret already masked, so the
 * user approves what would actually run.
 */
export interface McpImportDraftApi {
  label: string;
  transport: McpTransport;
  location: McpLocation;
  url: string | null;
  argv: string[];
  env_names: string[];
  headers: string[];
  secrets: McpImportSecretApi[];
  paths: string[];
  package: { runner: string; ecosystem: string } | null;
  warnings: string[];
}

export interface McpImportRejectedApi {
  name: string;
  reason: string;
}

/** The preview: nothing has been written yet. */
export interface McpImportPreviewApi {
  drafts: McpImportDraftApi[];
  rejected: McpImportRejectedApi[];
  dialect: string;
}

/** The save response: the preview that was approved, plus what it created. */
export interface McpImportSavedApi {
  drafts: McpImportDraftApi[];
  saved: McpConnectionApi[];
  rejected: McpImportRejectedApi[];
  dialect: string;
}

/** Where to send the browser, and what the flow is waiting for. */
export interface McpAuthorizeApi {
  authorization_url: string | null;
  needs_client_id: boolean;
  message: string;
}

/** One session's selection for one connection. */
export interface McpBindingApi {
  connection_id: number;
  connection_label: string;
  alias: string;
  enabled: boolean;
  /**
   * Set for a binding the platform mapped without its owner choosing. The
   * session keeps working while every capability defaults to confirm, so the
   * interface asks rather than silently narrowing or widening.
   */
  selection_pending: boolean;
  available: boolean;
  error: string | null;
  capabilities: McpCapabilityApi[];
  tools: Record<string, McpPolicy>;
  resources: Record<string, McpPolicy>;
  prompts: Record<string, McpPolicy>;
  config_revision: number;
  authorization_epoch: number;
  capability_revision: number;
  policy_revision: number;
}

export interface McpBindingsApi {
  bindings: McpBindingApi[];
}

/** One selection, as the replace call sends it back. */
export interface McpBindingRequest {
  connection_id: number;
  alias?: string;
  enabled: boolean;
  tools: Record<string, McpPolicy>;
  resources: Record<string, McpPolicy>;
  prompts: Record<string, McpPolicy>;
}

/** One call that needed a decision. */
export interface McpInvocationApi {
  id: number;
  job_id: string;
  thread_id: string;
  connection_id: number;
  connection_label: string | null;
  capability_name: string;
  status: McpInvocationStatus;
  arguments_digest: string;
  /** Null when the platform could not decode the stored body. */
  arguments: Record<string, unknown> | null;
  created_at: string | null;
  expires_at: string | null;
  decided_at: string | null;
  result_summary: string | null;
  /** What to tell the owner for a state that can no longer be decided. */
  guidance: string;
}

export interface McpInvocationsApi {
  invocations: McpInvocationApi[];
}

/** Add one connection by hand. A secret here is written, never echoed back. */
export interface CreateMcpConnectionRequest {
  label: string;
  transport: McpTransport;
  location: McpLocation;
  url?: string;
  launch?: McpLaunchRequest;
  auth_kind: McpAuthKind;
  template?: string;
  secret?: string;
  header_name?: string;
  header_scheme?: string;
  env_secrets?: Record<string, string>;
}

/** A stdio launch spec. `env` values may reference `secret://NAME`. */
export interface McpLaunchRequest {
  command: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string | null;
}

export interface McpInvocationQuery {
  jobId?: string;
  threadId?: string;
  /** Only calls still waiting on the owner. */
  pendingOnly?: boolean;
}

/** Every MCP connection this user has added, with the offered templates. */
export async function listMcpConnections(): Promise<McpConnectionsApi> {
  return jsonOrThrow(await fetchWithAuth(API_BASE, '/v1/agent/mcp/connections'));
}

/** Templates alone, for a caller that needs presets before it needs a list. */
export async function listMcpTemplates(): Promise<McpTemplateApi[]> {
  return jsonOrThrow(await fetchWithAuth(API_BASE, '/v1/agent/mcp/templates'));
}

/** Add one connection after the platform validates its address and credential. */
export async function createMcpConnection(
  body: CreateMcpConnectionRequest,
): Promise<McpConnectionApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/mcp/connections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(resp);
}

/**
 * Parse a pasted configuration without saving it.
 *
 * Separate from the save call rather than a boolean argument, because the two
 * answer with different objects: the preview describes drafts, the save
 * describes stored connections. One signature returning both would force the
 * caller to narrow a union at exactly the moment it is about to write.
 */
export async function previewMcpImport(
  text: string,
  formatHint?: string,
): Promise<McpImportPreviewApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/mcp/connections/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, format_hint: formatHint, save: false }),
  });
  return jsonOrThrow(resp);
}

/**
 * Save a paste the user has already seen.
 *
 * `locations` overrides the run location per draft, which is the one decision
 * the paste cannot make for the user: a colleague's snippet says nothing about
 * whether this deployment may run a local process for it.
 */
export async function saveMcpImport(
  text: string,
  locations: Record<string, McpLocation> = {},
  envSecrets: Record<string, string> = {},
  formatHint?: string,
): Promise<McpImportSavedApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/mcp/connections/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      text,
      format_hint: formatHint,
      save: true,
      locations,
      env_secrets: envSecrets,
    }),
  });
  return jsonOrThrow(resp);
}

/**
 * Handshake, authenticate and read what a server offers.
 *
 * Nothing else: no business tool is called, and the outcome is recorded on the
 * connection, which is why the caller reloads the list afterwards instead of
 * keeping this result.
 */
export async function testMcpConnection(connectionId: number): Promise<McpTestResultApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/mcp/connections/${connectionId}/test`, {
    method: 'POST',
  });
  return jsonOrThrow(resp);
}

/** Turn a connection off, keeping a tombstone and clearing its credential. */
export async function disconnectMcpConnection(connectionId: number): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/mcp/connections/${connectionId}`, {
    method: 'DELETE',
  });
  if (!resp.ok) await jsonOrThrow(resp);
}

/**
 * Start a browser authorization for an OAuth connection.
 *
 * `clientId` is sent when the user supplies one after the platform reported
 * `needs_client_id`; the platform still owns discovery, registration and PKCE,
 * so the browser only ever travels to a URL this service built.
 */
export async function authorizeMcpConnection(
  connectionId: number,
  clientId?: string,
): Promise<McpAuthorizeApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/mcp/connections/${connectionId}/authorize`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(clientId ? { client_id: clientId } : {}),
    },
  );
  return jsonOrThrow(resp);
}

/**
 * The stored capability snapshot: tools, resources, templates and prompts.
 *
 * Read separately from the connection list because it is the one place a
 * caller can learn what an *unbound* connection offers, and a binding response
 * only describes connections the conversation already has.
 */
export async function getMcpConnectionCapabilities(
  connectionId: number,
): Promise<McpCapabilityApi[]> {
  return jsonOrThrow(
    await fetchWithAuth(API_BASE, `/v1/agent/mcp/connections/${connectionId}/capabilities`),
  );
}

/** What one conversation may use, and what each connection offers. */
export async function getMcpThreadBindings(threadId: string): Promise<McpBindingsApi> {
  return jsonOrThrow(
    await fetchWithAuth(API_BASE, `/v1/agent/mcp/threads/${encodeURIComponent(threadId)}/bindings`),
  );
}

/**
 * Replace a conversation's selection wholesale.
 *
 * Wholesale rather than per-capability because the backend validates the
 * selection against the connections' current revisions as one write: a partial
 * update could leave a binding that already fails its own fence.
 */
export async function replaceMcpThreadBindings(
  threadId: string,
  bindings: McpBindingRequest[],
): Promise<McpBindingsApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/mcp/threads/${encodeURIComponent(threadId)}/bindings`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bindings }),
    },
  );
  return jsonOrThrow(resp);
}

/** Calls that needed a decision, for one job or one conversation. */
export async function listMcpInvocations(query: McpInvocationQuery): Promise<McpInvocationsApi> {
  const params = new URLSearchParams();
  if (query.jobId) params.set('job_id', query.jobId);
  if (query.threadId) params.set('thread_id', query.threadId);
  if (query.pendingOnly) params.set('pending_only', 'true');
  return jsonOrThrow(
    await fetchWithAuth(API_BASE, `/v1/agent/mcp/invocations?${params.toString()}`),
  );
}

/**
 * Approve or deny one held call.
 *
 * A late decision loses to the call's deadline in SQL and comes back as a
 * conflict, so callers must treat a 409 as "too late", not "retry".
 */
export async function decideMcpInvocation(
  invocationId: number,
  approve: boolean,
): Promise<McpInvocationApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/mcp/invocations/${invocationId}/decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ approve }),
  });
  return jsonOrThrow(resp);
}
