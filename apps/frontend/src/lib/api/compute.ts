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
  type: 'text' | 'number' | 'select' | 'json' | 'textarea' | 'string_list';
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

export interface CloudLabDefaults {
  project: string;
  duration: number;
  profile_project: string;
  profile_name: string;
  ssh_username: string;
  ssh_public_key: string;
  /** Hardware attempted in order for each new allocation. */
  node_types: string[];
  node_type_binding: string;
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
  /**
   * Without rotation, when the Host starts moving its sandboxes to other Hosts
   * one at a time through their checkpoints; 0 never. Older policies omit it.
   */
  move_out_before_seconds?: number;
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
  sandbox_storage_path?: string | null;
  ssh_host_key_policy?: 'unchecked' | 'known_hosts' | 'registration' | null;
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
  sandbox_storage_path?: string;
}

export interface DeploymentProfileRequest {
  id: string;
  label: string;
  deployment_sha: string;
  deploy_root: string;
  environment: string;
  files: Record<string, string>;
  source_bundle_base64: string;
  install_prerequisites: boolean;
  timeout_seconds: number;
  data_mount?: string;
}

export interface AutomaticComputeSetupStatus {
  available: boolean;
  deployment_sha: string | null;
  blocked_reasons: BlockedReason[];
  machine_defaults?: {
    gateway: { address: string };
    control: { address: string };
  };
  defaults?: Omit<AutomaticComputeSetupRequest, 'request_id'>;
  pending_request_id?: string | null;
}

export interface AutomaticComputeSetupRequest {
  request_id: string;
  gateway: { connection_id: string; address: string; port: number; username: string };
  control: { connection_id: string; address: string; port: number; username: string };
  gateway_port: number;
  chat_network_pool: string;
  dns_server: string;
  protected_cidrs: string[];
  slots: number;
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
export const saveComputeDeploymentProfile = (body: DeploymentProfileRequest) =>
  write<{ profile: { id: string; label: string } }>('/compute-deployment-profiles', body);
export const getComputeAutomaticSetup = (signal?: AbortSignal) =>
  get<AutomaticComputeSetupStatus>('/compute-automatic-setup', signal);
export const createAutomaticComputeSetup = (body: AutomaticComputeSetupRequest) =>
  write<{ profiles: { id: string; label: string }[] }>(
    '/compute-deployment-profiles/automatic',
    body,
  );
export const getCloudLabDefaults = (signal?: AbortSignal) =>
  get<{ defaults: CloudLabDefaults }>('/compute-defaults/cloudlab', signal);
export const saveCloudLabDefaults = (defaults: CloudLabDefaults) =>
  write<{ defaults: CloudLabDefaults }>('/compute-defaults/cloudlab', defaults, 'PUT');

export interface ComputeServerSnapshot {
  allocations: ComputeAllocation[];
  hosts: {
    host: string;
    compute_node_id: string | null;
    allocation_id: string | null;
    retained_groups: number;
    status: string;
    drain_fence_active?: boolean;
    removal_pending?: boolean;
    allows_new_groups?: boolean;
    allows_claims?: boolean;
  }[];
  active_host: string | null;
  observed_at: string;
}

export const getComputeServers = (signal?: AbortSignal) =>
  get<ComputeServerSnapshot>('/servers', signal);
export const removeComputeServer = (id: string, expectedRevision: number) =>
  write<{ removed: true }>(
    `/servers/${encodeURIComponent(id)}`,
    { expected_revision: expectedRevision },
    'DELETE',
  );
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
    configuration?: Record<string, unknown>;
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

export interface CloudLabAutoscalingConfiguration {
  project?: string;
  group?: string;
  profile_project?: string;
  profile_name?: string;
  duration?: number;
  bindings?: Record<string, unknown>;
  ssh_username?: string;
  ssh_public_key?: string;
  node_types?: string[];
  node_type_binding?: string;
  /** Autoscaled nodes always pin the key registered by their first-boot script. */
  host_key_source?: 'registration';
}

export interface CloudLabSlotPolicy {
  cpu_reservation_fraction: number;
  cpu_per_slot: number;
  memory_reserve_gib: number;
  memory_per_slot_gib: number;
  min_slots: number;
  max_slots: number;
  formula_version: 1;
}

export interface CloudLabAutoscalingPolicy {
  revision: number;
  enabled: boolean;
  min_hosts: number;
  max_hosts: number;
  connection_id: string | null;
  ssh_connection_id: string | null;
  configuration: CloudLabAutoscalingConfiguration;
  deployment_profiles: string[];
  sandbox_storage_path: string | null;
  renewal_policy: RenewalPolicy;
  slot_policy: CloudLabSlotPolicy;
  scale_up_sustain_seconds: number;
  scale_down_idle_seconds: number;
  cooldown_seconds: number;
  scale_down_enabled: boolean;
}

export interface CloudLabAutoscalingRuntime {
  generation: number;
  pressure_since: string | null;
  idle_since: string | null;
  cooldown_until: string | null;
  last_decision: Record<string, unknown>;
  last_decision_at: string | null;
  blocker_code: string | null;
  blocker_message: string | null;
  updated_at: string | null;
  observed_at: string | null;
  queue_pressure: number;
  slot_status: CloudLabAutoscalingSlotStatus[];
}

export interface CloudLabAutoscalingSlotStatus {
  allocation_id: string;
  host: string | null;
  desired_slots: number | null;
  applied_slots: number | null;
  blocker_code: string | null;
}

export interface CloudLabAutoscalingMember {
  allocation_id: string;
  deployment_profile: string;
  state: 'provisioning' | 'active' | 'retiring' | 'released' | 'blocked';
  created_at: string;
  updated_at: string;
  host: string | null;
  slots_total: number | null;
  slots_desired: number | null;
  latest_operation_id: string | null;
  latest_operation_kind: string | null;
  latest_operation_state:
    | 'queued'
    | 'running'
    | 'retry_wait'
    | 'blocked'
    | 'unknown'
    | 'failed'
    | 'succeeded'
    | 'cancelled'
    | null;
  latest_operation_message: string | null;
  latest_acquire_operation_id: string | null;
  latest_acquire_state: CloudLabAutoscalingMember['latest_operation_state'];
  latest_acquire_message: string | null;
  /** Its lease is in the move-out window: its sandboxes are moving to other servers. */
  moving_out?: boolean;
}

export interface CloudLabProfileReservation {
  deployment_profile: string;
  allocation_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface CloudLabAutoscalingSnapshot {
  policy: CloudLabAutoscalingPolicy;
  runtime: CloudLabAutoscalingRuntime;
  members: CloudLabAutoscalingMember[];
  profile_reservations: CloudLabProfileReservation[];
  observed_at?: string;
}

export interface CloudLabPreferredNode {
  node_type: string;
  site: string;
  available: number;
  total: number;
  introduced_year: number;
  physical_cores: number;
  logical_threads: number;
  memory_gib: number;
}

export interface CloudLabPreferredNodesSnapshot {
  nodes: CloudLabPreferredNode[];
  observed_at: string;
  source: string;
  filters: {
    total_greater_than: number;
    minimum_introduced_year: number;
    minimum_physical_cores: number;
    minimum_memory_gib: number;
    general_purpose_only: boolean;
  };
}

export type CloudLabAutoscalingPolicyWrite = Omit<CloudLabAutoscalingPolicy, 'revision'>;

export interface UpdateCloudLabAutoscalingRequest {
  expected_revision: number;
  policy: CloudLabAutoscalingPolicyWrite;
}
export const getCloudLabAutoscaling = (signal?: AbortSignal) =>
  get<CloudLabAutoscalingSnapshot>('/compute-autoscaling/cloudlab', signal);
export const getCloudLabPreferredNodes = (signal?: AbortSignal) =>
  get<CloudLabPreferredNodesSnapshot>('/compute-availability/cloudlab', signal);
export const updateCloudLabAutoscaling = (body: UpdateCloudLabAutoscalingRequest) =>
  write<CloudLabAutoscalingSnapshot>('/compute-autoscaling/cloudlab', body, 'PUT');
