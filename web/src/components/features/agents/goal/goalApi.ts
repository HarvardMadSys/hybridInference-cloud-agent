import { getAgentGoal, setAgentGoal } from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';
import type { GoalDraft } from './GoalDialog';

// The two things every goal writer has to get right, in one place: proving the
// revision it read, and recognising the refusal that says it did not.

/** Whether a refusal was the compare-and-set, and not some other failure. */
export function isGoalRevisionConflict(cause: unknown): boolean {
  return cause instanceof APIError && cause.code === 'goal_revision_conflict';
}

/**
 * Write the conversation's goal, proving the revision it was read at.
 *
 * A conversation whose goal this client has never read has no revision to
 * prove. The read that produces one is also what proves nothing was written
 * between it and the write — so it is done here rather than by sending the
 * write with no revision at all, which means "overwrite whatever is there" and
 * silently loses an edit made in another tab.
 */
export async function writeGoal(
  threadId: string,
  draft: GoalDraft,
  knownRevision?: number,
): Promise<void> {
  const revision = knownRevision ?? (await getAgentGoal(threadId)).revision;
  await setAgentGoal(threadId, {
    goal: { objective: draft.objective, max_rounds: draft.maxRounds },
    revision,
  });
}
