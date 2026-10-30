'use client';

import {
  type DeploymentWorkflow,
  type FarEndRequest,
  confirmFarEnd,
  retryWorkflow,
} from '@/lib/api/adminManagement';

import { readable, when } from './deploymentCopy';
import { BUTTON_CLASS, PRIMARY_CLASS, Pill, type Run, type Tone } from './deploymentUi';
import {
  RETRYABLE,
  TUNNEL_PHASES,
  WORKFLOWS,
  WORKFLOW_STATES,
  isActive,
  stepStates,
  type StepState,
} from './workflowSteps';

const TUNNEL_NAMES: Record<string, string> = {
  gateway: 'Gateway tunnel',
  mcp: 'MCP tunnel',
  control: 'Control-plane tunnel',
};

const STATE_TONES: Record<DeploymentWorkflow['state'], Tone> = {
  running: 'accent',
  waiting: 'warning',
  succeeded: 'success',
  failed: 'error',
  cancelled: 'neutral',
};

const CREDENTIAL_KEYS: Record<string, string> = {
  user: 'User credential key',
  compute: 'Compute credential key',
};

export function workflowTitle(workflow: Pick<DeploymentWorkflow, 'kind' | 'params'>): string {
  const key = workflow.kind === 'credential_key' ? String(workflow.params?.key ?? '') : '';
  return CREDENTIAL_KEYS[key] ?? WORKFLOWS[workflow.kind]?.title ?? 'Operation';
}

function counters(workflow: DeploymentWorkflow): string {
  const { totals, reindexed } = workflow.progress ?? {};
  const parts: string[] = [];
  if (totals?.rewritten !== undefined) parts.push(`${totals.rewritten} values re-encrypted`);
  if (totals?.unreadable) parts.push(`${totals.unreadable} unreadable with either key`);
  if (reindexed?.bindings !== undefined) {
    parts.push(`${reindexed.bindings} lookups rebuilt`);
  }
  return parts.join(', ');
}

function FarEndQuestion({
  workflowId,
  request,
  run,
}: {
  workflowId: string;
  request: FarEndRequest;
  run: Run;
}) {
  const adding = request.action === 'add';
  return (
    <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3.5">
      <p className="text-sm font-medium text-foreground">
        {adding ? 'Add these keys on the far end' : 'Remove the old keys from the far end'}
      </p>
      <p className="mt-1 text-xs leading-5 text-muted">
        {adding
          ? `${request.host} has a tunnel that ends somewhere this deployment cannot update. Allow these keys there, with the same restrictions as the current key, then confirm.`
          : `${request.host} now uses its new keys. Remove these old keys where the tunnel ends, then confirm.`}
      </p>
      <ul className="mt-3 space-y-2">
        {Object.entries(request.tunnels).map(([tunnel, key]) => (
          <li key={tunnel} className="min-w-0">
            <p className="text-xs font-medium text-muted">{TUNNEL_NAMES[tunnel] ?? 'Tunnel'}</p>
            <code className="mt-1 block break-all rounded-lg bg-surface px-3 py-2 font-mono text-xs text-foreground ring-1 ring-border">
              {key}
            </code>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className={`${PRIMARY_CLASS} mt-3`}
        onClick={() => {
          void run(() =>
            confirmFarEnd(workflowId, request.token).then(
              () => 'Thanks. The rotation continues once the server proves the change.',
            ),
          );
        }}
      >
        {adding ? 'I added these keys' : 'I removed these keys'}
      </button>
    </div>
  );
}

function TunnelServers({ workflow }: { workflow: DeploymentWorkflow }) {
  const phases = workflow.progress?.phases ?? {};
  const servers = Object.entries(phases);
  if (servers.length === 0) return null;
  return (
    <ul className="mt-2 space-y-1 text-xs text-muted">
      {servers.map(([host, phase]) => (
        <li key={host}>
          <span className="font-medium text-foreground">{host}</span>:{' '}
          {phase === 'done'
            ? 'finished'
            : (TUNNEL_PHASES.find((item) => item.id === phase)?.label ?? 'in progress')}
        </li>
      ))}
    </ul>
  );
}

const MARKS: Record<StepState, string> = {
  done: 'border-status-success bg-status-success text-white',
  current: 'border-accent bg-accent/10 text-accent',
  pending: 'border-border bg-surface text-muted',
};

function StepList({ workflow }: { workflow: DeploymentWorkflow }) {
  const definition = WORKFLOWS[workflow.kind];
  const states = stepStates(workflow);
  if (states.length === 0) return null;
  return (
    <ol className="mt-3 space-y-1.5">
      {states.map((step, index) => (
        <li
          key={step.id}
          aria-current={step.state === 'current' ? 'step' : undefined}
          className="flex items-center gap-2.5 text-sm"
        >
          <span
            aria-hidden="true"
            className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-[11px] font-semibold ${MARKS[step.state]}`}
          >
            {step.state === 'done' ? '✓' : index + 1}
          </span>
          <span
            className={
              step.state === 'pending'
                ? 'text-muted'
                : step.state === 'current'
                  ? 'font-medium text-foreground'
                  : 'text-foreground'
            }
          >
            {definition.steps[index].label}
          </span>
          {step.state === 'current' && <span className="sr-only">(current step)</span>}
        </li>
      ))}
    </ol>
  );
}

/** Why an operation waits or stopped, in plain words only. */
function Explanation({ workflow, asking }: { workflow: DeploymentWorkflow; asking: boolean }) {
  const failed = workflow.state === 'failed';
  const detail =
    !asking && (failed || workflow.state === 'waiting') ? readable(workflow.detail) : '';
  if (!detail && !failed) return null;
  return (
    <p className={`mt-2 text-xs leading-5 ${failed ? 'text-status-error' : 'text-muted'}`}>
      {detail || 'Stopped before finishing.'}
    </p>
  );
}

/** One operation in words: what it is, each step, and anything it needs from you. */
export function WorkflowProgress({ workflow, run }: { workflow: DeploymentWorkflow; run: Run }) {
  const attention = workflow.progress?.attention;
  const summary = counters(workflow);
  const asking = Boolean(attention && isActive(workflow));
  const started = when(workflow.created_at);
  return (
    <li
      className="border-b border-border/60 px-4 py-4 last:border-b-0 sm:px-5"
      data-workflow={workflow.kind}
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-medium text-foreground">{workflowTitle(workflow)}</p>
        <Pill tone={asking ? 'warning' : STATE_TONES[workflow.state]}>
          {asking ? 'Needs your confirmation' : WORKFLOW_STATES[workflow.state]}
        </Pill>
      </div>
      {started && <p className="mt-0.5 text-xs text-muted">Started {started}</p>}
      <StepList workflow={workflow} />
      {workflow.kind === 'tunnel_keys' && <TunnelServers workflow={workflow} />}
      {summary && <p className="mt-2 text-xs text-muted">{summary}</p>}
      <Explanation workflow={workflow} asking={asking} />
      {attention && asking && (
        <FarEndQuestion workflowId={workflow.workflow_id} request={attention} run={run} />
      )}
      {workflow.state === 'failed' && RETRYABLE.has(workflow.kind) && (
        <button
          type="button"
          className={`${BUTTON_CLASS} mt-3`}
          onClick={() => {
            void run(() =>
              retryWorkflow(workflow.workflow_id).then(() => 'Continuing from where it stopped.'),
            );
          }}
        >
          Continue from this step
        </button>
      )}
    </li>
  );
}
