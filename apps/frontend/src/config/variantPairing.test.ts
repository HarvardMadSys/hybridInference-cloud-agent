// The variant's browser API prefix, asserted where it is written.
//
// `next.config.js` is plain CommonJS loaded before any TypeScript exists, so
// this test runs the real file in a subprocess instead of restating the rule.
// A restatement is what `basePath.test.ts` has to do for normalization — it
// cannot import a CommonJS module into the TS pipeline — and a copy keeps
// passing after the file it copies has changed. The throw under test happens
// while the module reads `process.env` at load time, so it cannot be caught in
// this process either: a fresh `node` per case is the cheap way to see it.

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const CONFIG_PATH = fileURLToPath(new URL('../../next.config.js', import.meta.url));

interface Loaded {
  ok: boolean;
  output: string;
}

/**
 * Load `next.config.js` with exactly the two build arguments a variant sets.
 *
 * The inherited environment is not used: a developer shell that happens to
 * export one of these would otherwise decide what this test proves.
 */
function loadVariant(basePath: string, apiUrl: string): Loaded {
  try {
    const output = execFileSync(
      process.execPath,
      ['-e', `require(${JSON.stringify(CONFIG_PATH)})`],
      {
        env: {
          // Next's own types require this key; the config file does not read it.
          NODE_ENV: process.env.NODE_ENV || 'test',
          PATH: process.env.PATH,
          NEXT_PUBLIC_BASE_PATH: basePath,
          NEXT_PUBLIC_AGENT_API_URL: apiUrl,
        },
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    );
    return { ok: true, output };
  } catch (error) {
    const failure = error as { stdout?: string; stderr?: string };
    return { ok: false, output: `${failure.stdout ?? ''}${failure.stderr ?? ''}` };
  }
}

describe('the variant API-prefix assertion', () => {
  it.each([
    ['the root variant', '', '/api'],
    ['the /agents variant', '/agents', '/agents/api'],
    ['trailing slashes on either value', '/agents/', '/agents/api/'],
    ['a prefix that arrives without its leading slash', 'agents', '/agents/api'],
  ])('accepts %s', (_case, basePath, apiUrl) => {
    expect(loadVariant(basePath, apiUrl).ok).toBe(true);
  });

  it.each([
    ['the root prefix inside the /agents variant', '/agents', '/api'],
    ['the /agents prefix inside the root variant', '', '/agents/api'],
    ['a prefix belonging to neither variant', '/agents', '/v1'],
  ])('refuses %s', (_case, basePath, apiUrl) => {
    expect(loadVariant(basePath, apiUrl).ok).toBe(false);
  });

  it('names the value it got and the one the variant needs', () => {
    const { output } = loadVariant('/agents', '/api');
    expect(output).toContain('AGENT_WEB_API_URL');
    expect(output).toContain('"/api"');
    expect(output).toContain('"/agents/api"');
  });

  it('leaves an absolute prefix alone', () => {
    // The cross-origin shape. It cannot be derived from a mount point — it
    // needs CORS with credentials on the control plane — so this file has
    // nothing to say about it.
    expect(loadVariant('/agents', 'https://api.example.org').ok).toBe(true);
  });

  it('leaves an unset prefix alone', () => {
    // Pinned deliberately: an unset prefix is the same-origin shape, and under
    // a mount prefix the compose file is what refuses it — `AGENT_WEB_API_URL`
    // is a required variable there. Guessing a deployment's proxy topology
    // here would refuse a shape this file cannot see.
    expect(loadVariant('', '').ok).toBe(true);
  });
});
