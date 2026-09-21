import type { AgentJob, AgentJobState, ConversationState } from './types';
export type { ConversationState } from './types';

export const CONVERSATION_STATE_KEYS = {
  queued: 'conversation.state.queued',
  running: 'conversation.state.running',
  idle: 'conversation.state.idle',
  failed: 'conversation.state.failed',
} as const;

export function toConversationState(state: AgentJobState): ConversationState {
  if (state === 'done' || state === 'needs_review' || state === 'cancelled') return 'idle';
  return state;
}

/** Older servers only report the selected turn; newer ones aggregate every turn. */
export function getConversationState(
  job: Pick<AgentJob, 'state' | 'conversationState'>,
): ConversationState {
  return job.conversationState ?? toConversationState(job.state);
}
