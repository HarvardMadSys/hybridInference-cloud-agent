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
  headers: { key: string; value: string }[];
}

interface Declared {
  poweredByHeader: unknown;
  rules: HeaderRule[];
}

/** What `next.config.js` declares when built as the image is. */
function declared(): Declared {
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
        NEXT_PUBLIC_AGENT_API_URL: '/api',
      },
      encoding: 'utf8',
    },
  );
  return JSON.parse(output) as Declared;
}

describe('the security headers every response carries', () => {
  it('covers every path', () => {
    const { rules } = declared();

    // One rule over the whole app.
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe('/:path*');
    expect(Object.fromEntries(rules[0].headers.map(({ key, value }) => [key, value]))).toEqual({
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
      'X-Frame-Options': 'DENY',
    });
  });

  it('does not advertise the framework', () => {
    expect(declared().poweredByHeader).toBe(false);
  });
});
