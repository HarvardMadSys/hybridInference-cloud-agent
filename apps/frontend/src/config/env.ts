// Build-time configuration for the cloud agent web app.
//
// Everything here is a property of the *image variant*, not of a deployment:
// where the app is mounted, and the prefix the browser calls the control plane
// on. Next resolves both while the bundle is compiled, which is why changing
// either is a rebuild.
//
// The deployment's identity — product name, organisation, status page, the
// gateway's origin — deliberately does **not** live here. It is read per
// request on the server in `site.server.ts`, so one built image can serve
// several deployments without being rebuilt. A `NEXT_PUBLIC_` variable is
// substituted textually at build time, so anything left in this file is
// unreachable from the running container by construction.

/**
 * Control plane base URL (`NEXT_PUBLIC_AGENT_API_URL`).
 *
 * Empty means same-origin, which is the shipped shape: one host serving
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
 * Build identity and run mode.
 *
 * Not site configuration. `buildSha` and `buildTimestamp` describe the image,
 * not the deployment, and are never used as a brand or as a substitute for the
 * deployment's own identity.
 */
export const config = {
  apiBase: agentApiBase,
  environment: process.env.NODE_ENV || 'development',
  buildSha: process.env.NEXT_PUBLIC_BUILD_SHA || '',
  buildTimestamp: process.env.NEXT_PUBLIC_BUILD_TIMESTAMP || '',
  isDevelopment: process.env.NODE_ENV === 'development',
  isProduction: process.env.NODE_ENV === 'production',
} as const;
