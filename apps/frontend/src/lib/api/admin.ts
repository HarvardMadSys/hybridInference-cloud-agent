// Admin client for the runner host pool.
//
// Every endpoint here returns the **whole pool** rather than the row it acted
// on, and the callers below hand that snapshot straight to state. That is the
// backend's design (`_snapshot` in `api/admin_hosts.py`) and it matters for the
// UI: a drain changes `slots_used` on one host and `status` on another the
// moment a claim lands, so a page that patched one row locally would show a
// pool that never existed.
//
// Paths are `/admin/agent/runner-hosts...` — the router's own prefix. Under the
// path-based deployment the browser calls `/agents/api/...`, and `config.apiBase`
// carries that prefix; nothing here should spell it out.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const API_BASE = config.apiBase;
const HOSTS = '/admin/agent/runner-hosts';
const RUNTIME_VERSIONS = '/admin/agent/runtime-versions';
const HOST_SETTINGS = '/admin/agent/host-settings';

/** Status values the control plane stores; see `cloud_agent/host_pool.py`. */
export type AgentHostStatus = 'enrolling' | 'online' | 'draining' | 'disabled' | 'offline';

/**
 * Why a CPU figure is absent. The vocabulary is fixed by the control plane
 * (`cloud_agent/capacity_protocol.py`) and every member means something
 * different to the operator, so the page maps each one to its own label rather
 * than collapsing them into a single "unknown".
 */
export type CpuMissingReason =
  | 'warming_up'
  | 'counter_reset'
  | 'source_unavailable'
  | 'invalid_sample';

/** Why a memory figure is absent. Memory has no warm-up: it is instantaneous. */
export type MemoryMissingReason = 'source_unavailable' | 'invalid_sample';

/**
 * Why a storage figure is absent.
 *
 * Wider than the memory vocabulary because storage has a *deployment* half:
 * the host agent samples one filesystem and the deploy script attests that the
 * filesystem it sampled is the one retained sandboxes actually write to.
 * `unverified` is that attestation missing or stale and is not a fault in the
 * measurement itself — the numbers may be perfectly good and still not be
 * reported, because nobody has proven yet which disk they describe.
 *
 * `not_reported` is the control plane's word for a runner that never sent the
 * sub-block at all, which is also what an old snapshot reads back as.
 */
export type StorageMissingReason =
  | 'not_reported'
  | 'disabled'
  | 'unsupported'
  | 'unverified'
  | 'source_unavailable'
  | 'invalid_sample';

export type CapacityInventoryStatus = 'available' | 'partial' | 'unavailable';

/**
 * One execution environment's retained-container count, as its runner saw it.
 *
 * Counts are of **containers**, not of chat memberships: a Fork adds a member
 * to a group that already holds one container, so counting memberships would
 * report a machine as busier every time somebody branched a conversation.
 */
export interface AgentHostCapacityInventory {
  /** Deployment namespace the counts were scoped to, or null when unscoped. */
  namespace: string | null;
  status: CapacityInventoryStatus;
  /** Null — never 0 — when the status is 'unavailable'. */
  running: number | null;
  stopped: number | null;
  /** Fixed keys: created, paused, restarting, removing, dead. */
  other_states: Record<string, number | null> | null;
  /** Bounded identifier, e.g. 'unscoped_legacy_containers' or 'docker_timeout'. */
  reason: string | null;
  /** Chat containers carrying no namespace label; counted in no total. */
  unscoped_legacy_containers: number | null;
  /** One sandbox id was seen on more than one container. */
  duplicate_sandbox_id: boolean;
}

/**
 * The last accepted host observation, with the server's own accounting.
 *
 * A null `capacity` on a host means no usable observation has ever arrived,
 * which is not the same as an idle machine. Nothing here is ever defaulted to
 * zero on the way to the screen.
 *
 * The `storage_*` block is **optional**, and every field in it may be absent
 * as well as null: the UI ships independently of the collector, so a control
 * plane older than this page omits the keys entirely instead of sending nulls.
 * `undefined` therefore has to read as "not measured" everywhere — it is not
 * zero full, and rendering it as `0%` would report an empty disk on a machine
 * nobody has asked about the disk at all.
 */
export interface AgentHostCapacity {
  schema_version: number;
  source: string;
  sample_id: string;
  linux_boot_id: string | null;
  /** 0..100, already normalized across all CPUs. Null when not measured. */
  cpu_percent: number | null;
  cpu_logical_count: number | null;
  /** The interval `cpu_percent` averages, in seconds. */
  cpu_window_seconds: number | null;
  cpu_missing_reason: CpuMissingReason | null;
  memory_total_bytes: number | null;
  memory_available_bytes: number | null;
  /** total - available. */
  memory_used_bytes: number | null;
  /** 0..100. */
  memory_used_percent: number | null;
  memory_missing_reason: MemoryMissingReason | null;
  /**
   * Block space of the filesystem that holds the sandboxes' writable layers,
   * in `df` terms: `statvfs` on the runner's own overlay root, whose reported
   * capacity is the filesystem carrying the upper layer.
   */
  storage_total_bytes?: number | null;
  /**
   * `f_bfree`. Includes the blocks the filesystem reserves for root, which is
   * why it is *not* the same number as `storage_available_bytes` and why used
   * space may never be computed as `total - available`.
   */
  storage_free_bytes?: number | null;
  /** `f_bavail`: what an unprivileged writer may still use. Excludes reserved blocks. */
  storage_available_bytes?: number | null;
  /**
   * `total - free`: the blocks actually in use. The reserved-but-unused
   * blocks are in neither this figure nor `available` — they are free, just
   * not to an unprivileged writer — which is why used plus available need
   * not add up to the total.
   */
  storage_used_bytes?: number | null;
  /**
   * `100 × used / (used + available)`, the denominator GNU `df` prints, and
   * left unrounded by the API so that a cell can round it and a tooltip can
   * keep a tenth.
   */
  storage_used_percent?: number | null;
  /** Probe identifier, e.g. `linux_overlay_root_statvfs`. A string, never an enum. */
  storage_source?: string | null;
  /** Host mount root the numbers came from, e.g. `/mnt/agent-docker`. Tooltip only. */
  storage_target?: string | null;
  /** Null when a figure was measured; see `StorageMissingReason` otherwise. */
  storage_missing_reason?: StorageMissingReason | null;
  inventory: AgentHostCapacityInventory | null;
  /** ISO-8601 UTC; when the control plane accepted the block. */
  received_at: string;
  /** ISO-8601 UTC; when the observation was taken. */
  sampled_at: string;
  /** Age at the server's read instant, in seconds. Never negative. */
  sample_age_seconds: number;
  freshness: 'fresh' | 'stale';
}

export interface AgentRunnerHost {
  host: string;
  active: boolean;
  last_worker_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  /**
   * Age of the last claim poll. A runner polls only *between* jobs, so a busy
   * machine can look quiet for as long as its longest job — which is why the UI
   * says "last polled" and never "offline".
   */
  seconds_since_seen: number;
  /**
   * Everything from here down arrived with the dynamic pool, and a host that
   * joined by polling has none of it. Optional in the same way the server
   * defaults it: the page an operator opens to fix a bad host must render that
   * host, not fail on it.
   */
  status: AgentHostStatus;
  /** Opaque automated-drain owner or its released idempotency receipt. */
  drain_fence_token?: string | null;
  /** Host state immediately before the current automated owner acquired it. */
  drain_fence_status_before?: 'online' | 'draining' | null;
  /** True while the automated owner keeps this host draining. */
  drain_fence_active?: boolean;
  /** Required V2 CAS generation, rotated whenever drain-relevant host state changes. */
  drain_fence_epoch: string;
  /** True while a Remove waits for this draining host's running attempts to end. */
  removal_pending?: boolean;
  /** True while that Remove also moves this host's sandboxes to other hosts. */
  removal_moves_out?: boolean;
  /** Sandboxes this host can still move out while its removal moves them. */
  sandboxes_left?: number | null;
  /** Sandboxes on this host that can never move, while its removal moves the others. */
  sandboxes_unmovable?: number | null;
  /** True when the administrator agreed that sandboxes which cannot move stay. */
  removal_leaves_unmoved?: boolean;
  /**
   * True when the host redeemed an enrollment token, which is the difference
   * between a name that is a scheduling label and a name that is an identity.
   */
  enrolled: boolean;
  labels: Record<string, unknown>;
  capabilities: string[];
  slots_total: number | null;
  slots_used: number;
  /**
   * Turns at once an operator asked this host to run, set from the Servers
   * page, or null when the host runs its own default. The host takes it up on
   * its next heartbeat and reports it back as `slots_total`; the scheduler
   * admits against the lower of the two meanwhile. Absent from a control plane
   * older than this field; the page reads it as null.
   */
  slots_desired?: number | null;
  /**
   * Why this host is claiming nothing despite having room — it could not start
   * the process a turn runs in, repeatedly. Empty means nothing is wrong; null
   * means this host does not report it.
   */
  launch_error?: string | null;
  runner_version: string | null;
  sandbox_manager_url: string | null;
  last_heartbeat_at: string | null;
  /**
   * The container runtime this host's jobs actually execute under. `''` is a
   * positive statement — shared-kernel runc — where null means the host has
   * never said.
   */
  sandbox_runtime: string | null;
  /** Digest of the sandbox image this host launches, as last reported. */
  sandbox_image_digest: string | null;
  /** The *default* CLI version per runtime id: what a job runs unselected. */
  runtime_versions: Record<string, string> | null;
  /**
   * Every CLI version this host's image holds, keyed by runtime id, default
   * first. The menu an admin may select from — the control plane cannot serve
   * a version absent from it, because it is not in the image.
   */
  runtime_versions_available: Record<string, string[]> | null;
  /**
   * Generation of this host's capacity data; rotated by re-enrollment. A client
   * uses it to drop a curve that belongs to the machine this one replaced.
   */
  capacity_epoch: string | null;
  /**
   * The last accepted observation, or null when none has ever arrived — which
   * is not the same as an idle machine and must not be rendered as zero.
   */
  capacity: AgentHostCapacity | null;
  /**
   * Runtime settings set for this server on the admin page, by key. They win
   * over the global values. Absent from a control plane older than settings.
   */
  settings_desired?: HostSettingValues;
  /**
   * The runtime settings this host reported applying on its last heartbeat, or
   * null when it never has — a runner that predates admin settings.
   */
  settings_applied?: HostSettingValues | null;
  /**
   * What this host's own `.env` (or the built-in default) gives each setting
   * with no admin value. Clearing an admin value returns the host to this.
   */
  settings_local?: HostSettingValues | null;
  /** Archive-policy revision this host's sandbox manager confirmed applying. */
  sandbox_archive_settings_applied_revision?: number | null;
  /**
   * True when this host can be given untrusted (Kata) work and reports no
   * container disk quota, so a new untrusted sandbox's writable layer is
   * unbounded. A recommendation only; nothing is refused. Absent from a
   * control plane older than the warning; the page reads it as false.
   */
  untrusted_without_disk_quota?: boolean;
}

/** Which unit a runtime setting's whole number counts. */
export type HostSettingUnit = 'seconds' | 'MiB' | 'KiB' | 'count' | 'millicores';

/**
 * One runtime setting an admin may set, as the control plane defines it
 * (`cloud_agent_shared/host_settings.py`). The page renders from this list
 * rather than its own copy, so a bound can never drift from the server's.
 */
export interface HostSettingDefinition {
  key: string;
  /** The host environment variable it overrides. */
  env: string;
  label: string;
  description: string;
  unit: HostSettingUnit;
  /** The built-in value when neither an admin nor the host's `.env` sets it. */
  default: number;
  minimum: number;
  maximum: number;
  /** Which host process applies it. */
  scope: 'runner' | 'sandbox_manager' | 'both';
}

/** Runtime setting values by key. */
export type HostSettingValues = Record<string, number>;

export interface AgentRunnerHostPool {
  execution_profile?: string;
  hosts: AgentRunnerHost[];
  /** New-chat placement host, or null when any runner may materialize one. */
  active_host: string | null;
  /**
   * The deployment's chosen CLI version per runtime id. A runtime absent here
   * runs whatever its claiming host reports as the image default.
   */
  runtime_versions: Record<string, string>;
  /**
   * Runtime settings set for every server. A server's own value wins over
   * these; a setting set nowhere uses each host's own. Absent from a control
   * plane older than settings.
   */
  host_settings?: HostSettingValues;
  /** Every runtime setting an admin may set, with its unit and bounds. */
  host_setting_definitions?: HostSettingDefinition[];
  /**
   * The database instant this read was taken at, by the server's clock. Every
   * age on the page is measured from it, plus locally observed elapsed time,
   * rather than from the browser's wall clock.
   */
  observed_at: string | null;
}

/** The windows the capacity-history endpoint serves. */
export type CapacityRange = '1h' | '6h' | '24h';

/**
 * One minute of history: an observation, or an explicit gap.
 *
 * A bucket nobody observed carries nulls rather than zeroes, and that is the
 * whole point of the shape — a machine measured at 3% and a machine nobody
 * measured are different facts.
 */
export interface AgentHostCapacityPoint {
  /** ISO-8601 UTC; the start of the minute. */
  bucket_at: string;
  /** Null means this minute was not observed — an explicit gap. */
  sampled_at: string | null;
  linux_boot_id: string | null;
  cpu_percent: number | null;
  cpu_logical_count: number | null;
  cpu_window_seconds: number | null;
  cpu_missing_reason: CpuMissingReason | null;
  memory_total_bytes: number | null;
  memory_available_bytes: number | null;
  memory_used_bytes: number | null;
  memory_used_percent: number | null;
  memory_missing_reason: MemoryMissingReason | null;
}

export interface AgentHostCapacityHistory {
  host: string;
  capacity_epoch: string | null;
  /** ISO-8601 UTC, inclusive; floored to the minute. */
  since: string;
  /** ISO-8601 UTC, inclusive; floored to the minute. */
  until: string;
  /** ISO-8601 UTC; when the control plane read this window. */
  observed_at: string;
  /** Bucket width. */
  resolution_seconds: number;
  /** Each point is one observation per minute, not a continuous average. */
  semantics: 'sampled_observation';
  points: AgentHostCapacityPoint[];
}

export async function getAgentRunnerHosts(signal?: AbortSignal): Promise<AgentRunnerHostPool> {
  return jsonOrThrow<AgentRunnerHostPool>(await fetchWithAuth(API_BASE, HOSTS, { signal }));
}

/**
 * Read one host's stored CPU and memory history.
 *
 * Separate from the pool on purpose: the table is the page's main information
 * and it loads on every poll, while a 24-hour curve is wanted only when an
 * operator opens one machine.
 *
 * `host` is the enrollment name — the same identity the actions use, not the
 * physical-group label. An unknown host is a 404 and an unsupported range a
 * 422, both surfaced by `jsonOrThrow` rather than swallowed here.
 */
export async function getAgentRunnerHostCapacityHistory(
  host: string,
  range: CapacityRange,
  signal?: AbortSignal,
): Promise<AgentHostCapacityHistory> {
  const query = new URLSearchParams({ range }).toString();
  const resp = await fetchWithAuth(
    API_BASE,
    `${HOSTS}/${encodeURIComponent(host)}/capacity-history?${query}`,
    { signal },
  );
  return jsonOrThrow<AgentHostCapacityHistory>(resp);
}

/** Pin new-chat placement; existing retained groups stay on their owning host. */
export async function setActiveAgentRunnerHost(host: string | null): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/active`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ host }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Choose the agent CLI version this deployment runs for one runtime; pass null
 * to go back to the image default.
 *
 * Bounded by what the pool's images contain — a version no host offers is
 * refused with a 404 rather than stored, because the claim would silently fall
 * back and the page would then show a version that never runs.
 *
 * Not retroactive: a thread already running keeps the version it started on,
 * because moving a live harness session between versions costs it its memory.
 */
export async function setAgentRuntimeVersion(
  runtime: string,
  version: string | null,
): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, RUNTIME_VERSIONS, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ runtime, version }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Drop a decommissioned host from the list. Housekeeping only — a runner still
 * polling there re-adds itself within seconds.
 */
export async function forgetAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}`, {
    method: 'DELETE',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/** Stop this host taking new work; let what it has finish. */
export async function drainAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/drain`, {
    method: 'POST',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/** Put a drained host back to work; this also cancels a pending removal. */
export async function undrainAgentRunnerHost(host: string): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/undrain`, {
    method: 'POST',
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Advertised on the heartbeat by a runner that takes its limit from
 * `slots_desired`. Mirrors `HOST_CONCURRENCY_CAPABILITY` in
 * `cloud_agent_host/shared_protocol.py`; a host without it keeps the number it
 * was deployed with until it is redeployed.
 */
export const HOST_CONCURRENCY_CAPABILITY = 'host-concurrency-v1';

/**
 * The limit an operator may ask for. Two is the shared-live floor — a fork's
 * sibling runs beside its parent — and 128 is a typo guard; both mirror
 * `HOST_CONCURRENCY_MIN_SLOTS` / `HOST_CONCURRENCY_MAX_SLOTS` on the server,
 * which refuses anything outside them. Neither says what a machine can carry.
 */
export const MIN_HOST_SLOTS = 2;
export const MAX_HOST_SLOTS = 128;

/**
 * Ask one host to run this many turns at once; pass null to return it to its
 * own default.
 *
 * Nothing here starts or stops anything: the host reads the number off its
 * next heartbeat answer and serves it as its limit, starting a process per
 * turn as it claims one. Turns already running always finish. The scheduler
 * admits against the lower of the saved and reported numbers meanwhile, so a
 * lowered number takes effect at once and a raised one when the host confirms.
 *
 * **Resolving is not applying.** The response says the number was saved; the
 * host row in it says whether the host has taken it up.
 */
export async function setAgentRunnerHostSlots(
  host: string,
  slots: number | null,
): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/slots`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ slots }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Advertised on the heartbeat by a runner that applies the runtime settings set
 * on this page and reports what it applied. Mirrors `HOST_SETTINGS_CAPABILITY`
 * in `cloud_agent_shared/shared_protocol.py`; a host without it keeps its
 * `.env` values until it is redeployed.
 */
export const HOST_SETTINGS_CAPABILITY = 'host-settings-v1';

/**
 * Set or clear runtime settings for every server. Each value is a whole number
 * inside its setting's bounds, or null to clear it so each host uses its own.
 * Settings not named keep their values.
 *
 * **Saving is not applying.** Each host takes the values up from its next
 * heartbeat answer; the host rows in the response say which have.
 */
export async function setAgentHostSettings(
  settings: Record<string, number | null>,
): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, HOST_SETTINGS, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Set or clear runtime settings for one server, over the global values. Null
 * clears one, and the server goes back to the global value, or its own.
 */
export async function setAgentRunnerHostSettings(
  host: string,
  settings: Record<string, number | null>,
): Promise<AgentRunnerHostPool> {
  const resp = await fetchWithAuth(API_BASE, `${HOSTS}/${encodeURIComponent(host)}/settings`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ settings }),
  });
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/**
 * Revoke this host's credential and take it out of the pool.
 *
 * Unforced, the host first moves its sandboxes to other hosts when they can
 * move: it stops taking new work, reports `removal_pending` and
 * `removal_moves_out`, and the control plane revokes it once none is left and
 * its running attempts end. When some would stay on it, the request fails with
 * the `no_move_target` code (nowhere to move them now) or `cannot_move` (some
 * never can) unless `allowUnmoved`; then whatever can move still moves first,
 * and the host is revoked once its running attempts end.
 *
 * `force` is off by default for a reason worth repeating at the call site:
 * attempts still running on that host are ended. Their leases expire at once,
 * and the work is requeued exactly once, or a chat whose turn may already have
 * run is marked lost.
 */
export async function removeAgentRunnerHost(
  host: string,
  force = false,
  allowUnmoved = false,
): Promise<AgentRunnerHostPool> {
  const params = new URLSearchParams();
  if (force) params.set('force', 'true');
  if (allowUnmoved) params.set('allow_unmoved', 'true');
  const query = params.size > 0 ? `?${params.toString()}` : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `${HOSTS}/${encodeURIComponent(host)}/remove${query}`,
    {
      method: 'POST',
    },
  );
  return jsonOrThrow<AgentRunnerHostPool>(resp);
}

/** What an Audit log entry is about; the page filters by it. */
export type AdminAuditCategory = 'settings' | 'servers' | 'measurements' | 'content' | 'database';

/** One recorded admin action (`GET /admin/agent/audit-log`). */
export interface AdminAuditLogEntry {
  id: number;
  /** Dotted action name, e.g. `agent.runner_host.drain`. */
  action: string;
  /** The filter this action falls under, or null for none. */
  category: AdminAuditCategory | null;
  /** The administrator who acted; null on rows older than the column. */
  actor_id: string | null;
  admin_ip: string;
  /** The user acted upon, such as the owner of content an admin read. */
  target_user_id: string | null;
  details: Record<string, unknown> | null;
  success: boolean;
  created_at: string;
}

export interface AdminAuditLogResponse {
  entries: AdminAuditLogEntry[];
  /** Pass as `before` for the next, older page; null when there is none. */
  next_before: number | null;
}

/** Recorded admin actions, newest first; polling reads are never included. */
export async function listAdminAuditLog(
  { category, before }: { category?: AdminAuditCategory | null; before?: number | null } = {},
  signal?: AbortSignal,
): Promise<AdminAuditLogResponse> {
  const query = new URLSearchParams();
  if (category) query.set('category', category);
  if (before) query.set('before', String(before));
  const qs = query.toString();
  const suffix = qs ? `?${qs}` : '';
  return jsonOrThrow<AdminAuditLogResponse>(
    await fetchWithAuth(API_BASE, `/admin/agent/audit-log${suffix}`, { signal }),
  );
}
