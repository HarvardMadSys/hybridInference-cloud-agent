// Holding a team invitation token across the sign-in round trip.
//
// The link carries its token in the URL fragment. Signing in leaves this
// origin and comes back to a fixed path, which would drop the fragment, so the
// join page parks the token here first. It is a bearer value for one join
// request, so it gets the narrowest home that survives the trip: this tab's
// sessionStorage, with an age limit, removed as soon as it is used — never
// localStorage, a cookie, a query string or anything sent to a server.

const STORAGE_KEY = 'cloud-agent:team-invitation';
/** Long enough to sign in, short enough that an abandoned tab does not keep it. */
export const INVITATION_STASH_MAX_AGE_MS = 30 * 60 * 1000;
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{43}$/;

/** The token in a `#token=…` fragment, or null when the fragment holds none. */
export function tokenFromHash(hash: string): string | null {
  const params = new URLSearchParams(hash.startsWith('#') ? hash.slice(1) : hash);
  const token = params.get('token');
  return token && TOKEN_PATTERN.test(token) ? token : null;
}

export function stashInvitationToken(token: string, now = Date.now()): void {
  try {
    window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ token, savedAt: now }));
  } catch {
    // Storage can be unavailable (private mode, blocked site data). The page
    // still works for a visitor who is already signed in.
  }
}

export function readInvitationToken(now = Date.now()): string | null {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { token?: unknown; savedAt?: unknown };
    const fresh =
      typeof parsed.savedAt === 'number' &&
      now - parsed.savedAt >= 0 &&
      now - parsed.savedAt <= INVITATION_STASH_MAX_AGE_MS;
    if (typeof parsed.token === 'string' && TOKEN_PATTERN.test(parsed.token) && fresh) {
      return parsed.token;
    }
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Unreadable or malformed: treat as absent.
  }
  return null;
}

export function clearInvitationToken(): void {
  try {
    window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Nothing to clear.
  }
}
