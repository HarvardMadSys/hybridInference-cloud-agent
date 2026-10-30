import type { DeploymentWorkflow } from '@/lib/api/adminManagement';

export interface WorkflowDefinition {
  title: string;
  steps: { id: string; label: string }[];
}

/** Every workflow by its plain name, and its steps in the order they run. */
export const WORKFLOWS: Record<string, WorkflowDefinition> = {
  rollout: {
    title: 'Release rollout',
    steps: [
      { id: 'prepare', label: 'Prepare every machine' },
      { id: 'prepare_database', label: 'Migrate the database' },
      { id: 'activate_hosts', label: 'Activate servers' },
      { id: 'activate_control_plane', label: 'Activate the control plane' },
    ],
  },
  admin_token: {
    title: 'ADMIN_TOKEN',
    steps: [
      { id: 'apply', label: 'Write it on each control-plane machine' },
      { id: 'verify', label: 'Every process uses it' },
    ],
  },
  root_key: {
    title: 'Settings root key',
    steps: [
      { id: 'separate', label: 'Separate it from the session secret' },
      { id: 'stage', label: 'Hold the new key and the old one' },
      { id: 'reencrypt', label: 'Re-encrypt saved secrets' },
      { id: 'retire', label: 'Drop the old key' },
    ],
  },
  credential_key: {
    title: 'Credential encryption key',
    steps: [
      { id: 'stage', label: 'Every process holds both keys' },
      { id: 'reencrypt', label: 'Re-encrypt stored credentials' },
      { id: 'switch_index', label: 'Build lookups with the new key' },
      { id: 'reindex', label: 'Rebuild environment fingerprints' },
      { id: 'retire', label: 'Drop the old key' },
    ],
  },
  control_signing: {
    title: 'Host and worker signing key',
    steps: [
      { id: 'stage', label: 'The new key signs' },
      { id: 'adopt', label: 'Every process signs with it' },
      { id: 'renew_hosts', label: 'Every server renews its credential' },
      { id: 'drain_tokens', label: 'Earlier turns finish' },
      { id: 'retire', label: 'Retire the previous key' },
    ],
  },
  sandbox_manager_token: {
    title: 'Sandbox manager credential',
    steps: [
      { id: 'stage', label: 'Each server accepts the next credential' },
      { id: 'verify_next', label: 'Proved on every server' },
      { id: 'switch', label: 'The control plane changes to it' },
      { id: 'adopt', label: 'Every process uses it' },
      { id: 'promote', label: 'Each server drops the old one' },
      { id: 'verify', label: 'Proved again' },
    ],
  },
  tunnel_keys: { title: 'Tunnel SSH keys', steps: [] },
  database_connection: {
    title: 'Database connection',
    steps: [
      { id: 'probe', label: 'Prove it is this same database' },
      { id: 'switch_leader', label: 'Switch the first machine' },
      { id: 'switch_others', label: 'Switch the other machines' },
      { id: 'verify', label: 'Every process uses it' },
    ],
  },
  database_migration: {
    title: 'Database move',
    steps: [
      { id: 'maintenance', label: 'Every process stops accepting changes' },
      { id: 'migrate', label: 'Fence, copy, verify, switch' },
      { id: 'switch_others', label: 'Switch the other machines' },
      { id: 'verify', label: 'Every process uses the new database' },
    ],
  },
};

/** The steps of a server's tunnel key change, in order. */
export const TUNNEL_PHASES = [
  { id: 'generate', label: 'New keys' },
  { id: 'authorize', label: 'Accepted on the far end' },
  { id: 'switch', label: 'Server switched' },
  { id: 'confirm', label: 'Heartbeat over the new tunnel' },
  { id: 'record', label: 'Recorded' },
  { id: 'revoke', label: 'Old keys revoked' },
];

export const WORKFLOW_STATES: Record<DeploymentWorkflow['state'], string> = {
  running: 'Running',
  waiting: 'Waiting',
  succeeded: 'Finished',
  failed: 'Stopped',
  cancelled: 'Cancelled',
};

/** Rotations that continue from the step that stopped; the others start again. */
export const RETRYABLE = new Set([
  'root_key',
  'credential_key',
  'control_signing',
  'sandbox_manager_token',
  'tunnel_keys',
]);

export type StepState = 'done' | 'current' | 'pending';

/** Where each step stands. A finished workflow's steps are all done; a stopped one's current. */
export function stepStates(workflow: DeploymentWorkflow): { id: string; state: StepState }[] {
  const steps = WORKFLOWS[workflow.kind]?.steps ?? [];
  const at = steps.findIndex((step) => step.id === workflow.step);
  return steps.map((step, index) => ({
    id: step.id,
    state:
      workflow.state === 'succeeded' || (at >= 0 && index < at)
        ? 'done'
        : index === at
          ? 'current'
          : 'pending',
  }));
}

export function isActive(workflow: DeploymentWorkflow): boolean {
  return workflow.state === 'running' || workflow.state === 'waiting';
}
