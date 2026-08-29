// Build-time configuration for the cloud agent web app.
//
// The bundle is built once and served statically, so every value here has to
// be available at build time — which is why they are all `NEXT_PUBLIC_*`.
//
// Two URLs, two different services, and conflating them is the mistake this
// module exists to prevent:
//
//   * `agentApiBase` is *this* service's control plane. It is the only origin
//     the session cookie is sent to.
//   * `gatewayAuthorizeUrl` is the FreeInference gateway's consent page. It
//     owns login; we never see a password, and never hold the gateway's keys.

/**
 * Control plane base URL (`NEXT_PUBLIC_AGENT_API_URL`).
 *
 * Empty means same-origin, which is the shape DR4 describes: one host serving
 * the web app and the API. A cross-origin deployment must set this *and*
 * arrange CORS with credentials on the control plane, because the session is a
 * cookie rather than a bearer token.
 */
export const agentApiBase = (process.env.NEXT_PUBLIC_AGENT_API_URL || '').replace(/\/+$/, '');

/**
 * The prefix this app is served under (`NEXT_PUBLIC_BASE_PATH`).
 *
 * Empty for a deployment that owns its own hostname. Set to `/agents` to
 * mount the app inside the gateway's existing origin, which is what makes the
 * session cookie same-origin and removes CORS from the problem entirely.
 *
 * **Next.js prefixes what it generates, not what we write.** `<Link>`, router
 * navigations and asset URLs pick `basePath` up automatically; a path built by
 * string concatenation does not. So anything that hands a URL to another
 * service — above all the OAuth redirect, which the gateway matches as an
 * exact string — has to apply this itself.
 *
 * Normalized rather than trusted: a trailing slash makes every derived path
 * contain `//`, and a missing leading slash makes them relative.
 */
export const basePath = normalizeBasePath(process.env.NEXT_PUBLIC_BASE_PATH);

export function normalizeBasePath(raw: string | undefined): string {
  const trimmed = (raw || '').trim().replace(/\/+$/, '');
  if (!trimmed) return '';
  return trimmed.startsWith('/') ? trimmed : `/${trimmed}`;
}

/**
 * The gateway's consent page (`NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL`).
 *
 * Unset means this deployment cannot sign anyone in. That is surfaced as a
 * visible error rather than a redirect to a half-built URL — a login loop is
 * much harder to diagnose than a missing-configuration message.
 */
export const gatewayAuthorizeUrl = (process.env.NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL || '').trim();

/** Resolve an absolute production URL or a same-origin local path. */
export function resolvedGatewayAuthorizeUrl(authorizeUrl = gatewayAuthorizeUrl): string {
  if (!authorizeUrl) return '';
  try {
    return new URL(authorizeUrl, window.location.origin).toString();
  } catch {
    return '';
  }
}

/**
 * The gateway's token endpoint, derived from the authorize URL's origin.
 *
 * Derived rather than configured because the two are the same deployment by
 * construction (ENVIRONMENT.md defines the authorize URL as
 * `${GATEWAY_BASE_URL}/authorize`), and a second variable is a second thing to
 * get out of step. Returns an empty string when authorization is unconfigured.
 */
export function gatewayTokenUrl(): string {
  const authorizeUrl = resolvedGatewayAuthorizeUrl();
  if (!authorizeUrl) return '';
  try {
    return new URL('/v1/identity/token', authorizeUrl).toString();
  } catch {
    return '';
  }
}

/**
 * The client id this app presents to the gateway. Fixed: the gateway's
 * `/authorize` page allowlists it by name, so it is a contract value rather
 * than a deployment choice.
 */
export const OAUTH_CLIENT_ID = 'cloud-agent';

export const config = {
  apiBase: agentApiBase,
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'FreeInference Cloud Agent',
  environment: process.env.NODE_ENV || 'development',
  buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
  buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',
  isDevelopment: process.env.NODE_ENV === 'development',
  isProduction: process.env.NODE_ENV === 'production',
} as const;

export type Config = typeof config;
