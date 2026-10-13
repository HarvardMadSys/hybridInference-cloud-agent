// The response headers the web app declares, read from the real
// `next.config.js` for each variant.
//
// The file is plain CommonJS that reads its build arguments when it loads, so
// it runs in a fresh `node` per case, as in `variantPairing.test.ts`. Whether
// the built server sends them is checked by
// `scripts/check-runtime-site-config.mjs`, which boots both variants.

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const CONFIG_PATH = fileURLToPath(new URL('../../next.config.js', import.meta.url));

interface Declared {
  poweredByHeader: unknown;
  rules: { source: string; basePath?: boolean; headers: { key: string; value: string }[] }[];
}

/** What `next.config.js` declares when built as one variant. */
function declared(basePath: string, apiUrl: string): Declared {
  const script =
    `const config = require(${JSON.stringify(CONFIG_PATH)});` +
    'Promise.resolve(config.headers ? config.headers() : []).then((rules) =>' +
    ' process.stdout.write(JSON.stringify({ poweredByHeader: config.poweredByHeader, rules })));';
  const output = execFileSync(process.execPath, ['-e', script], {
    env: {
      NODE_ENV: process.env.NODE_ENV || 'test',
      PATH: process.env.PATH,
      NEXT_PUBLIC_BASE_PATH: basePath,
      NEXT_PUBLIC_AGENT_API_URL: apiUrl,
    },
    encoding: 'utf8',
  });
  return JSON.parse(output) as Declared;
}

describe.each([
  ['the root variant', '', '/api'],
  ['the /agents variant', '/agents', '/agents/api'],
])('the response headers of %s', (_variant, basePath, apiUrl) => {
  it('cover every path under the mount', () => {
    // Next prefixes `source` with the base path unless a rule sets
    // `basePath: false`, so `/:path*` is the variant's whole tree.
    const { rules } = declared(basePath, apiUrl);
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe('/:path*');
    expect(rules[0].basePath).toBeUndefined();
  });

  // The app can share the gateway's origin. Another site framing it, a sniffed
  // response, or an injected plugin or <base> would be a hole in that origin.
  it('refuse framing by other sites, sniffing, plugins and <base>', () => {
    const { rules } = declared(basePath, apiUrl);
    expect(Object.fromEntries(rules[0].headers.map(({ key, value }) => [key, value]))).toEqual({
      'Content-Security-Policy': "frame-ancestors 'self'; object-src 'none'; base-uri 'none'",
      'X-Frame-Options': 'SAMEORIGIN',
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
    });
  });

  it('do not advertise the framework', () => {
    expect(declared(basePath, apiUrl).poweredByHeader).toBe(false);
  });
});
