// @vitest-environment jsdom

// The redirect URI must name the path the callback is actually served at.
//
// Mounted under `basePath=/agents`, the callback page lives at
// `/agents/callback`. Next.js applies the prefix to the routes and links it
// generates — not to a string this module concatenates. So a `callbackUri()`
// that forgets the prefix names a path that does not exist, and because the
// gateway matches `IDENTITY_ALLOWED_REDIRECTS` as an exact string, the failure
// arrives as a refusal at `/authorize` before the user reaches consent.
//
// `basePath` is read at module scope from `process.env`, so each case
// re-imports the module with the environment already set. `vi.resetModules()`
// is what makes that re-import real rather than a cache hit.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ORIGIN = 'https://staging.freeinference.org';

async function loadSignin(basePath: string | undefined) {
  vi.resetModules();
  if (basePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
  else process.env.NEXT_PUBLIC_BASE_PATH = basePath;
  return import('./signin');
}

describe('callbackUri', () => {
  const original = process.env.NEXT_PUBLIC_BASE_PATH;

  beforeEach(() => {
    Object.defineProperty(window, 'location', {
      value: { origin: ORIGIN, assign: vi.fn() },
      writable: true,
    });
  });

  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
    else process.env.NEXT_PUBLIC_BASE_PATH = original;
    vi.resetModules();
  });

  it('carries the base path the app is mounted under', async () => {
    const { callbackUri } = await loadSignin('/agents');
    expect(callbackUri()).toBe(`${ORIGIN}/agents/callback`);
  });

  it('omits it entirely for a deployment that owns its hostname', async () => {
    const { callbackUri } = await loadSignin(undefined);
    expect(callbackUri()).toBe(`${ORIGIN}/callback`);
  });

  it('never doubles a slash, whatever the prefix was written as', async () => {
    for (const written of ['/agents/', 'agents', '  /agents  ']) {
      const { callbackUri } = await loadSignin(written);
      expect(callbackUri()).toBe(`${ORIGIN}/agents/callback`);
    }
  });

  it('is what the authorize URL actually sends', async () => {
    process.env.NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL = `${ORIGIN}/authorize`;
    const { buildAuthorizeUrl, callbackUri } = await loadSignin('/agents');

    const url = new URL(await buildAuthorizeUrl('/'));

    // The pair that matters: whatever the deployment must allowlist is
    // exactly what goes on the wire. Asserting only on `callbackUri()` would
    // pass while `buildAuthorizeUrl` sent something else.
    expect(url.searchParams.get('redirect_uri')).toBe(callbackUri());
    expect(url.searchParams.get('redirect_uri')).toBe(`${ORIGIN}/agents/callback`);
  });

  it('resolves a local same-origin authorize path in the browser', async () => {
    process.env.NEXT_PUBLIC_GATEWAY_AUTHORIZE_URL = '/authorize';
    const { buildAuthorizeUrl } = await loadSignin('');

    const url = new URL(await buildAuthorizeUrl('/'));

    expect(url.origin + url.pathname).toBe(`${ORIGIN}/authorize`);
    expect(url.searchParams.get('redirect_uri')).toBe(`${ORIGIN}/callback`);
  });
});

// The other direction, and the reason both live in this module.
//
// `callbackUri()` *adds* the prefix because it hands a URL to the gateway.
// `safeReturnTo()` *strips* it because it hands a path to Next's router, which
// prefixes what it is given. Get that backwards and the prefix is applied
// twice: signing in at `/agents` returned to `/agents/agents`, which the app
// resolved as a job whose id was `agents` and rendered as "No such agent job:
// agents". A page that loads, and is wrong.
describe('safeReturnTo', () => {
  const original = process.env.NEXT_PUBLIC_BASE_PATH;

  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
    else process.env.NEXT_PUBLIC_BASE_PATH = original;
    vi.resetModules();
  });

  it('strips the base path the browser reported', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    expect(safeReturnTo('/agents')).toBe('/');
    expect(safeReturnTo('/agents/ajob_123')).toBe('/ajob_123');
    expect(safeReturnTo('/agents/archived?x=1')).toBe('/archived?x=1');
  });

  it('leaves a path that is already internal alone', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    // Not `/agents`-prefixed, so nothing to strip — and stripping a bare
    // prefix match would turn `/agentsomething` into `omething`.
    expect(safeReturnTo('/agentsomething')).toBe('/agentsomething');
    expect(safeReturnTo('/archived')).toBe('/archived');
  });

  it('is a no-op when the app owns its origin', async () => {
    const { safeReturnTo } = await loadSignin('');
    expect(safeReturnTo('/agents/ajob_1')).toBe('/agents/ajob_1');
    expect(safeReturnTo('/archived')).toBe('/archived');
  });

  it('refuses anything that is not a same-site path', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    // A returnTo survives a round trip through another origin, so it is
    // attacker-influenced by construction.
    expect(safeReturnTo('//evil.example/x')).toBe('/');
    expect(safeReturnTo('https://evil.example/x')).toBe('/');
    expect(safeReturnTo(undefined)).toBe('/');
    expect(safeReturnTo('')).toBe('/');
  });
});
