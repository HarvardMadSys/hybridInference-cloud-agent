// Resource lifecycle stays separate from host admission and capacity APIs.
// Secret material is accepted only by connection writes; no read type has it.
import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const ROOT = '/admin/agent';

export interface BlockedReason {
  code: string;
  message: string;
}

export interface ComputeField {
  key: string;
  label: string;
  type: 'text' | 'number' | 'select' | 'json' | 'textarea';
  required?: boolean;
  advanced?: boolean;
  section?: 'template' | 'access';
  deployment_only?: boolean;
  default?: unknown;
  description?: string;
  options?: { value: string; label: string }[];
  modes?: ('attach' | 'provision')[];
  min?: number;
  max?: number;
  max_length?: number;
}

export interface ComputeProvider {
  id: string;
  label: string;
  modes: ('attach' | 'provision')[];
  capabilities: Record<string, boolean>;
  fields: ComputeField[];
  connection_fields: ComputeField[];
  secret_label?: string;
  requires_ssh_connection?: boolean;
}

export interface ComputeCatalog {
  providers: ComputeProvider[];
  deployment_profiles: { id: string; label: string }[];
  secret_store_configured: boolean;
}

export interface ComputeConnection {
  id: string;
  name: string;
  provider_id: string;
  configuration: Record<string, unknown>;
  status: string;
  masked_summary: string;
  revision: number;
  blocked_reasons: BlockedReason[];
}

export interface RenewalPolicy {
  enabled: boolean;
  renew_before_seconds: number;
  extend_to_horizon_seconds: number;
  stop_new_groups_before_seconds: number;
  stop_claims_before_seconds: number;
  reason: string;
  /** Move the Host to a new node when an extension is refused or the node is old. */
  rotation_enabled: boolean;
  max_node_age_seconds: number;
  /** Hardware a replacement node may use, newest first. */
  node_types: string[];
  /** The provider profile parameter that selects the hardware. */
  node_type_binding: string;
}

/** When the node is next renewed or replaced, and how its last move went. */
export interface ComputeRotation {
  enabled: boolean;
  decide_at: string | null;
  replace_after: string | null;
  last: {
    id: string;
    state: string;
    step: string;
    message: string;
    updated_at: string;
    completed_at: string | null;
  } | null;
}

export interface ComputeNode {
  id: string;
  address?: string | null;
  port?: number | null;
  username?: string | null;
  name: string;
  hosts: string[];
  state?: string;
}

export interface ComputeAllocation {
  id: string;
  connection_id?: string | null;
  ssh_connection_id?: string | null;
  name: string;
  provider_id: string;
  state: string;
  external_id: string | null;
  expires_at: string | null;
  observed_at: string | null;
  check_interval_seconds?: number | null;
  policy: RenewalPolicy;
  revision: number;
  management_mode?: 'observe' | 'manage';
  rotation?: ComputeRotation;
  /** Set when this node took its Host over from that allocation. */
  predecessor_id?: string | null;
  allowed_actions: string[];
  blocked_reasons: BlockedReason[];
  nodes: ComputeNode[];
}

export interface ComputeOperation {
  id: string;
  request_id: string;
  kind: string;
  allocation_id: string | null;
  state: string;
  step: string;
  message: string;
  created_at: string;
  updated_at: string;
  allowed_actions: string[];
  blocked_reasons: BlockedReason[];
  result?: Record<string, unknown> | null;
}

export interface ServerRequest {
  request_id: string;
  name: string;
  provider_id: string;
  mode: 'attach' | 'provision';
  connection_id: string;
  ssh_connection_id?: string;
  configuration: Record<string, unknown>;
  deployment_profile?: string;
}

export interface RetirementImpact {
  allocation_id: string;
  revision: number;
  nodes: ComputeNode[];
  hosts: string[];
  active_attempts: number;
  retained_groups: number;
  global_active_host: string | null;
  allowed_actions: string[];
  blocked_reasons: BlockedReason[];
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return jsonOrThrow<T>(
    await fetchWithAuth(config.apiBase, `${ROOT}${path}`, { signal, cache: 'no-store' }),
  );
}

async function write<T>(path: string, body: unknown, method = 'POST'): Promise<T> {
  return jsonOrThrow<T>(
    await fetchWithAuth(config.apiBase, `${ROOT}${path}`, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }),
  );
}

export const getComputeProviders = (signal?: AbortSignal) =>
  get<ComputeCatalog>('/compute-providers', signal);
export const getComputeConnections = (signal?: AbortSignal) =>
  get<{ connections: ComputeConnection[] }>('/compute-connections', signal);
export interface ComputeServerSnapshot {
  allocations: ComputeAllocation[];
  hosts: {
    host: string;
    compute_node_id: string | null;
    allocation_id: string | null;
    retained_groups: number;
    status: string;
    allows_new_groups?: boolean;
    allows_claims?: boolean;
  }[];
  active_host: string | null;
  observed_at: string;
}

export const getComputeServers = (signal?: AbortSignal) =>
  get<ComputeServerSnapshot>('/servers', signal);
export const getComputeOperations = (requestId?: string, signal?: AbortSignal) =>
  get<{ operations: ComputeOperation[] }>(
    `/compute-operations${requestId ? `?request_id=${encodeURIComponent(requestId)}` : ''}`,
    signal,
  );
export const getRetirementImpact = (id: string) =>
  get<RetirementImpact>(`/compute-allocations/${encodeURIComponent(id)}/retirement-impact`);

export const saveComputeConnection = (
  body: {
    name: string;
    provider_id: string;
    configuration: Record<string, unknown>;
    secret?: string;
    expected_revision?: number;
  },
  id?: string,
) =>
  write<{ connection: ComputeConnection }>(
    `/compute-connections${id ? `/${encodeURIComponent(id)}` : ''}`,
    id ? (({ provider_id: _provider, ...update }) => update)(body) : body,
    id ? 'PATCH' : 'POST',
  );

export const createComputeServer = (body: ServerRequest) =>
  write<{ operation: ComputeOperation }>('/servers', body);
export const preflightComputeServer = (body: ServerRequest) =>
  write<{ operation: ComputeOperation }>('/server-preflights', body);
export const importComputeResource = (body: ServerRequest) =>
  write<{ operation: ComputeOperation }>('/compute-allocations/import', {
    request_id: body.request_id,
    name: body.name,
    provider_id: body.provider_id,
    connection_id: body.connection_id,
    configuration: body.configuration,
  });
export const setComputePolicy = (
  id: string,
  policy: RenewalPolicy,
  revision: number,
  managementMode?: 'observe' | 'manage',
) =>
  write<{ allocation: ComputeAllocation }>(
    `/compute-allocations/${encodeURIComponent(id)}/policy`,
    {
      policy,
      expected_revision: revision,
      ...(managementMode ? { management_mode: managementMode } : {}),
    },
    'PATCH',
  );
export const extendComputeAllocation = (
  id: string,
  body: { request_id: string; target_expires_at: string; reason: string },
) =>
  write<{ operation: ComputeOperation }>(
    `/compute-allocations/${encodeURIComponent(id)}/extend`,
    body,
  );
export const retireComputeAllocation = (
  id: string,
  body: { request_id: string; expected_revision: number },
) =>
  write<{ operation: ComputeOperation }>(
    `/compute-allocations/${encodeURIComponent(id)}/retire`,
    body,
  );
export const rotateComputeAllocation = (
  id: string,
  body: { request_id: string; expected_revision: number },
) =>
  write<{ operation: ComputeOperation }>(
    `/compute-allocations/${encodeURIComponent(id)}/rotate`,
    body,
  );
export const actOnComputeOperation = (id: string, action: 'retry' | 'cancel') =>
  write<{ operation: ComputeOperation }>(
    `/compute-operations/${encodeURIComponent(id)}/${action}`,
    {},
  );

export const validateComputeConnection = (id: string) =>
  write<{ connection: ComputeConnection }>(
    `/compute-connections/${encodeURIComponent(id)}/validate`,
    {},
  );

export const resolveComputeOperation = (id: string, externalId: string) =>
  write<{ operation: ComputeOperation }>(`/compute-operations/${encodeURIComponent(id)}/resolve`, {
    external_id: externalId,
  });
