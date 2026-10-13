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

import { cache } from 'react';
import { connection } from 'next/server';

import {
  parseRuntimeSiteConfig,
  parseSavedSiteConfig,
  parseSavedPublicAppearance,
  type RuntimeSiteConfig,
} from './site';

// Appearance is public display data, not session state or admission policy.
// Keep it only as a failure fallback: every render still asks for fresh saved
// settings. Keying by the control-plane URL keeps deployments separate, and
// omitting deployment metadata lets each render attach its current image data.
type PublicAppearance = Pick<RuntimeSiteConfig, 'branding' | 'features'>;
let lastKnownGood: { api: string; sequence: number; appearance: PublicAppearance } | null = null;
let readSequence = 0;
const APPEARANCE_READ_BUDGET_MS = 400;

async function readPublicAppearance(api: string): Promise<PublicAppearance | null> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<null>((resolve) => {
    timer = setTimeout(() => {
      resolve(null);
      controller.abort();
    }, APPEARANCE_READ_BUDGET_MS);
  });
  const read = (async () => {
    const response = await fetch(`${api}/v1/agent/site-config`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (!response.ok) return null;
    return parseSavedPublicAppearance(await response.json());
  })();

  try {
    // Bound the whole response, including its JSON body, even if cancellation
    // is delayed. Late responses have no side effects on the saved fallback.
    return await Promise.race([read, deadline]);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The site identity for the request being rendered.
 *
 * Async because `connection()` is: the value must not be computed before the
 * request exists, or it is computed once at build time again.
 *
 * Metadata and the root layout read this separately. Share their promise for
 * this render, including the abort signal that opts out of Next's fetch
 * deduplication. React discards this cache between requests, so appearance
 * edits remain visible on the next page load.
 */
export const readRuntimeSiteConfig = cache(async (): Promise<RuntimeSiteConfig> => {
  await connection();
  const api = (process.env['AGENT_CONTROL_PLANE_URL'] || '').replace(/\/+$/, '');
  if (api) {
    const sequence = ++readSequence;
    const appearance = await readPublicAppearance(api);
    if (appearance) {
      // An older overlapping render must not replace a newer successful read.
      if (sequence > (lastKnownGood?.sequence ?? 0)) {
        lastKnownGood = { api, sequence, appearance };
      }
      return parseSavedSiteConfig(appearance, process.env);
    }
    // A slow or unavailable backend must not hold every page for seconds or
    // reset a known deployment's branding. Cold starts use neutral defaults.
    return parseSavedSiteConfig(
      lastKnownGood?.api === api ? lastKnownGood.appearance : null,
      process.env,
    );
  }
  return parseRuntimeSiteConfig(process.env);
});
