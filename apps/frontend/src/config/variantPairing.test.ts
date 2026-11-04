// The image's browser API prefix, asserted where it is written.
//
// `next.config.js` is plain CommonJS loaded before any TypeScript exists, so
// this test runs the real file in a subprocess instead of restating the rule.
// A copy would keep passing after the file it copies has changed. The throw
// under test happens while the module reads `process.env` at load time, so a
// fresh `node` per case is the cheap way to see it.

import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

const CONFIG_PATH = fileURLToPath(new URL('../../next.config.js', import.meta.url));

interface Loaded {
  ok: boolean;
  output: string;
}

/**
 * Load `next.config.js` with exactly the build argument an image sets.
 *
 * The inherited environment is not used: a developer shell that happens to
 * export one of these would otherwise decide what this test proves.
 */
function loadVariant(apiUrl: string): Loaded {
  try {
    const output = execFileSync(
      process.execPath,
      ['-e', `require(${JSON.stringify(CONFIG_PATH)})`],
      {
        env: {
          // Next's own types require this key; the config file does not read it.
          NODE_ENV: process.env.NODE_ENV || 'test',
          PATH: process.env.PATH,
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

describe('the browser API-prefix assertion', () => {
  it.each([
    ['the same-origin prefix', '/api'],
    ['a trailing slash', '/api/'],
  ])('accepts %s', (_case, apiUrl) => {
    expect(loadVariant(apiUrl).ok).toBe(true);
  });

  it.each([
    ['the retired /agents prefix', '/agents/api'],
    ['any other relative prefix', '/v1'],
  ])('refuses %s', (_case, apiUrl) => {
    expect(loadVariant(apiUrl).ok).toBe(false);
  });

  it('names the value it got and the one it needs', () => {
    const { output } = loadVariant('/agents/api');
    expect(output).toContain('NEXT_PUBLIC_AGENT_API_URL');
    expect(output).toContain('"/agents/api"');
    expect(output).toContain('"/api"');
  });

  it('leaves an absolute prefix alone', () => {
    // The cross-origin shape: it needs CORS with credentials on the control
    // plane, which is a deployment's decision, not this file's.
    expect(loadVariant('https://api.example.org').ok).toBe(true);
  });

  it('leaves an unset prefix alone', () => {
    expect(loadVariant('').ok).toBe(true);
  });
});
