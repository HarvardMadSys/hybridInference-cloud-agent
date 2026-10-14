// The static security headers, read from `next.config.js` itself.
//
// Run in a subprocess for the reason `variantPairing.test.ts` gives: the config
// reads `process.env` when it loads, and a developer shell that exports a
// variant's build arguments must not decide what this proves. The policy with
// the nonce is middleware's and is tested in `contentSecurityPolicy.test.ts`.
// Whether the built server sends these is checked by
// `scripts/check-runtime-site-config.mjs`, which boots both variants.

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const CONFIG_PATH = fileURLToPath(new URL('../../next.config.js', import.meta.url));

interface HeaderRule {
  source: string;
  basePath?: boolean;
  headers: { key: string; value: string }[];
}

interface Declared {
  poweredByHeader: unknown;
  rules: HeaderRule[];
}

/** What `next.config.js` declares when built as one variant. */
function declared(basePath: string, apiUrl: string): Declared {
  const output = execFileSync(
    process.execPath,
    [
      '-e',
      `const config = require(${JSON.stringify(CONFIG_PATH)});` +
        'config.headers().then((rules) => process.stdout.write(' +
        'JSON.stringify({ poweredByHeader: config.poweredByHeader, rules })))',
    ],
    {
      env: {
        NODE_ENV: process.env.NODE_ENV || 'test',
        PATH: process.env.PATH,
        NEXT_PUBLIC_BASE_PATH: basePath,
        NEXT_PUBLIC_AGENT_API_URL: apiUrl,
      },
      encoding: 'utf8',
    },
  );
  return JSON.parse(output) as Declared;
}

const VARIANTS: [string, string, string][] = [
  ['the root variant', '', '/api'],
  ['the /agents variant', '/agents', '/agents/api'],
];

describe('the security headers every response carries', () => {
  it.each(VARIANTS)('covers every path of %s', (_case, basePath, apiUrl) => {
    const { rules } = declared(basePath, apiUrl);

    // One rule over the whole app. `source` is relative to `basePath`, so the
    // same pattern is the whole mount in either variant. A rule that set
    // `basePath: false` would no longer be prefixed, and `/:path*` would stop
    // meaning the variant's tree.
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe('/:path*');
    expect(rules[0].basePath).toBeUndefined();
    expect(Object.fromEntries(rules[0].headers.map(({ key, value }) => [key, value]))).toEqual({
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
      'X-Frame-Options': 'DENY',
    });
  });

  it.each(VARIANTS)('does not advertise the framework in %s', (_case, basePath, apiUrl) => {
    expect(declared(basePath, apiUrl).poweredByHeader).toBe(false);
  });
});
