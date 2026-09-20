import type { AgentGoal, AgentJob } from '../types';

/**
 * Whether anything in this conversation is still moving.
 *
 * Wider than "the viewed turn is running", and that is the whole point: the
 * page renders a *selected* turn, and a later one can be queued behind it. A
 * goal band that read only the selected job would say "used 3/10" with no round
 * in flight while a round is in fact in flight behind it — which is exactly the
 * moment the owner is watching to see whether the goal is still moving.
 *
 * A job whose workspace is still being published is already ``running`` here:
 * ``toDisplayState`` maps the API's ``publishing`` onto ``running`` before this
 * type ever sees it, so there is no separate case to add.
 */
export function hasLiveWork(job: Pick<AgentJob, 'state' | 'conversationState'>): boolean {
  if (job.state === 'running' || job.state === 'queued') return true;
  return job.conversationState === 'running' || job.conversationState === 'queued';
}

/**
 * Whether this conversation's goal still owes the platform another round.
 *
 * An ``active`` goal is legitimately idle for a moment — the previous round has
 * settled and the next one has not been queued yet — so the state of the turn
 * on screen cannot answer whether there is more to come. Observation that
 * stopped with the turn would freeze the owner's view on a state the platform
 * has already moved past.
 */
export function hasActiveGoal(goal: Pick<AgentGoal, 'state'> | null | undefined): boolean {
  return goal?.state === 'active';
}
