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
// `jsonOrThrow` below is carried over **verbatim** from the source client so
// that error classification is unchanged for every call `agents.ts` makes.
// Several of its branches (password reset, email verification, duplicate
// signup) can only fire on gateway endpoints this service does not call; they
// are inert here and kept rather than pruned, because a migration that edits
// logic is a migration that cannot be diffed. Cleanup is a post-cutover PR.

import { APIError, httpStatusToErrorCode } from '@/lib/utils/errors';

/**
 * Dispatched when the control plane says the session is gone. `AuthProvider`
 * listens and drops the user, which sends the app back through sign-in.
 */
export const AUTH_EXPIRED_EVENT = 'cloud-agent:auth-expired';

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
 * the mechanism is the session cookie: nothing is added to the headers, and
 * `credentials: 'include'` is not optional — omitting it on a cross-origin
 * deployment silently produces an anonymous request that 401s.
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
  const resp = await safeFetch(`${apiBase}${input}`, {
    ...init,
    credentials: 'include',
  });

  if (resp.status === 401) notifyAuthExpired();

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
      const error = structuredError;
      errorMessage = (error.message as string) || errorMessage;
      const lowerMessage = errorMessage.toLowerCase();

      // Configuration is an explicit recovery decision. Its wording must not
      // turn an operator error into a retryable expired/invalid-token error.
      if (error.type === 'agent_configuration_error') {
        errorCode = error.type;
      } else if (
        lowerMessage.includes('reset link has already been used') ||
        (lowerMessage.includes('reset token') && lowerMessage.includes('used'))
      ) {
        errorCode = 'RESET_TOKEN_USED';
      } else if (lowerMessage.includes('reset link has expired')) {
        errorCode = 'RESET_TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid or expired reset token')) {
        errorCode = 'RESET_TOKEN_INVALID';
      } else if (lowerMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (lowerMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid') && lowerMessage.includes('token')) {
        errorCode = 'INVALID_TOKEN';
      } else if (error.type === 'validation_error') {
        errorCode = 'VALIDATION_ERROR';
      } else if (
        resp.status === 409 &&
        (lowerMessage.includes('already registered') ||
          (lowerMessage.includes('user') && lowerMessage.includes('already exists')))
      ) {
        errorCode = 'USER_ALREADY_EXISTS';
      } else if (resp.status === 401) {
        errorCode = 'INVALID_CREDENTIALS';
      } else if (
        resp.status === 403 &&
        ((lowerMessage.includes('email') && lowerMessage.includes('not verified')) ||
          lowerMessage.includes('verify your email'))
      ) {
        // Only a genuine unverified-email 403 should drive the login page's
        // "resend verification" flow (mirrors the { detail } branch below).
        // Other typed 403s — suspended account, insufficient permissions,
        // plan/quota forbidden — must NOT be mislabeled as EMAIL_NOT_VERIFIED;
        // they fall through so their real message is surfaced verbatim.
        errorCode = 'EMAIL_NOT_VERIFIED';
      } else if (resp.status === 403 && lowerMessage.includes('suspend')) {
        errorCode = 'ACCOUNT_SUSPENDED';
      } else if (typeof error.type === 'string' && error.type) {
        // Preserve domain conflicts such as native_fork_failed. Treating every
        // structured 409 as USER_ALREADY_EXISTS makes unrelated agent errors
        // render as an email-registration failure wherever getErrorMessage is
        // used.
        errorCode = error.type;
      }
    } else if (data.detail) {
      // FastAPI validation error format: { detail: "..." }
      errorMessage = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail);
      const lowerMessage = errorMessage.toLowerCase();

      // Try to extract more specific error from detail message
      if (lowerMessage.includes('already registered')) {
        errorCode = 'USER_ALREADY_EXISTS';
      } else if (
        (lowerMessage.includes('email') && lowerMessage.includes('not verified')) ||
        lowerMessage.includes('verify your email')
      ) {
        // Login raises a plain HTTPException ({ detail: "Email not verified..." }),
        // not the typed { error: {...} } shape, so match on the message here so the
        // login page can offer a "resend verification email" action.
        errorCode = 'EMAIL_NOT_VERIFIED';
      } else if (
        lowerMessage.includes('reset link has already been used') ||
        (lowerMessage.includes('reset token') && lowerMessage.includes('used'))
      ) {
        errorCode = 'RESET_TOKEN_USED';
      } else if (lowerMessage.includes('reset link has expired')) {
        errorCode = 'RESET_TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid or expired reset token')) {
        errorCode = 'RESET_TOKEN_INVALID';
      } else if (lowerMessage.includes('already been used')) {
        errorCode = 'TOKEN_ALREADY_USED';
      } else if (lowerMessage.includes('expired')) {
        errorCode = 'TOKEN_EXPIRED';
      } else if (lowerMessage.includes('invalid') && lowerMessage.includes('token')) {
        errorCode = 'INVALID_TOKEN';
      } else if (lowerMessage.includes('password') && lowerMessage.includes('characters')) {
        errorCode = 'WEAK_PASSWORD';
      } else if (lowerMessage.includes('invalid email or password')) {
        errorCode = 'INVALID_CREDENTIALS';
      }
      // If no specific pattern matched, use the detail message directly
      // Don't force it into a predefined error code
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
