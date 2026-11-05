// Build-time configuration for the cloud agent web app.
//
// Everything here is a property of the *image*, not of a deployment: the
// prefix the browser calls the control plane on. Next resolves it while the
// bundle is compiled, which is why changing it is a rebuild. The app itself is
// always served at the root of its hostname.
//
// The deployment's identity — product name, organisation, status page —
// deliberately does **not** live here. It is read per
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
