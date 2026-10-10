// The server-only half of the site configuration.
//
// Two things happen here and nowhere else, which is why it is a separate file
// from the parser:
//
// 1. `connection()` marks the render as per-request. Without it the root
//    layout and its `generateMetadata` are evaluated once, during
//    `next build`, and the environment of whatever machine ran the build is
//    baked into every page — the exact failure this work exists to remove.
//    Waiting for the request makes saved appearance edits visible when the
//    next page is rendered.
//
// 2. The control plane's public appearance settings are fetched. `process.env`
//    supplies only its deployment address, image metadata, and a legacy
//    fallback when that address is absent. Importing `next/server` here means a client
//    component that imports this file fails the build instead of quietly
//    shipping server configuration into the browser bundle.
//
// Nothing else may read these variables. `site.ts` holds the parsing so it
// stays testable without a request.

// The marker, not a convention. A Client Component that imports this module
// must fail the build, and `import 'server-only'` is what makes it: the
// package resolves to an empty module under the `react-server` condition and
// to a throwing one otherwise, so the client bundler stops on it. Relying on
// `next/server` alone does not do this — a probe that imported this file from
// a Client Component built successfully without this line.
import 'server-only';

import { connection } from 'next/server';

import { parseRuntimeSiteConfig, parseSavedSiteConfig, type RuntimeSiteConfig } from './site';

/**
 * The site identity for the request being rendered.
 *
 * Async because `connection()` is: the value must not be computed before the
 * request exists, or it is computed once at build time again.
 */
export async function readRuntimeSiteConfig(): Promise<RuntimeSiteConfig> {
  await connection();
  const api = (process.env['AGENT_CONTROL_PLANE_URL'] || '').replace(/\/+$/, '');
  if (api) {
    try {
      const response = await fetch(`${api}/v1/agent/site-config`, {
        cache: 'no-store',
        signal: AbortSignal.timeout(5000),
      });
      if (response.ok) return parseSavedSiteConfig(await response.json(), process.env);
    } catch {
      // Appearance settings must not prevent the administrator opening the
      // settings page during a transient backend failure or malformed response.
    }
    return parseSavedSiteConfig(null, process.env);
  }
  return parseRuntimeSiteConfig(process.env);
}
