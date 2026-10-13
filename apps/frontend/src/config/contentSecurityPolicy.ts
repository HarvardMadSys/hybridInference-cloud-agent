// The Content-Security-Policy on every document this app serves.
//
// This app shares an origin with the gateway, which keeps its access token in
// `sessionStorage`, so the line that matters is the one
// `Markdown.untrusted.test.tsx` pins: model output and repository content never
// become script. The policy is the second line behind it. A page whose markup
// someone else managed to influence still runs only the scripts this
// deployment served, and cannot be framed into a page that tricks a click.
//
// **Scripts are allowed by nonce, not by origin.** Next renders inline
// bootstrap scripts of its own (the RSC payload pushes), so `'self'` alone
// cannot work, and `'unsafe-inline'` would also allow exactly what the policy
// exists to stop — including navigating to a `javascript:` URL. Instead the
// middleware draws a fresh nonce per request and puts this policy on the
// *request* as well as the response: Next reads the nonce from there and sets
// it on every script it renders. `'strict-dynamic'` then extends that trust to
// the chunks those scripts load, and to nothing parsed out of markup. Every
// route is rendered per request already (`readRuntimeSiteConfig` awaits
// `connection()`), which is what a nonce needs.
//
// The theme boot script in the root layout is allowed by its hash rather than
// by the nonce: it is a fixed string, so the layout need not read the nonce
// and the hash follows the script wherever `themeBootScript()` changes.
//
// The rest names what the app actually uses:
//
//   style-src   `'unsafe-inline'`: React renders `style` attributes, and the
//               toaster injects a stylesheet at runtime;
//   img-src     `data:` for the account avatar, `blob:` for workspace image
//               previews, `https:` for MCP Registry icons, which are
//               publisher-chosen URLs (Markdown images are never fetched);
//   connect-src the control plane: same origin in the shipped topology, plus
//               its origin when the image is built with an absolute API URL;
//   frame-ancestors `'none'`: nothing embeds this app, pgAdmin opens in a tab
//               of its own and the terminal is not a frame.

import { agentApiBase } from '@/config/env';
import { themeBootScript } from '@/lib/theme';

export interface ContentSecurityPolicyOptions {
  /** This request's script nonce, base64. */
  nonce: string;
  /** The theme boot script's CSP hash source, e.g. `sha256-…`. */
  themeScriptHash: string;
  /** The browser's control-plane prefix; an absolute URL adds its origin. */
  apiBase?: string;
  /**
   * `next dev`: React's development build and Next's refresh runtime evaluate
   * code from strings. Never true in a built image.
   */
  development?: boolean;
}

/** The origin of an absolute API base, or null for a same-origin prefix. */
function crossOriginApi(apiBase: string): string | null {
  if (!/^https?:\/\//i.test(apiBase)) return null;
  try {
    return new URL(apiBase).origin;
  } catch {
    return null;
  }
}

/** The policy for one document, as a header value. */
export function contentSecurityPolicy({
  nonce,
  themeScriptHash,
  apiBase = agentApiBase,
  development = false,
}: ContentSecurityPolicyOptions): string {
  const apiOrigin = crossOriginApi(apiBase);
  const directives: [string, string[]][] = [
    ['default-src', ["'self'"]],
    [
      'script-src',
      [
        // Ignored where 'strict-dynamic' is understood; the fallback for a
        // browser that predates it.
        "'self'",
        `'nonce-${nonce}'`,
        `'${themeScriptHash}'`,
        "'strict-dynamic'",
        ...(development ? ["'unsafe-eval'"] : []),
      ],
    ],
    ['style-src', ["'self'", "'unsafe-inline'"]],
    ['img-src', ["'self'", 'data:', 'blob:', 'https:']],
    ['connect-src', ["'self'", ...(apiOrigin ? [apiOrigin] : [])]],
    ['object-src', ["'none'"]],
    ['base-uri', ["'none'"]],
    ['form-action', ["'self'"]],
    ['frame-ancestors', ["'none'"]],
  ];
  return directives.map(([name, sources]) => `${name} ${sources.join(' ')}`).join('; ');
}

/** A fresh script nonce: 128 random bits, base64. */
export function createNonce(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  return btoa(String.fromCharCode(...bytes));
}

let themeHash: Promise<string> | null = null;

/** The `sha256-…` source that allows the theme boot script, computed once. */
export function themeBootScriptHash(): Promise<string> {
  themeHash ??= crypto.subtle
    .digest('SHA-256', new TextEncoder().encode(themeBootScript()))
    .then((digest) => `sha256-${btoa(String.fromCharCode(...new Uint8Array(digest)))}`);
  return themeHash;
}
