import type { AgentJob } from '../types';

/**
 * Whether anything in this conversation is queued or running right now.
 *
 * Wider than "the viewed turn is running", and that is the whole point: the
 * page renders a *selected* turn, and a later one can be queued behind it. A
 * goal band that read only the selected job would say "used 3/10" with no round
 * in flight while a round is in fact in flight behind it — which is exactly the
 * moment the owner is watching to see whether the goal is still moving.
 */
export function hasLiveWork(job: Pick<AgentJob, 'state' | 'conversationState'>): boolean {
  if (job.state === 'running' || job.state === 'queued') return true;
  return job.conversationState === 'running' || job.conversationState === 'queued';
}
