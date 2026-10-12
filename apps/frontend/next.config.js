/** @type {import('next').NextConfig} */

// The source repo's config proxied a dozen gateway paths through Next's
// rewrites because the browser and the API shared an origin by way of this
// process. Here the control plane is addressed directly by
// NEXT_PUBLIC_AGENT_API_URL, and the shipped topology puts it on the same host
// under /api — nothing to rewrite. Deployments that split the two must arrange
// CORS with credentials on the control plane rather than proxy through here.
//
// `basePath` mounts the whole app under a prefix, which is how it shares the
// gateway's hostname instead of taking one of its own:
//
//   staging.freeinference.org/agents/*     → this app
//   staging.freeinference.org/agents/api/* → the control plane
//   everything else                        → the gateway
//
// Same origin, so the session cookie needs no CORS and no DNS record is
// involved — and a rollback is a reverse-proxy edit rather than a DNS change
// waiting out a TTL.
//
// Baked in at build time: Next resolves this when the bundle is built, so a
// deployment that changes it rebuilds the image. That is why it is a build
// argument in the compose file and not a runtime variable.
//
// Duplicated from `src/config/env.ts` rather than imported — this file is
// plain CommonJS loaded before the TS pipeline exists. `variantPairing.test.ts`
// executes this config to check the supported base-path and API-prefix pairs.
const rawBasePath = (process.env.NEXT_PUBLIC_BASE_PATH || '').trim().replace(/\/+$/, '');
const basePath = !rawBasePath ? '' : rawBasePath.startsWith('/') ? rawBasePath : `/${rawBasePath}`;

// The browser's API prefix belongs to the variant, not to the deployment:
//
//   basePath ""         -> /api
//   basePath "/agents"  -> /agents/api
//
// A relative prefix that disagrees with the mount point is not a configuration
// a deployment can be in and still work. The app renders, and then every
// request it makes goes somewhere the reverse proxy does not route — starting
// with the sign-in link, which is an absolute path resolved against the origin
// rather than against the page. The symptom is a 404 on a page that looks
// correct, so the build refuses instead of shipping it.
//
// Only a *relative* disagreement is refused. Empty is the same-origin shape a
// deployment that owns its hostname uses, and an absolute URL is the
// cross-origin shape, which needs CORS with credentials on the control plane —
// a deployment's decision rather than this file's.
//
// `variantPairing.test.ts` runs this file under a crafted environment, so the
// rule is tested where it is written rather than restated in a copy.
const rawApiPrefix = (process.env.NEXT_PUBLIC_AGENT_API_URL || '').trim().replace(/\/+$/, '');
const derivedApiPrefix = `${basePath}/api`;
if (rawApiPrefix.startsWith('/') && rawApiPrefix !== derivedApiPrefix) {
  throw new Error(
    `NEXT_PUBLIC_AGENT_API_URL is ${JSON.stringify(rawApiPrefix)}, but this image is built ` +
      `with basePath ${JSON.stringify(basePath)}, whose browser API prefix is ` +
      `${JSON.stringify(derivedApiPrefix)}. A relative API prefix has to belong to the ` +
      `variant it is built into: set AGENT_WEB_API_URL to ` +
      `${JSON.stringify(derivedApiPrefix)}, or to an absolute URL for a cross-origin ` +
      `deployment.`,
  );
}

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // Omitted entirely when empty: Next rejects `basePath: ''`, and empty means
  // a deployment that owns its hostname.
  ...(basePath ? { basePath } : {}),
};

module.exports = nextConfig;
