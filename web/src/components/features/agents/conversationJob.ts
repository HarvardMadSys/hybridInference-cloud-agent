import type { AgentJobApi } from '@/lib/api/agents';
import type { AgentJob } from './types';

export function latestJob(jobs: readonly AgentJobApi[]): AgentJobApi | undefined {
  return jobs.reduce<AgentJobApi | undefined>(
    (latest, job) => (!latest || (job.turn_no ?? 1) > (latest.turn_no ?? 1) ? job : latest),
    undefined,
  );
}
export function isActiveJobState(state: string | undefined): boolean {
  return ['queued', 'running', 'publishing'].includes(state ?? '');
}
export function isActiveConversationState(state: string | null | undefined): boolean {
  return state === 'running' || state === 'queued';
}
export function isBusyJob(job: AgentJobApi): boolean {
  return isActiveJobState(job.state) || isActiveConversationState(job.conversation_state);
}
export function hasLostContext(job: Pick<AgentJob, 'nativeState' | 'contextStatus'>): boolean {
  return job.nativeState === 'lost' || job.contextStatus === 'lost';
}
export function ownerRunNote(job: AgentJob): string | undefined {
  if (job.state === 'done' || job.state === 'needs_review') return undefined;
  return (
    job.stateNote ||
    (job.state === 'failed'
      ? 'The run stopped before it could finish. No failure details were recorded.'
      : undefined)
  );
}
export function latestAttempt(job: AgentJob): number {
  return job.currentAttemptNo ?? job.attempts.at(-1)?.no ?? 0;
}
