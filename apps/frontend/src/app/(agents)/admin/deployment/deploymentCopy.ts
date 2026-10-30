// What administrators read on the Deployment page. The API speaks in contract
// names (configuration keys, operation and service ids, catalog item ids); those
// stay internal and are only ever shown through the names below.

import type { CatalogItem } from '@/lib/api/adminManagement';

/** Contract names and plumbing that must never reach a label or an instruction. */
export const INTERNAL_TEXT =
  /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|\b[a-z]+_[a-z0-9_]+\b|\.env\b|AGENTS\.md|python -m|deploy\.agent|--[a-z]/;

/** Text from the API, shown only when it reads as plain language. */
export function readable(text: string | null | undefined): string {
  return text && !INTERNAL_TEXT.test(text) ? text : '';
}

function sentence(words: string): string {
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** A name nobody wrote copy for yet, in words rather than as an identifier. */
export function humanize(name: string): string {
  return sentence(
    name
      .replace(/^AGENT_/, '')
      .replace(/[._-]+/g, ' ')
      .toLowerCase()
      .trim(),
  );
}

export const ROLE_NAMES: Record<string, string> = {
  'control-plane': 'Control plane',
  host: 'Server',
};

const SERVICE_NAMES: Record<string, string> = {
  'control-plane': 'API',
  'mcp-relay': 'MCP listener',
  web: 'web app',
  edge: 'edge proxy',
  pgadmin: 'pgAdmin',
  runner: 'runner',
  'sandbox-manager': 'sandbox manager',
  tunnels: 'tunnels',
  'gateway-tunnel': 'gateway tunnel',
  'mcp-tunnel': 'MCP tunnel',
  'control-plane-tunnel': 'control-plane tunnel',
};

export function serviceName(service: string): string {
  return SERVICE_NAMES[service] ?? service.replace(/[_-]+/g, ' ');
}

/** "the API, MCP listener and web app" */
export function serviceList(services: string[]): string {
  return listing(services.map(serviceName));
}

const PROCESS_NAMES: Record<string, string> = { api: 'API', mcp: 'MCP listener' };

/** A reporting process by what it is: "api" is the API. */
export function processKind(role: string): string {
  return PROCESS_NAMES[role] ?? 'process';
}

/** "mcp@cp-1", as a process reports itself, in words: "the MCP listener on cp-1". */
export function processName(reported: string): string {
  const [role, machine] = reported.split('@');
  return machine ? `the ${processKind(role)} on ${machine}` : `the ${processKind(role)}`;
}

/** "a, b and c" */
export function listing(names: string[]): string {
  const unique = [...new Set(names)];
  if (unique.length <= 1) return unique.join('');
  return `${unique.slice(0, -1).join(', ')} and ${unique.at(-1)}`;
}

interface KeyCopy {
  label: string;
  help: string;
  choices?: Record<string, string>;
}

const YES_NO = { true: 'Yes', false: 'No', True: 'Yes', False: 'No', 1: 'On', 0: 'Off' };

/** Each machine configuration value by what it does. */
const KEY_COPY: Record<string, KeyCopy> = {
  AGENT_CONTROL_PLANE_BIND: {
    label: 'API listen address',
    help: 'The interface the API accepts connections on, for example 127.0.0.1.',
  },
  AGENT_CONTROL_PLANE_PORT: { label: 'API port', help: 'The port the API listens on.' },
  AGENT_MCP_RELAY_BIND: {
    label: 'MCP listener address',
    help: 'The interface MCP clients connect to, for example 127.0.0.1.',
  },
  AGENT_MCP_RELAY_PORT: { label: 'MCP listener port', help: 'The port MCP clients connect to.' },
  AGENT_WEB_BIND: {
    label: 'Web app listen address',
    help: 'The interface the web app is served on, for example 127.0.0.1.',
  },
  AGENT_WEB_PORT: { label: 'Web app port', help: 'The port the web app is served on.' },
  AGENT_EDGE_BIND: {
    label: 'Edge proxy listen address',
    help: 'The interface the bundled edge proxy accepts visitors on, for example 0.0.0.0.',
  },
  AGENT_EDGE_PORT: { label: 'Edge proxy port', help: 'The port visitors reach the edge proxy on.' },
  AGENT_NETWORK_NAME: {
    label: 'Container network',
    help: 'The private network the services share on this machine.',
  },
  COMPOSE_PROFILES: {
    label: 'Bundled services',
    help: 'Optional services that run alongside the application on this machine.',
    choices: {
      '': 'None',
      edge: 'Edge proxy',
      pgadmin: 'pgAdmin',
      'edge,pgadmin': 'Edge proxy and pgAdmin',
    },
  },
  AGENT_PGADMIN_BOOTSTRAP_URL: {
    label: 'pgAdmin setup address',
    help: 'Where the control plane reaches pgAdmin during first-time setup, for example http://pgadmin:80.',
  },
  AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL: {
    label: 'pgAdmin public setup address',
    help: 'Where an administrator’s browser opens pgAdmin during first-time setup, for example https://pgadmin.example.org.',
  },
  AGENT_PGADMIN_BOOTSTRAP_DIR: {
    label: 'pgAdmin setup folder',
    help: 'The folder on this machine that holds pgAdmin’s first-time setup files.',
  },
  AGENT_PGADMIN_COOKIE_SECURE: {
    label: 'Secure pgAdmin cookies',
    help: 'Send pgAdmin’s sign-in cookie over HTTPS only.',
    choices: YES_NO,
  },
  AGENT_PGADMIN_URL: {
    label: 'Allowed pgAdmin address',
    help: 'A pgAdmin service the saved pgAdmin link may point to, for example https://pgadmin.example.org.',
  },
  AGENT_OPERATION_PROFILING_MODE: {
    label: 'Profiling ceiling',
    help: 'The most profiling this machine allows. The Admin profiling control works within it.',
    choices: { disabled: 'Off', environment: 'Set on the machine', admin: 'Controlled from Admin' },
  },
  AGENT_OPERATION_PROFILING_DEPLOYMENT: {
    label: 'Profiling deployment name',
    help: 'How this deployment is named in collected profiles.',
  },
  AGENT_WEB_VARIANT: {
    label: 'Web app location',
    help: 'Whether the web app is served at the site root or under /agents.',
    choices: { root: 'Site root', agents: 'Under /agents' },
  },
  AGENT_RUNNER_STOP_GRACE: {
    label: 'Runner stop grace period',
    help: 'How long the runner may finish work before it is stopped, for example 30s.',
  },
  AGENT_SANDBOX_MANAGER_STOP_GRACE: {
    label: 'Sandbox manager stop grace period',
    help: 'How long the sandbox manager may finish work before it is stopped, for example 30s.',
  },
  AGENT_HOST_CAPACITY_ENABLED: {
    label: 'Capacity reporting',
    help: 'Report this server’s free capacity so turns are placed where there is room.',
    choices: YES_NO,
  },
  AGENT_SANDBOX_IMAGE_PULL: {
    label: 'Pull sandbox images',
    help: 'Download missing sandbox images instead of using only images already on the server.',
    choices: YES_NO,
  },
  AGENT_RUNTIME_DOWNLOAD_NETWORK: {
    label: 'Download network',
    help: 'The network agent runtimes are downloaded through.',
  },
  AGENT_SANDBOX_RUNTIME: {
    label: 'Sandbox runtime',
    help: 'Only runtimes this server’s local policy allows are offered.',
    choices: {
      runc: 'Standard containers',
      'io.containerd.kata.v2': 'Kata virtual machines (containerd)',
      kata: 'Kata virtual machines',
    },
  },
  AGENT_EGRESS_DNS_SERVER: {
    label: 'Sandbox DNS server',
    help: 'The resolver sandboxes use for outbound names, for example 10.0.0.53.',
  },
  AGENT_EGRESS_CHAT_NETWORK_POOL: {
    label: 'Sandbox address pool',
    help: 'Address ranges sandbox networks are allocated from, for example 10.88.0.0/16.',
  },
  AGENT_EGRESS_PROTECTED_CIDRS: {
    label: 'Protected address ranges',
    help: 'Ranges sandboxes may never reach, for example 10.0.0.0/8. Ranges can be added, not removed.',
  },
  AGENT_SANDBOX_MANAGER_TOKEN: {
    label: 'Sandbox manager credential',
    help: 'Usually replaced with its rotation under Keys and credentials.',
  },
  AGENT_SANDBOX_MANAGER_TOKEN_NEXT: {
    label: 'Incoming sandbox manager credential',
    help: 'Accepted alongside the current credential while a rotation is under way.',
  },
};

const KIND_NAMES: Record<string, string> = {
  bind: 'Listen address',
  port: 'Port',
  url: 'Address',
  path: 'Folder',
  name: 'Name',
  secret: 'Secret',
};

export function keyLabel(name: string, kind = ''): string {
  return KEY_COPY[name]?.label ?? (humanize(name) || KIND_NAMES[kind] || 'Value');
}

export function keyHelp(name: string): string {
  return KEY_COPY[name]?.help ?? '';
}

export function choiceLabel(name: string, choice: string): string {
  return KEY_COPY[name]?.choices?.[choice] ?? (choice ? humanize(choice) : 'None');
}

interface ItemCopy {
  label: string;
  description: string;
}

/** Catalog items in an administrator's words; the API's own label is the fallback. */
const ITEM_COPY: Record<string, ItemCopy> = {
  'hosts.runtime_settings': {
    label: 'Server runtime settings',
    description: 'Runtime limits and behavior, for every server or just one.',
  },
  'hosts.slots': {
    label: 'Turn slots per server',
    description: 'How many turns each server runs at once.',
  },
  'hosts.identity': {
    label: 'Server identity and enrollment',
    description: 'Enrolling, removing and re-enrolling servers.',
  },
  'hosts.topology': {
    label: 'Runner and sandbox services',
    description: 'Stop grace periods, capacity reporting and image downloads on each server.',
  },
  'hosts.sandbox_boundary': {
    label: 'Sandbox isolation and network',
    description:
      'Only values the server’s local policy allows can be chosen, and protection can only grow.',
  },
  'hosts.sandbox_manager_token': {
    label: 'Sandbox manager credential',
    description: 'The credential the control plane presents to each server.',
  },
  'hosts.kata': {
    label: 'Kata runtime version',
    description: 'A reviewed version is installed after the server finishes its running turns.',
  },
  'hosts.tunnel_keys': {
    label: 'Tunnel keys',
    description: 'The keys each server’s tunnels sign in with.',
  },
  'deployment.control_plane_listeners': {
    label: 'Addresses and ports',
    description:
      'Where the API, MCP listener, web app and edge proxy listen. A new address is kept only if it answers.',
  },
  'deployment.optional_services': {
    label: 'Bundled services',
    description: 'Turns the bundled pgAdmin and edge proxy on or off.',
  },
  'deployment.pgadmin': {
    label: 'pgAdmin',
    description: 'First-time setup addresses, cookie security and the pgAdmin services allowed.',
  },
  'deployment.profiling': {
    label: 'Operation profiling',
    description: 'The most profiling this deployment allows.',
  },
  'deployment.web_routing': {
    label: 'Web app location',
    description: 'Whether the web app is served at the site root or under /agents.',
  },
  'deployment.compose_override': {
    label: 'Custom container overrides',
    description: 'Shown when a machine has them; changed on the machine itself.',
  },
  'deployment.mounted_files': {
    label: 'Mounted keys and compute profiles',
    description: 'The GitHub App key and compute profiles are managed on their own pages.',
  },
  'compute.setup_prefill': {
    label: 'Compute setup addresses',
    description: 'Suggested addresses for the compute setup form.',
  },
  'release.desired': {
    label: 'Desired release',
    description: 'Chosen from releases every machine has verified.',
  },
  'release.identity': {
    label: 'Running release',
    description: 'What each machine reports it runs.',
  },
  'release.image_inputs': {
    label: 'Agent image contents',
    description: 'Part of each release. The agent version new chats use is set in Settings.',
  },
  'deployment.agent_install': {
    label: 'Deployment agent installation',
    description: 'Set up on each machine when its agent is installed.',
  },
  'deployment.legacy_names': {
    label: 'Outdated configuration entries',
    description: 'Entries a machine still has but no longer reads; remove them on the machine.',
  },
  'security.admin_credentials': {
    label: 'Administrator API credentials',
    description: 'Named, revocable credentials for automation.',
  },
  'security.admin_token': {
    label: 'Recovery administrator token',
    description: 'Full administrator access when normal sign-in is unavailable.',
  },
  'security.settings_root_key': {
    label: 'Settings root key',
    description: 'Encrypts saved settings secrets. Never stored in the database.',
  },
  'security.key_rotation': {
    label: 'Signing key and sandbox manager credential',
    description: 'Rotated with every server moving over before the previous one is retired.',
  },
  'security.credential_keys': {
    label: 'Credential encryption keys',
    description: 'Every stored credential is re-encrypted before the previous key is dropped.',
  },
  'database.connection': {
    label: 'Database connection',
    description: 'A new address or account for the same database, proven before it is used.',
  },
  'database.migration': {
    label: 'Move to another database',
    description: 'Copies the data to an empty database, verifies it and switches over.',
  },
};

export function itemLabel(item: Pick<CatalogItem, 'id' | 'label'> | string): string {
  const id = typeof item === 'string' ? item : item.id;
  const fallback = typeof item === 'string' ? '' : readable(item.label);
  return ITEM_COPY[id]?.label ?? (fallback || humanize(id.split('.').at(-1) ?? id));
}

export function itemDescription(item: Pick<CatalogItem, 'id' | 'description'>): string {
  return ITEM_COPY[item.id]?.description ?? readable(item.description);
}

/** What each machine step does, as the activity list names it. */
const OPERATION_NAMES: Record<string, string> = {
  apply_config: 'Apply configuration',
  rollback_config: 'Restore an earlier configuration',
  restart_services: 'Restart services',
  release_prepare: 'Prepare release',
  release_prepare_database: 'Update the database for the release',
  release_activate: 'Switch to release',
  release_abandon: 'Discard prepared release',
  kata_install: 'Install Kata runtime',
  rotation_step: 'Key rotation step',
  database_check: 'Check database connection',
  database_switch: 'Switch database connection',
  database_migrate: 'Move database',
};

export function operationName(operation: string): string {
  return OPERATION_NAMES[operation] ?? humanize(operation);
}

const STEP_STATES: Record<string, string> = {
  pending: 'Waiting for the machine',
  delivered: 'Received by the machine',
  running: 'In progress',
  succeeded: 'Done',
  failed: 'Failed',
  rejected: 'Refused by the machine',
  expired: 'Expired before the machine picked it up',
  cancelled: 'Cancelled',
};

export function stepState(state: string): string {
  return STEP_STATES[state] ?? humanize(state);
}

const SCOPE_NAMES: Record<string, string> = {
  admin: 'Administrator',
  deployment: 'Deployments',
};

export function scopeName(scope: string): string {
  return SCOPE_NAMES[scope] ?? humanize(scope);
}

/** "1 release", "3 releases" */
export function count(n: number, word: string): string {
  return `${n} ${n === 1 ? word : `${word}s`}`;
}

/** "abc1234def56", a release or commit as people compare them. */
export function shortVersion(commit: string | undefined | null): string {
  return commit ? commit.slice(0, 12) : '';
}

export function when(timestamp: string | null | undefined): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  return Number.isNaN(date.getTime())
    ? ''
    : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}
