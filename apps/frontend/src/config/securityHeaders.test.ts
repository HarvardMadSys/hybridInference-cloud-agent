// The static security headers, read from `next.config.js` itself.
//
// Run in a subprocess for the reason `variantPairing.test.ts` gives: the config
// reads `process.env` when it loads, and a developer shell that exports a
// variant's build arguments must not decide what this proves. The policy with
// the nonce is middleware's and is tested in `contentSecurityPolicy.test.ts`.

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const CONFIG_PATH = fileURLToPath(new URL('../../next.config.js', import.meta.url));

interface HeaderRule {
  source: string;
  headers: { key: string; value: string }[];
}

function headerRules(basePath: string, apiUrl: string): HeaderRule[] {
  const output = execFileSync(
    process.execPath,
    [
      '-e',
      `require(${JSON.stringify(CONFIG_PATH)}).headers().then((rules) => ` +
        'process.stdout.write(JSON.stringify(rules)))',
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
  return JSON.parse(output) as HeaderRule[];
}

describe('the security headers every response carries', () => {
  it.each([
    ['the root variant', '', '/api'],
    ['the /agents variant', '/agents', '/agents/api'],
  ])('covers every path of %s', (_case, basePath, apiUrl) => {
    const rules = headerRules(basePath, apiUrl);

    // One rule over the whole app. `source` is relative to `basePath`, so the
    // same pattern is the whole mount in either variant.
    expect(rules).toHaveLength(1);
    expect(rules[0].source).toBe('/:path*');
    expect(Object.fromEntries(rules[0].headers.map(({ key, value }) => [key, value]))).toEqual({
      'X-Content-Type-Options': 'nosniff',
      'Referrer-Policy': 'strict-origin-when-cross-origin',
      'X-Frame-Options': 'DENY',
    });
  });
});
