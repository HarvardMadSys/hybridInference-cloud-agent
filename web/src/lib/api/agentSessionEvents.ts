export const AGENT_SESSIONS_CHANGED = 'agents:sessions-changed';

export interface AgentSessionsChange {
  threadIds: string[];
  archived?: boolean;
  title?: string;
  pinnedAt?: string | null;
  deleted?: boolean;
}

export function announceAgentSessionsChanged(change: AgentSessionsChange): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent(AGENT_SESSIONS_CHANGED, { detail: change }));
  }
}

export function onAgentSessionsChanged(
  listener: (change: AgentSessionsChange) => void,
): () => void {
  const handle = (event: Event) => listener((event as CustomEvent<AgentSessionsChange>).detail);
  window.addEventListener(AGENT_SESSIONS_CHANGED, handle);
  return () => window.removeEventListener(AGENT_SESSIONS_CHANGED, handle);
}
