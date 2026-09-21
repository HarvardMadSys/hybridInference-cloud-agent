/** UI locators are shared across windows; request IDs belong to the record. */
export function sideChatKeys(userId: string, threadId: string, sideId = 'side-chat') {
  const scope = `${encodeURIComponent(userId)}:${encodeURIComponent(threadId)}`;
  // Keep the original tab's locator, including any in-flight fork request.
  const suffix = sideId === 'side-chat' ? '' : `:${encodeURIComponent(sideId)}`;
  return {
    session: `agents.sideChat.session.${scope}${suffix}`,
    draft: `agents.sideChat.draft.${scope}${suffix}`,
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
