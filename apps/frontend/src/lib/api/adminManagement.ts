// Admin client for configuration management: the semantic catalog, saved
// versus adopted state, deployment agents, workflows and named credentials.
//
// Secret values for a machine are sealed here, in the browser, to that
// machine's agent key (ECDH P-256, HKDF-SHA256, AES-256-GCM), in the format of
// `cloud_agent_shared/deployment_contract.py`. The control plane relays the
// ciphertext and cannot read it. See `deploy/agent/README.md`.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const MANAGEMENT = '/admin/agent/management';
const DEPLOYMENTS = '/admin/agent/deployments';
const CREDENTIALS = '/admin/agent/credentials';

export type ManagementMethod = 'edit' | 'apply' | 'workflow' | 'observe' | 'object';

export interface CatalogItem {
  id: string;
  label: string;
  group: string;
  method: ManagementMethod;
  sources: { kind: string; reader: string; meaning: string }[];
  owner: string;
  description: string;
  apply_mode: string;
  restart_scope: string[];
  role: 'control-plane' | 'host' | 'any' | null;
  env_names: string[];
  setting_keys: string[];
  bounded: boolean;
}

export interface ManagementCatalog {
  methods: ManagementMethod[];
  apply_modes: string[];
  items: CatalogItem[];
}

export interface InstanceView {
  instance_id: string;
  role: string;
  machine: string;
  build_sha: string;
  capabilities: string[];
  adopted_settings_revision: number;
  adopted: boolean;
  restart_required: string[];
  current: boolean;
  stopped: boolean;
  last_seen_at: string;
  root_key_source: string | null;
  root_key_id: string | null;
}

export interface ManagementStatus {
  settings: {
    saved_revision: number;
    reporting: number;
    adopted: number;
    restart_required: Record<string, string[]>;
  };
  instances: InstanceView[];
  unidentified_sessions: {
    application_name: string;
    user: string;
    client_addr: string;
    backend_start: string | null;
  }[];
  hosts: {
    host: string;
    status: string;
    reports_settings: boolean;
    settings_pending: string[];
    config_reports: Record<string, string>;
  }[];
}

export interface AgentObserved {
  deployed_commit?: string;
  config_rev?: number | null;
  config_versions?: number[];
  managed_keys?: string[];
  unmanaged_keys?: string[];
  legacy_names_present?: string[];
  releases?: Record<
    string,
    { source_commit: string; database_revision?: string; pinned_by?: string }
  >;
  release?: { manifest_sha256: string; commit: string } | null;
  kata?: { version: string; sha256: string } | null;
  runner_host?: string;
  services?: Record<string, boolean>;
  policy?: {
    allowed: Record<string, string[]>;
    kata: { version: string; sha256: string }[];
    has_kata_helper: boolean;
    has_database_tools: boolean;
    tunnel_authorized_keys?: string[];
  };
  /** Where this machine's database connection points; never the password. */
  database?: {
    host: string;
    port: number;
    database: string;
    user: string;
    query: string;
  } | null;
  secret_ids?: Record<string, string>;
}

export interface DeploymentAgent {
  agent_id: string;
  name: string;
  role: 'control-plane' | 'host';
  machine: string;
  public_key: string;
  enrolled_at: string;
  last_report_at: string | null;
  revoked: boolean;
  online: boolean;
  observed: AgentObserved;
  local_operations: { id: string; operation: string; state: string; detail: string }[];
}

export interface DeploymentIntent {
  intent_id: string;
  agent_id: string;
  seq: number;
  operation: string;
  params: Record<string, unknown>;
  state: string;
  detail: string;
  evidence: Record<string, unknown>;
  workflow_id: string | null;
  created_by: string;
  created_at: string;
}

export interface DeploymentWorkflow {
  workflow_id: string;
  kind: string;
  state: 'running' | 'waiting' | 'succeeded' | 'failed' | 'cancelled';
  step: string;
  detail: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  /** Counters and any question for an administrator; never a sealed value. */
  progress?: WorkflowProgress;
}

export interface FarEndRequest {
  token: string;
  host: string;
  action: 'add' | 'remove';
  tunnels: Record<string, string>;
}

export interface WorkflowProgress {
  totals?: { rewritten?: number; current?: number; unreadable?: number; raced?: number };
  reindexed?: { bindings?: number; snapshots?: number };
  phases?: Record<string, string>;
  attention?: FarEndRequest | null;
  public_keys?: Record<string, { old?: Record<string, string>; new?: Record<string, string> }>;
  [key: string]: unknown;
}

export interface MaintenanceState {
  active: boolean;
  kind?: string;
  workflow_id?: string;
  since?: string;
  detail?: string;
  /** What this process last heard from agents; it is lost if the process restarts. */
  progress?: Record<
    string,
    {
      operations: { operation?: string; phase?: string | null; state?: string }[];
      received_at: string;
    }
  >;
}

export interface HostCredentialView {
  host: string;
  status: string;
  current_key: boolean;
  key_known: boolean;
  renews: boolean;
  renewal_pending: boolean;
  renewal_requested: boolean;
  last_heartbeat_at: string | null;
  lag: string | null;
}

export interface ConfigKeyContract {
  kind: string;
  item: string;
  services: string[];
  choices: string[];
  boundary: '' | 'policy' | 'grow';
}

export interface DeploymentOverview {
  /** Absent from a control plane without maintenance mode. */
  maintenance?: MaintenanceState;
  agents: DeploymentAgent[];
  intents: DeploymentIntent[];
  workflows: DeploymentWorkflow[];
  contract: {
    operations: Record<
      string,
      { roles: string[]; confirm: boolean; sealed: boolean; params: string[]; description: string }
    >;
    config_keys: Record<string, Record<string, ConfigKeyContract>>;
    services: Record<string, string[]>;
  };
}

export interface AdminCredential {
  credential_id: string;
  name: string;
  scopes: string[];
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export type SealedBox = { v: 1; epk: string; iv: string; ct: string };

function url(path: string): string {
  return path;
}

async function get<T>(path: string): Promise<T> {
  return jsonOrThrow<T>(await fetchWithAuth(config.apiBase, url(path)));
}

async function post<T>(path: string, body: unknown): Promise<T> {
  return jsonOrThrow<T>(
    await fetchWithAuth(config.apiBase, url(path), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  );
}

export const getManagementCatalog = () => get<ManagementCatalog>(`${MANAGEMENT}/catalog`);
export const getManagementStatus = () => get<ManagementStatus>(`${MANAGEMENT}/status`);
export const getDeployments = () => get<DeploymentOverview>(DEPLOYMENTS);
export const getWorkflow = (id: string) =>
  get<DeploymentWorkflow & { intents: DeploymentIntent[]; progress: Record<string, unknown> }>(
    `${DEPLOYMENTS}/workflows/${encodeURIComponent(id)}`,
  );

export const createEnrollToken = (role: 'control-plane' | 'host') =>
  post<{ token: string; expires_in_seconds: number }>(`${DEPLOYMENTS}/enroll-token`, { role });

export const createIntent = (
  agentId: string,
  request: {
    operation: string;
    params: Record<string, unknown>;
    sealed?: Record<string, SealedBox>;
    confirm?: string;
  },
) =>
  post<DeploymentIntent>(`${DEPLOYMENTS}/agents/${encodeURIComponent(agentId)}/intents`, {
    sealed: {},
    ...request,
    idempotency_key: randomHex(16),
  });

export const cancelIntent = (intentId: string) =>
  post<{ cancelled: boolean }>(`${DEPLOYMENTS}/intents/${encodeURIComponent(intentId)}/cancel`, {});

export const revokeAgent = (agentId: string, confirm: string) =>
  post<{ revoked: boolean }>(`${DEPLOYMENTS}/agents/${encodeURIComponent(agentId)}/revoke`, {
    confirm,
  });

export const startRollout = (manifestSha256: string) =>
  post<DeploymentWorkflow>(`${DEPLOYMENTS}/rollouts`, {
    manifest_sha256: manifestSha256,
    confirm: 'release_activate',
  });

export const startWorkflow = (request: {
  kind: string;
  params?: Record<string, unknown>;
  sealed_by_agent?: Record<string, Record<string, SealedBox>>;
  secret?: string;
}) =>
  post<DeploymentWorkflow>(`${DEPLOYMENTS}/workflows`, {
    params: {},
    sealed_by_agent: {},
    ...request,
    confirm: request.kind,
  });

export const retryWorkflow = (workflowId: string) =>
  post<DeploymentWorkflow>(`${DEPLOYMENTS}/workflows/${encodeURIComponent(workflowId)}/retry`, {});

export const confirmFarEnd = (workflowId: string, token: string) =>
  post<DeploymentWorkflow>(
    `${DEPLOYMENTS}/workflows/${encodeURIComponent(workflowId)}/confirm-far-end`,
    { token },
  );

export const getHostCredentials = () =>
  get<{ signing_key_id: string; hosts: HostCredentialView[] }>(`${DEPLOYMENTS}/host-credentials`);

export const renewHostCredential = (host: string) =>
  post<{ requested: boolean }>(
    `${DEPLOYMENTS}/host-credentials/${encodeURIComponent(host)}/renew`,
    { confirm: host },
  );

export const listCredentials = () => get<{ credentials: AdminCredential[] }>(CREDENTIALS);
export const createCredential = (name: string, deployment: boolean, expiresInDays: number | null) =>
  post<AdminCredential & { credential: string }>(CREDENTIALS, {
    name,
    deployment,
    expires_in_days: expiresInDays,
  });
export const revokeCredential = (id: string) =>
  post<{ revoked: boolean }>(`${CREDENTIALS}/${encodeURIComponent(id)}/revoke`, {});

// -- sealing ------------------------------------------------------------------

const encoder = new TextEncoder();

function toBase64(bytes: Uint8Array): string {
  let text = '';
  for (const byte of bytes) text += String.fromCharCode(byte);
  return btoa(text);
}

export function fromBase64(text: string): Uint8Array<ArrayBuffer> {
  const raw = atob(text);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

export function randomHex(bytes: number): string {
  const values = crypto.getRandomValues(new Uint8Array(bytes));
  return Array.from(values, (value) => value.toString(16).padStart(2, '0')).join('');
}

/** A random secret of 32 bytes, URL-safe, for a new token or key. */
export function randomSecret(): string {
  return toBase64(crypto.getRandomValues(new Uint8Array(32)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
}

export async function deriveSealKey(shared: ArrayBuffer, purpose: string): Promise<CryptoKey> {
  const material = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveKey']);
  return crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: encoder.encode('cloud-agent sealed value v1'),
      info: encoder.encode(purpose),
    },
    material,
    { name: 'AES-GCM', length: 256 },
    false,
    ['encrypt', 'decrypt'],
  );
}

/** The binding of a sealed value to one agent and one key name. */
export const sealedPurpose = (agentId: string, name: string) => `${agentId}:${name}`;

/** Encrypt `plaintext` so only the agent holding `publicKey` can read it. */
export async function seal(
  publicKey: string,
  plaintext: string,
  purpose: string,
): Promise<SealedBox> {
  const peer = await crypto.subtle.importKey(
    'raw',
    fromBase64(publicKey),
    { name: 'ECDH', namedCurve: 'P-256' },
    false,
    [],
  );
  const ephemeral = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, true, [
    'deriveBits',
  ]);
  const shared = await crypto.subtle.deriveBits(
    { name: 'ECDH', public: peer },
    ephemeral.privateKey,
    256,
  );
  const key = await deriveSealKey(shared, purpose);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv, additionalData: encoder.encode(purpose) },
    key,
    encoder.encode(plaintext),
  );
  const epk = await crypto.subtle.exportKey('raw', ephemeral.publicKey);
  return {
    v: 1,
    epk: toBase64(new Uint8Array(epk)),
    iv: toBase64(iv),
    ct: toBase64(new Uint8Array(ct)),
  };
}

/** One-way identifier of a secret, as control-plane processes and agents report it. */
export async function keyIdentifier(secret: string): Promise<string> {
  if (!secret) return '';
  const key = await crypto.subtle.importKey(
    'raw',
    encoder.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const mac = await crypto.subtle.sign(
    'HMAC',
    key,
    encoder.encode('cloud-agent key identifier v1'),
  );
  return Array.from(new Uint8Array(mac), (value) => value.toString(16).padStart(2, '0'))
    .join('')
    .slice(0, 16);
}

export interface DatabaseAddress {
  host: string;
  port: number;
  database: string;
  user: string;
  password: string;
  /** Any options the connection already carries, kept as they are. */
  query?: string;
}

/** The connection URL an address describes; only ever sealed, never sent as it is. */
export function databaseUrl(address: DatabaseAddress): string {
  const part = encodeURIComponent;
  const host = address.host.includes(':') ? `[${address.host}]` : address.host;
  const url = `postgresql://${part(address.user)}:${part(address.password)}@${host}:${address.port}/${part(address.database)}`;
  return address.query ? `${url}?${address.query}` : url;
}

/** Seal one value to every listed agent, keyed by agent id. */
export async function sealForAgents(
  agents: DeploymentAgent[],
  name: string,
  value: string,
): Promise<Record<string, Record<string, SealedBox>>> {
  const sealed: Record<string, Record<string, SealedBox>> = {};
  for (const agent of agents) {
    sealed[agent.agent_id] = {
      [name]: await seal(agent.public_key, value, sealedPurpose(agent.agent_id, name)),
    };
  }
  return sealed;
}
