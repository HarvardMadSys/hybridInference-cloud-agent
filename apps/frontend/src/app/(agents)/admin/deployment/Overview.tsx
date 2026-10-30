'use client';

import type {
  DeploymentAgent,
  DeploymentWorkflow,
  InstanceView,
  ManagementStatus,
} from '@/lib/api/adminManagement';

import type { DeploymentTab } from './ConfigurationMap';
import { listing, processKind, processName, shortVersion } from './deploymentCopy';
import { Empty, FieldRow, GroupCard, Pill, type Tone } from './deploymentUi';
import { WORKFLOW_STATES, isActive } from './workflowSteps';
import { workflowTitle } from './WorkflowProgress';

/** Which page area shows an operation of this kind. */
export function workflowTab(kind: string): DeploymentTab {
  if (kind === 'rollout') return 'releases';
  if (kind.startsWith('database_')) return 'database';
  return KEY_KINDS.has(kind) ? 'keys' : 'activity';
}

const KEY_KINDS = new Set([
  'admin_token',
  'root_key',
  'credential_key',
  'control_signing',
  'sandbox_manager_token',
  'tunnel_keys',
]);

function sentence(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function adoption(status: ManagementStatus): [string, Tone, string] {
  const { reporting, adopted } = status.settings;
  if (reporting === 0) {
    return [
      'Not known',
      'neutral',
      'No running process has reported yet, so whether the saved settings are in use is not known.',
    ];
  }
  if (adopted === reporting) {
    return [
      'In use',
      'success',
      reporting === 1
        ? 'The one running process uses the saved settings.'
        : `All ${reporting} running processes use the saved settings.`,
    ];
  }
  return [
    'Partly in use',
    'warning',
    `${adopted} of ${reporting} running processes use the saved settings; the others have not picked them up yet.`,
  ];
}

function processState(instance: InstanceView): [string, Tone] {
  if (!instance.current)
    return instance.stopped ? ['Stopped', 'neutral'] : ['Not reporting', 'warning'];
  return instance.adopted ? ['Up to date', 'success'] : ['Earlier settings', 'warning'];
}

function SettingsStatus({
  status,
  agents,
}: {
  status: ManagementStatus;
  agents: DeploymentAgent[];
}) {
  const [label, tone, text] = adoption(status);
  const restart = Object.entries(status.settings.restart_required);
  const processes = restart.flatMap(([, where]) => where);
  const online = agents.filter((agent) => agent.online).length;
  const sessions = status.unidentified_sessions.length;
  return (
    <GroupCard title="Status" icon="review">
      <FieldRow
        label="Saved settings"
        help="Changed on the Settings page; each process reports what it uses."
      >
        <div className="space-y-1.5">
          <Pill tone={tone}>{label}</Pill>
          <p className="text-sm leading-6 text-foreground">{text}</p>
        </div>
      </FieldRow>
      {restart.length > 0 && (
        <FieldRow label="Waiting for a restart">
          <p className="text-sm leading-6 text-foreground">
            {restart.length === 1
              ? '1 saved setting takes'
              : `${restart.length} saved settings take`}{' '}
            effect after a restart of {listing(processes.map(processName))}.
          </p>
        </FieldRow>
      )}
      <FieldRow label="Machines with a deployment agent">
        {agents.length === 0 ? (
          <p className="text-sm leading-6 text-muted">None yet.</p>
        ) : (
          <div className="flex flex-wrap gap-2">
            <Pill tone="success">{online} reporting</Pill>
            {agents.length > online && (
              <Pill tone="warning">{agents.length - online} not reporting</Pill>
            )}
          </div>
        )}
      </FieldRow>
      {sessions > 0 && (
        <FieldRow
          label="Other database connections"
          help="Opened by tools or by older releases that do not report."
        >
          <p className="text-sm leading-6 text-foreground">
            {sessions === 1 ? '1 connection does' : `${sessions} connections do`} not report which
            settings it uses, so that is not known.
          </p>
        </FieldRow>
      )}
    </GroupCard>
  );
}

function Processes({ instances }: { instances: InstanceView[] }) {
  return (
    <GroupCard title="Running processes" icon="settings">
      {instances.length === 0 ? (
        <Empty>No process has reported yet.</Empty>
      ) : (
        <ul>
          {instances.map((instance) => {
            const [state, tone] = processState(instance);
            return (
              <li
                key={instance.instance_id}
                className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 px-4 py-3.5 last:border-b-0 sm:px-5"
              >
                <div className="min-w-0">
                  <p className="break-words text-sm font-medium text-foreground">
                    {sentence(processKind(instance.role))} on {instance.machine}
                  </p>
                  <p className="mt-0.5 font-mono text-xs text-muted">
                    {shortVersion(instance.build_sha) || 'Release not reported'}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {instance.restart_required.length > 0 && (
                    <Pill tone="warning">Restart needed</Pill>
                  )}
                  <Pill tone={tone}>{state}</Pill>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </GroupCard>
  );
}

function InProgress({
  workflows,
  onOpen,
}: {
  workflows: DeploymentWorkflow[];
  onOpen: (tab: DeploymentTab) => void;
}) {
  const active = workflows.filter(isActive);
  if (active.length === 0) return null;
  return (
    <GroupCard title="In progress" icon="review">
      <ul>
        {active.map((workflow) => {
          const tab = workflowTab(workflow.kind);
          return (
            <li
              key={workflow.workflow_id}
              className="flex flex-wrap items-center justify-between gap-3 border-b border-border/60 px-4 py-3.5 last:border-b-0 sm:px-5"
            >
              <p className="text-sm font-medium text-foreground">{workflowTitle(workflow)}</p>
              <div className="flex items-center gap-2">
                <Pill tone={workflow.progress?.attention ? 'warning' : 'accent'}>
                  {workflow.progress?.attention
                    ? 'Needs your confirmation'
                    : WORKFLOW_STATES[workflow.state]}
                </Pill>
                <a
                  href={`?tab=${tab}`}
                  className="inline-flex min-h-9 items-center rounded-lg px-2 text-sm font-medium text-accent hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light"
                  onClick={(event) => {
                    event.preventDefault();
                    onOpen(tab);
                  }}
                >
                  Open
                </a>
              </div>
            </li>
          );
        })}
      </ul>
    </GroupCard>
  );
}

/** Whether saved settings are in use, what is running, and what is under way. */
export function Overview({
  status,
  agents,
  workflows,
  onOpen,
}: {
  /** `undefined` while the first report loads; `null` from a control plane without one. */
  status: ManagementStatus | null | undefined;
  agents: DeploymentAgent[];
  workflows: DeploymentWorkflow[];
  onOpen: (tab: DeploymentTab) => void;
}) {
  return (
    <>
      <InProgress workflows={workflows} onOpen={onOpen} />
      {status ? (
        <>
          <SettingsStatus status={status} agents={agents} />
          <Processes instances={status.instances} />
        </>
      ) : (
        <GroupCard title="Status" icon="review">
          <Empty>
            {status === undefined
              ? 'Loading…'
              : 'This control plane does not report which settings its processes use.'}
          </Empty>
        </GroupCard>
      )}
    </>
  );
}
