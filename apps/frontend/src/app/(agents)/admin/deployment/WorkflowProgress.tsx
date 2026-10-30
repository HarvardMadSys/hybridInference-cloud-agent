'use client';

import {
  type DeploymentWorkflow,
  type FarEndRequest,
  confirmFarEnd,
  retryWorkflow,
} from '@/lib/api/adminManagement';
import {
  RETRYABLE,
  TUNNEL_PHASES,
  WORKFLOWS,
  WORKFLOW_STATES,
  isActive,
  stepStates,
} from './workflowSteps';

type Run = (work: () => Promise<string | void>) => Promise<void>;

const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-50';
const MARK = { done: '✓', current: '→', pending: '·' };

function counters(workflow: DeploymentWorkflow): string {
  const { totals, reindexed } = workflow.progress ?? {};
  const parts: string[] = [];
  if (totals?.rewritten !== undefined) parts.push(`${totals.rewritten} values re-encrypted`);
  if (totals?.unreadable) parts.push(`${totals.unreadable} unreadable with either key`);
  if (reindexed?.bindings !== undefined) {
    parts.push(`${reindexed.bindings} fingerprints rebuilt`);
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
    <div className="mt-2 rounded-md border border-border p-3">
      <p className="text-sm">
        <strong>{request.host}</strong>:{' '}
        {adding
          ? 'nothing here can edit this far end. Add these keys to its authorized_keys, keeping the same restrictions as the current entry, then confirm.'
          : 'the server runs on its new keys. Remove these old keys from the far end, then confirm.'}
      </p>
      <ul className="mt-2 text-xs">
        {Object.entries(request.tunnels).map(([tunnel, key]) => (
          <li key={tunnel} className="mt-1 break-all">
            <span className="text-muted">{tunnel}:</span> <code>{key}</code>
          </li>
        ))}
      </ul>
      <button
        type="button"
        className={`${BUTTON} mt-2`}
        onClick={() => {
          void run(() => confirmFarEnd(workflowId, request.token).then(() => undefined));
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
    <ul className="mt-1 text-xs">
      {servers.map(([host, phase]) => (
        <li key={host}>
          {host}:{' '}
          {phase === 'done'
            ? 'finished'
            : (TUNNEL_PHASES.find((item) => item.id === phase)?.label ?? phase)}
        </li>
      ))}
    </ul>
  );
}

/** One workflow in words: what it is, each step, and anything it needs from you. */
export function WorkflowProgress({ workflow, run }: { workflow: DeploymentWorkflow; run: Run }) {
  const definition = WORKFLOWS[workflow.kind];
  const states = stepStates(workflow);
  const attention = workflow.progress?.attention;
  const summary = counters(workflow);
  return (
    <li className="mt-3 rounded-md border border-border p-3" data-workflow={workflow.kind}>
      <div className="flex flex-wrap items-baseline gap-2 text-sm">
        <strong>{definition?.title ?? workflow.kind}</strong>
        <span className={workflow.state === 'failed' ? 'text-red-700' : 'text-muted'}>
          {WORKFLOW_STATES[workflow.state]}
        </span>
      </div>
      {states.length > 0 && (
        <ol className="mt-1 text-xs">
          {states.map((step, index) => (
            <li key={step.id} aria-current={step.state === 'current' ? 'step' : undefined}>
              <span aria-hidden="true">{MARK[step.state]}</span>{' '}
              <span className={step.state === 'pending' ? 'text-muted' : undefined}>
                {definition.steps[index].label}
              </span>
            </li>
          ))}
        </ol>
      )}
      {workflow.kind === 'tunnel_keys' && <TunnelServers workflow={workflow} />}
      {summary && <p className="mt-1 text-xs text-muted">{summary}</p>}
      {workflow.detail && <p className="mt-1 text-xs">{workflow.detail}</p>}
      {attention && isActive(workflow) && (
        <FarEndQuestion workflowId={workflow.workflow_id} request={attention} run={run} />
      )}
      {workflow.state === 'failed' && RETRYABLE.has(workflow.kind) && (
        <button
          type="button"
          className={`${BUTTON} mt-2`}
          onClick={() => {
            void run(() => retryWorkflow(workflow.workflow_id).then(() => undefined));
          }}
        >
          Continue from this step
        </button>
      )}
    </li>
  );
}
