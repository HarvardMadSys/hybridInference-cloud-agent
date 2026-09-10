/** Owner-scoped invalidation after a successful archive-list restore. */
export const CONVERSATION_RESTORED_EVENT = 'agent:conversation-restored';
export interface ConversationRestoredDetail {
  threadId: string;
  userId: string | undefined;
}
export function announceConversationRestored(detail: ConversationRestoredDetail): void {
  window.dispatchEvent(new CustomEvent(CONVERSATION_RESTORED_EVENT, { detail }));
}
