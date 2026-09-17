/** UI locators are shared across windows; request IDs belong to the record. */
export function sideChatKeys(userId: string, threadId: string) {
  const scope = `${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;
  return {
    session: `agents.sideChat.session.${scope}`,
    draft: `agents.sideChat.draft.${scope}`,
  };
}

export function clearSideChatDrafts(userId?: string): void {
  if (!userId) return;
  try {
    const prefix = `agents.sideChat.draft.${encodeURIComponent(userId)}:`;
    for (const key of Object.keys(sessionStorage)) {
      if (
        key.startsWith(prefix) ||
        (key.startsWith(`agent-side-chat:${userId}:`) && key.endsWith(':draft'))
      ) {
        sessionStorage.removeItem(key);
      }
    }
    window.dispatchEvent(new CustomEvent('agents:side-chat-drafts-cleared', { detail: userId }));
  } catch {
    /* Signing out still succeeds when storage is unavailable. */
  }
}
