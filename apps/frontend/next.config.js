/** @type {import('next').NextConfig} */

// The source repo's config proxied a dozen gateway paths through Next's
// rewrites because the browser and the API shared an origin by way of this
// process. Here the control plane is addressed directly by
// NEXT_PUBLIC_AGENT_API_URL, and the shipped topology puts it on the same host
// under /api — nothing to rewrite. Deployments that split the two must arrange
// CORS with credentials on the control plane rather than proxy through here.
//
// The app is served at the root of its own hostname, with the control plane
// under `/api` on the same origin, so the session cookie needs no CORS.
// Mounting it under a path prefix inside another site's origin is no longer
// supported (the FreeInference distribution moved to its own hostname on
// 2026-09-11).
//
// A relative API prefix can only be `/api`: anything else sends every request
// somewhere the edge does not route, starting with sign-in, and the app looks
// correct until then. An absolute URL is the cross-origin shape, which needs
// CORS with credentials on the control plane; that is a deployment's decision.
// `variantPairing.test.ts` runs this file to check the rule where it is written.
const rawApiPrefix = (process.env.NEXT_PUBLIC_AGENT_API_URL || '').trim().replace(/\/+$/, '');
if (rawApiPrefix.startsWith('/') && rawApiPrefix !== '/api') {
  throw new Error(
    `NEXT_PUBLIC_AGENT_API_URL is ${JSON.stringify(rawApiPrefix)}; a relative browser API ` +
      'prefix must be "/api", where the edge routes the control plane. Use an absolute URL ' +
      'for a cross-origin deployment.',
  );
}

// Security headers on every response this app serves, documents and build
// assets alike. The Content-Security-Policy is not among them: it carries a
// per-request script nonce, so `src/middleware.ts` sets it, and with it
// `frame-ancestors 'none'`. `X-Frame-Options` says the same for browsers that
// predate `frame-ancestors`. Strict-Transport-Security belongs to whatever
// terminates TLS in front of this process, not to a server that speaks plain
// HTTP behind it (see `deploy/control-plane/edge.nginx.conf`).
//
// `securityHeaders.test.ts` reads these from this file, and the runtime-config
// build check asserts that the built server sends them.
const SECURITY_HEADERS = [
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  // Cross-origin requests and links learn the origin only, never a
  // conversation's path. Same-origin requests keep the full referrer.
  { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
  { key: 'X-Frame-Options', value: 'DENY' },
];

const nextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // No `X-Powered-By`: it tells nobody anything but which framework to probe.
  poweredByHeader: false,
  async headers() {
    return [{ source: '/:path*', headers: SECURITY_HEADERS }];
  },
};

module.exports = nextConfig;
