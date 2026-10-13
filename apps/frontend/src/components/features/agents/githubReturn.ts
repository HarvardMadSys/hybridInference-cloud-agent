/**
 * Coming back from GitHub to where the owner left.
 *
 * Connecting GitHub or giving the App a repository is a round trip through
 * github.com. It used to end on the Integrations page, so a task half written
 * in the composer was lost and the owner had to find their way back to it.
 * Before leaving, the composer notes where it was and what was in it; the
 * callback page returns there, and the composer puts the draft back.
 *
 * Session storage, per tab, because the round trip happens in this tab. The
 * draft is the owner's own unsent text and choices — never a credential — and
 * expires, so an abandoned trip does not surface in a task started much later.
 */

const RETURN_KEY = 'agents.github.return';
const DRAFT_KEY = 'agents.composer.draft';
const draftKey = (userId?: string) =>
  userId ? `${DRAFT_KEY}:${encodeURIComponent(userId)}` : DRAFT_KEY;
const MAX_AGE_MS = 30 * 60 * 1000;

/** What the composer had when the owner left it. */
export interface ComposerDraft {
  task: string;
  repo: string;
  runtime: string;
  model: string;
}

function write(key: string, value: unknown): void {
  try {
    window.sessionStorage.setItem(key, JSON.stringify({ value, savedAt: Date.now() }));
  } catch {
    // Storage can be unavailable (private mode, quota). The trip still works;
    // it just ends on the Integrations page, as it always did.
  }
}

function read<T>(key: string): T | null {
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const saved = JSON.parse(raw) as { value?: T; savedAt?: number };
    if (typeof saved.savedAt !== 'number' || Date.now() - saved.savedAt > MAX_AGE_MS) {
      window.sessionStorage.removeItem(key);
      return null;
    }
    return saved.value ?? null;
  } catch {
    return null;
  }
}

function remove(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    // Nothing to clean up if storage is unavailable.
  }
}

/** A path inside this app, and nothing that could leave it. */
function isAppPath(path: unknown): path is string {
  return typeof path === 'string' && path.startsWith('/') && !path.startsWith('//');
}

/** Note where to come back to, and the composer's draft if there is one. */
export function rememberReturn(path: string, draft?: ComposerDraft, userId?: string): void {
  if (!isAppPath(path)) return;
  write(RETURN_KEY, path);
  if (draft && (draft.task.trim() || draft.repo)) write(draftKey(userId), draft);
  else remove(draftKey(userId));
}

/** Where the callback should go, once. `null` sends it to its usual place. */
export function takeReturnPath(): string | null {
  const path = read<string>(RETURN_KEY);
  remove(RETURN_KEY);
  return isAppPath(path) ? path : null;
}

/**
 * The draft to restore, without consuming it: a component may be rendered
 * twice before it commits, and only the one that applies it should clear it.
 */
export function peekComposerDraft(userId?: string): ComposerDraft | null {
  const draft = read<ComposerDraft>(draftKey(userId));
  if (!draft || typeof draft.task !== 'string') return null;
  return {
    task: draft.task,
    repo: typeof draft.repo === 'string' ? draft.repo : '',
    runtime: typeof draft.runtime === 'string' ? draft.runtime : '',
    model: typeof draft.model === 'string' ? draft.model : '',
  };
}

const USED_KEY = 'agents.oauth.usedStates';

/**
 * A callback this tab has already completed. Its code and state work once, so
 * the same page opened again — Back from GitHub's install page, a refresh —
 * would send them again and be refused, showing an error for a connection that
 * in fact succeeded.
 */
export function callbackAlreadyUsed(state: string): boolean {
  return (read<string[]>(USED_KEY) ?? []).includes(state);
}

/** Remember a completed callback, keeping only the last few. */
export function markCallbackUsed(state: string): void {
  const used = (read<string[]>(USED_KEY) ?? []).filter((entry) => entry !== state);
  write(USED_KEY, [...used, state].slice(-10));
}

/** The draft has been put back, or the owner came back some other way. */
export function forgetComposerDraft(userId?: string): void {
  remove(draftKey(userId));
  remove(RETURN_KEY);
}

const GITHUB_ACCESS_REQUEST = 'agents:github-access-request';

/**
 * Hand a trip to GitHub to the new-task composer, when one is on screen.
 *
 * The composer is what holds the owner's draft, so a trip started elsewhere on
 * the page — the sidebar's getting-started step — goes through it, and the
 * task written so far comes back with the owner. Whether a composer took the
 * trip; when none did, the caller makes it.
 */
export function requestGitHubAccess(kind: 'connect' | 'manage'): boolean {
  const request = new CustomEvent(GITHUB_ACCESS_REQUEST, { detail: kind, cancelable: true });
  return !window.dispatchEvent(request);
}

/** Take the trips to GitHub asked for elsewhere on the page. Returns the unsubscribe. */
export function onGitHubAccessRequest(take: (kind: 'connect' | 'manage') => void): () => void {
  const listener = (request: Event) => {
    const kind = (request as CustomEvent<unknown>).detail;
    if (kind !== 'connect' && kind !== 'manage') return;
    request.preventDefault();
    take(kind);
  };
  window.addEventListener(GITHUB_ACCESS_REQUEST, listener);
  return () => window.removeEventListener(GITHUB_ACCESS_REQUEST, listener);
}
