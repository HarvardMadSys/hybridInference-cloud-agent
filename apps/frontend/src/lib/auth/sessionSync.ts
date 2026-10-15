// Tell this browser's other Agents tabs whose session the cookie now holds.
//
// The session cookie is the browser's, so signing in as another gateway account
// in one tab ("Switch account") changes every tab's. The tab that signed in
// lands on a fresh page and knows; the others would keep showing the previous
// account's tasks, and every request from them would now be answered for the
// new one. A tab that resolves its session announces the account here, and one
// showing a different account checks its own.
//
// BroadcastChannel delivers a message to every other object with the channel's
// name, including ones in the sending tab, so each tab keeps one object for
// sending and receiving alike. Where it is unavailable the focus check in
// `AuthProvider` still applies.

const CHANNEL = 'cloud-agent:session';

/** The account the browser's Agents session belongs to now, or null when signed out. */
export interface SessionSyncMessage {
  userId: string | null;
}

let channel: BroadcastChannel | null | undefined;

function tabChannel(): BroadcastChannel | null {
  if (channel === undefined) {
    try {
      channel = typeof BroadcastChannel === 'undefined' ? null : new BroadcastChannel(CHANNEL);
    } catch {
      channel = null;
    }
  }
  return channel;
}

export function announceSession(userId: string | null): void {
  try {
    tabChannel()?.postMessage({ userId } satisfies SessionSyncMessage);
  } catch {
    // The other tabs fall back to checking when they are next looked at.
  }
}

/** Call `handler` with every announcement from another tab; returns the unsubscribe. */
export function listenForSession(handler: (message: SessionSyncMessage) => void): () => void {
  const target = tabChannel();
  if (!target) return () => undefined;
  const onMessage = (event: MessageEvent<SessionSyncMessage>) => handler(event.data);
  target.addEventListener('message', onMessage);
  return () => target.removeEventListener('message', onMessage);
}
