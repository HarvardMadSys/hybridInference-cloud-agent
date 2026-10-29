// Centralized HTTP client.
//
// **Rewritten rather than moved.** The source repo's client kept the gateway's
// access token in `sessionStorage`, attached it as a bearer header, and ran its
// own refresh loop. This service has no bearer token to keep: sign-in ends in
// an HttpOnly session cookie minted by the control plane (see
// `cloud_agent/identity.py`), which JavaScript cannot read and
// therefore cannot leak. Every request simply carries `credentials: 'include'`
// and the browser attaches the cookie.
//
// What that removes, deliberately:
//   * `setAccessToken` / `getAccessToken` and the `sessionStorage` they used;
//   * the JWT expiry peek — the cookie's expiry is the server's business;
//   * the refresh-and-retry dance. A 401 here means the session is over, and
//     the caller is sent back through the gateway's `/authorize` flow.
//
// `jsonOrThrow` below was carried over verbatim from the source client, and
// its legacy `error_code` branch still is. Its plain-`detail` branch no longer
// infers a code from the wording: the gateway-era patterns (password reset,
// email verification, duplicate signup, the catch-all "expired") could only
// match endpoints this service does not call, and the catch-all replaced the
// server's own refusal with a different instruction. A plain detail is now
// shown as sent with UNKNOWN_ERROR. The structured `{ error: { type, message } }`
// branch is this service's own shape, and it classifies by `type` alone: the
// source's message patterns ran first there and replaced typed reasons callers
// recover on.

import { APIError, httpStatusToErrorCode } from '@/lib/utils/errors';

/**
 * Dispatched when the control plane says the session is gone. `AuthProvider`
 * listens and drops the user, which sends the app back through sign-in.
 */
export const AUTH_EXPIRED_EVENT = 'cloud-agent:auth-expired';
/** Fired when the API refuses a request pinned to an account the browser left. */
export const ACCOUNT_SWITCHED_EVENT = 'cloud-agent:account-switched';

// The local account this tab shows. Every request names it, and the API
// refuses (409 account_switched) once the browser's active account is another,
// so a tab left on one account never acts as the next one.
let pinnedAccount: string | null = null;

/** Name the local account this tab shows on every request, or stop naming one. */
export function pinAccount(accountId: string | null): void {
  pinnedAccount = accountId;
}

// Wrap fetch so a genuine connectivity failure (DNS, offline, connection
// refused/reset) — which rejects with a TypeError before any response exists —
// surfaces as a NETWORK_ERROR APIError instead of a raw, unmapped TypeError.
// This is the ONLY path that should produce NETWORK_ERROR; once a response is
// received the connection is fine and errors are classified by status instead.
export async function safeFetch(input: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init);
  } catch (err) {
    if (err instanceof APIError) throw err;
    // An intentional abort (AbortController on unmount, debounced search, etc.)
    // isn't a connectivity failure — let it propagate so callers can ignore it
    // rather than surface "check your connection".
    if (err instanceof Error && err.name === 'AbortError') throw err;
    const message = err instanceof Error ? err.message : 'Network request failed';
    throw new APIError('NETWORK_ERROR', message);
  }
}

function notifyAuthExpired(): void {
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}

/**
 * Issue an authenticated request.
 *
 * The name is kept from the source so the moved API client reads the same, but
 * the mechanism is the session cookie, and `credentials: 'include'` is not
 * optional — omitting it on a cross-origin deployment silently produces an
 * anonymous request that 401s. The only header added is `X-Agent-Account`, the
 * local account this tab shows (see `pinAccount`); it carries no authority.
 *
 * A 401 is reported to the app once, as an event, and then returned. It is not
 * retried: there is no refresh token here, so a second attempt would fail the
 * same way and only delay the redirect to sign-in.
 */
export async function fetchWithAuth(
  apiBase: string,
  input: string,
  init: RequestInit = {},
): Promise<Response> {
  let headers = init.headers;
  if (pinnedAccount) {
    const pinned = new Headers(init.headers);
    if (!pinned.has('X-Agent-Account')) pinned.set('X-Agent-Account', pinnedAccount);
    headers = pinned;
  }
  const resp = await safeFetch(`${apiBase}${input}`, {
    ...init,
    ...(headers === undefined ? {} : { headers }),
    credentials: 'include',
  });

  if (resp.status === 401) notifyAuthExpired();
  if (resp.status === 409 && typeof window !== 'undefined') {
    const code = await resp
      .clone()
      .json()
      .then((body) => body?.detail?.error?.type)
      .catch(() => undefined);
    if (code === 'account_switched') window.dispatchEvent(new Event(ACCOUNT_SWITCHED_EVENT));
  }

  return resp;
}

export async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (resp.ok) return resp.json() as Promise<T>;

  let errorData: unknown = null;
  try {
    errorData = await resp.json();
  } catch {
    // Body isn't JSON — typically an edge proxy / load balancer HTML error page,
    // not a connectivity failure (the response arrived). Classify by status so
    // the user sees a timeout / unavailable / server-error message instead of a
    // misleading "check your connection".
    throw new APIError(
      httpStatusToErrorCode(resp.status),
      `HTTP ${resp.status}: ${resp.statusText}`,
      resp.status,
    );
  }

  // Handle backend error format: { error: { type: "...", message: "...", code: 500 } },
  // the same object wrapped by FastAPI as { detail: { error: {...} } }, or
  // { detail: "..." } for plain HTTPException/validation errors.
  let errorCode = 'UNKNOWN_ERROR';
  let errorMessage = '';
  let errorDetails: Record<string, unknown> | undefined;

  if (errorData && typeof errorData === 'object') {
    const data = errorData as Record<string, unknown>;
    const detail =
      data.detail && typeof data.detail === 'object' && !Array.isArray(data.detail)
        ? (data.detail as Record<string, unknown>)
        : undefined;
    const structuredError =
      data.error && typeof data.error === 'object'
        ? (data.error as Record<string, unknown>)
        : detail?.error && typeof detail.error === 'object'
          ? (detail.error as Record<string, unknown>)
          : undefined;

    if (structuredError) {
      // Backend error format, either direct or under FastAPI's `detail`.
      //
      // Its `type` is the control plane's machine-readable reason, and the one
      // callers recover on: `request_cursor_expired` offers a restart,
      // `revision_conflict` keeps the newer copy, `native_fork_failed` marks a
      // fork failed, and `agent_configuration_error` is an operator's to fix.
      // The message is prose for people, so it is shown and never parsed.
      // Reading it here turned "That page expired" into a signed-out session
      // and every typed 401 into a wrong password — the gateway's
      // email-and-password codes, which no endpoint of this service issues.
      const error = structuredError;
      errorMessage = (error.message as string) || errorMessage;
      if (typeof error.type === 'string' && error.type) errorCode = error.type;
    } else if (typeof detail?.code === 'string' && typeof detail.message === 'string') {
      // Typed resource errors retain their own recovery semantics. A provider
      // credential error must not be mistaken for an expired user session.
      errorCode = detail.code;
      errorMessage = detail.message;
    } else if (data.detail) {
      // FastAPI validation error format: { detail: "..." }
      //
      // Shown as sent, with no code inferred from the wording. The gateway-era
      // patterns that used to live here (password reset, email verification,
      // duplicate signup, and the catch-all "expired" → TOKEN_EXPIRED) matched
      // only endpoints this service does not call, and the catch-all replaced
      // the server's own refusal — "This sign-in has expired. Please start
      // again." — with "Session has expired. Please login again". Which sign-in
      // flow a refusal belongs to is carried by the status; a 401 is announced
      // by fetchWithAuth (AUTH_EXPIRED_EVENT), not by string matching.
      errorMessage = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
    } else if (data.error_code && typeof data.error_code === 'string') {
      // Legacy format
      errorCode = data.error_code;
      errorMessage = (data.message as string) || errorMessage;
      // Carry the admin-authored suspension message (ACCOUNT_SUSPENDED) so the
      // login page can show it in place of the generic suspended text.
      if (typeof data.suspension_message === 'string' && data.suspension_message) {
        errorDetails = { suspension_message: data.suspension_message };
      }
    }
  }

  throw new APIError(errorCode, errorMessage, resp.status, errorDetails);
}
