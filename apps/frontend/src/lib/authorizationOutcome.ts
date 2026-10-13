/**
 * What an OAuth return page says about an authorization that did not finish.
 *
 * `/integrations` and `/connected` are reached by redirects — from the control
 * plane's MCP callback, and from GitHub or GitLab — and the outcome arrives in
 * the query string. So does anything a crafted link puts there: the page looks
 * the same whoever wrote the URL. React escapes the text, so it cannot become
 * markup, but a sentence on a trusted page reads as the app speaking. So these
 * pages show wording written here, chosen by the outcome the redirect names.
 *
 * Text that names no known outcome is not dropped, because it may be the only
 * diagnosis there is (a token endpoint's status, an authorization server's own
 * error code). It is shortened, quoted and labelled as unverified instead, so it
 * reads as what it is: something the link carried, not something the app says.
 */

/**
 * The authorization errors of RFC 6749 §4.1.2.1, plus the two GitHub adds.
 *
 * A fragment, so each page can frame it in its own sentence.
 */
const AUTHORIZATION_ERRORS = new Map<string, string>([
  ['access_denied', 'access was cancelled or denied'],
  ['invalid_request', 'the authorization request was refused as invalid'],
  ['unauthorized_client', 'this app is not allowed to ask for authorization this way'],
  ['unsupported_response_type', 'the server does not support this kind of authorization'],
  ['invalid_scope', 'the requested permissions were refused'],
  ['server_error', 'the server hit an error while authorizing'],
  ['temporarily_unavailable', 'the server is temporarily unavailable'],
  ['redirect_uri_mismatch', "the app's registered callback address does not match this site"],
  ['application_suspended', 'the app has been suspended'],
]);

/**
 * The fixed reasons the MCP callback sends back as `mcp_message`.
 *
 * Written by the control plane (`oauth_callback` and `_callback_redirect` in
 * `apps/backend/cloud_agent/api/mcp_connections.py`), so they are this
 * service's own wording and are shown as they are. A reason added there and
 * not here still reaches the reader, as unverified detail.
 */
const MCP_CALLBACK_REASONS = new Set([
  'GitHub sign-in was cancelled. You can try again.',
  'missing state or code',
  'sign in to the account that started this authorization, then sign in to the server again',
  'this authorization expired, was already used, or was started by another account',
  'this authorization is not for that connection',
  'that connection was removed',
  'the connection was reconfigured',
  'the connection was reconnected',
  'a newer authorization replaced this one',
  'this flow could not be read',
  'the connection changed during authorization',
]);

/** Long enough for an error code or a status line, and no more. */
const DETAIL_LIMIT = 120;

function capitalized(fragment: string): string {
  return fragment.charAt(0).toUpperCase() + fragment.slice(1);
}

/**
 * Query-string text after the app's own words, as a quotation of it.
 *
 * Control and formatting characters go first: a bidirectional override would
 * otherwise reorder what is on screen, and a line break would make the quote
 * look like a paragraph of its own.
 */
function withDetail(summary: string, text: string): string {
  const flat = text
    .replace(/[\p{Cc}\p{Cf}]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!flat) return summary;
  // By code point, so the cut never splits a surrogate pair.
  const characters = Array.from(flat);
  const short =
    characters.length > DETAIL_LIMIT ? `${characters.slice(0, DETAIL_LIMIT - 1).join('')}…` : flat;
  return `${summary} Unverified detail from the redirect: “${short}”`;
}

/** Why an MCP server's sign-in did not complete, from the callback's `mcp_message`. */
export function describeMcpAuthorizationFailure(message: string | null | undefined): string {
  const generic = 'Sign-in did not complete. Try again.';
  const reason = message?.trim();
  if (!reason) return generic;
  if (MCP_CALLBACK_REASONS.has(reason)) return reason;
  const known = AUTHORIZATION_ERRORS.get(reason);
  if (known) return `Sign-in did not complete: ${known} (${reason}). Try again.`;
  return withDetail(generic, reason);
}

/**
 * Why GitHub or GitLab sent the browser back without a code.
 *
 * Read from the provider's `error` code; its `error_description` is free text,
 * so it is shown only when the code names nothing known, and only as detail.
 */
export function describeProviderAuthorizationFailure(
  providerName: string,
  error: string | null | undefined,
  description: string | null | undefined,
): string {
  const code = error?.trim() ?? '';
  const known = AUTHORIZATION_ERRORS.get(code);
  if (known) return `${capitalized(known)} (${code}).`;
  const generic = `${providerName} did not complete the authorization.`;
  return withDetail(generic, description?.trim() || code);
}
