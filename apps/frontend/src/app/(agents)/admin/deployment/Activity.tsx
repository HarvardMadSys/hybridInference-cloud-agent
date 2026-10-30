'use client';

import {
  type DeploymentAgent,
  type DeploymentIntent,
  type DeploymentWorkflow,
  cancelIntent,
} from '@/lib/api/adminManagement';

import { operationName, readable, stepState, when } from './deploymentCopy';
import { Empty, GroupCard, Pill, type Run, TEXT_BUTTON_CLASS, type Tone } from './deploymentUi';
import { WorkflowProgress } from './WorkflowProgress';

const STEP_TONES: Record<string, Tone> = {
  pending: 'neutral',
  delivered: 'accent',
  running: 'accent',
  succeeded: 'success',
  failed: 'error',
  rejected: 'error',
  expired: 'warning',
  cancelled: 'neutral',
};

/** Guided operations of one area, newest first. */
export function Operations({
  title,
  workflows,
  run,
  empty,
}: {
  title: string;
  workflows: DeploymentWorkflow[];
  run: Run;
  empty?: string;
}) {
  if (workflows.length === 0 && !empty) return null;
  return (
    <GroupCard title={title} icon="review">
      {workflows.length === 0 ? (
        <Empty>{empty}</Empty>
      ) : (
        <ul>
          {workflows.map((workflow) => (
            <WorkflowProgress key={workflow.workflow_id} workflow={workflow} run={run} />
          ))}
        </ul>
      )}
    </GroupCard>
  );
}

/** What each machine was asked to do, and what it answered. */
export function MachineSteps({
  intents,
  agents,
  run,
}: {
  intents: DeploymentIntent[];
  agents: DeploymentAgent[];
  run: Run;
}) {
  const names = new Map(agents.map((agent) => [agent.agent_id, agent.name]));
  return (
    <GroupCard
      title="Machine activity"
      icon="settings"
      description="The most recent steps sent to machines, and what each machine reported back."
    >
      {intents.length === 0 ? (
        <Empty>No step has been sent to a machine yet.</Empty>
      ) : (
        <ul>
          {intents.slice(0, 20).map((intent) => {
            const detail = readable(intent.detail);
            const sent = when(intent.created_at);
            return (
              <li
                key={intent.intent_id}
                className="flex flex-wrap items-start justify-between gap-3 border-b border-border/60 px-4 py-3.5 last:border-b-0 sm:px-5"
              >
                <div className="min-w-0">
                  <p className="text-sm font-medium text-foreground">
                    {operationName(intent.operation)}
                  </p>
                  <p className="mt-0.5 text-xs leading-5 text-muted">
                    {[names.get(intent.agent_id), sent].filter(Boolean).join(' · ')}
                  </p>
                  {detail && (
                    <p className="mt-1 break-words text-xs leading-5 text-muted">{detail}</p>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <Pill tone={STEP_TONES[intent.state] ?? 'neutral'}>
                    {stepState(intent.state)}
                  </Pill>
                  {intent.state === 'pending' && (
                    <button
                      type="button"
                      className={TEXT_BUTTON_CLASS}
                      onClick={() =>
                        void run(() => cancelIntent(intent.intent_id).then(() => 'Step cancelled.'))
                      }
                    >
                      Cancel
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </GroupCard>
  );
}
