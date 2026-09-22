import { getAgentGoal, setAgentGoal } from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';

// The two things every goal writer has to get right, in one place: proving the
// revision it read, and recognising the refusal that says it did not.

/** Whether a refusal was the compare-and-set, and not some other failure. */
export function isGoalRevisionConflict(cause: unknown): boolean {
  return cause instanceof APIError && cause.code === 'goal_revision_conflict';
}

/**
 * Write the conversation's goal, proving the revision it was read at.
 *
 * The objective is the whole draft, and the round bound is deliberately not
 * mentioned. That is not the same as clearing it: the server treats an omitted
 * ``max_rounds`` as "leave the bound alone" and only an explicit ``null`` as
 * "lift it". So this request means what an edit should mean — these words, and
 * nothing said about the limit — and opening the band's editor on a goal
 * somebody bounded and pressing Save leaves that bound exactly where it was.
 *
 * It is also how a new goal ends up unbounded: a goal that has no bound has
 * nothing to leave alone. Sending a number here is what used to make every goal
 * finite at a default nobody chose.
 *
 * A conversation whose goal this client has never read has no revision to
 * prove. The read that produces one is also what proves nothing was written
 * between it and the write — so it is done here rather than by sending the
 * write with no revision at all, which means "overwrite whatever is there" and
 * silently loses an edit made in another tab.
 */
export async function writeGoal(
  threadId: string,
  objective: string,
  knownRevision?: number,
): Promise<void> {
  const revision = knownRevision ?? (await getAgentGoal(threadId)).revision;
  await setAgentGoal(threadId, { goal: { objective }, revision });
}
