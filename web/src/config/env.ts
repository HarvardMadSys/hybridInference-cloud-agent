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
//   * `gatewayUrl` is the HybridInference gateway's origin, for the links this
//     app does not host itself. It owns login; we never see a password, never
//     hold its keys, and — since the sign-in round trip moved into the control
//     plane — never build an authorize URL here either.

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
 * The gateway's origin (`NEXT_PUBLIC_GATEWAY_URL`).
 *
 * For the links this app does not host itself — nothing about sign-in. It
 * replaced `NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL`, which the browser needed only
 * while it assembled the authorize URL and redeemed the code itself. Both are
 * the control plane's job now (`GATEWAY_AUTHORIZE_URL`, server-side), and a
 * variable named for a consent page that no longer starts one here would be a
 * standing invitation to build a second login flow next to the real one.
 */
export const gatewayUrl = (process.env.NEXT_PUBLIC_GATEWAY_URL || '').trim();

export const config = {
  apiBase: agentApiBase,
  appName: process.env.NEXT_PUBLIC_APP_NAME || 'Cloud Agent',
  environment: process.env.NODE_ENV || 'development',
  buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
  buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',
  isDevelopment: process.env.NODE_ENV === 'development',
  isProduction: process.env.NODE_ENV === 'production',
} as const;

export type Config = typeof config;
