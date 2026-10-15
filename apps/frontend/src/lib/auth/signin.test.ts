// @vitest-environment jsdom

// Sign-in starts at the control plane, and the browser's whole contribution is
// a landing path.
//
// The prefix rule is what these cases are mostly about. `basePath` is read at
// module scope from `process.env`, so each case re-imports the module with the
// environment already set; `vi.resetModules()` is what makes that re-import
// real rather than a cache hit.
//
// What is *not* here any more is as important as what is: there is no
// `callbackUri`, no `buildAuthorizeUrl` and no PKCE, because the browser no
// longer assembles the authorize URL or redeems the code. The redirect URI the
// gateway matches is configured on the server (`AGENT_SESSION_REDIRECT_URI`),
// where the browser cannot get it wrong.

import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadSignin(basePath: string | undefined, apiBase = '') {
  vi.resetModules();
  if (basePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
  else process.env.NEXT_PUBLIC_BASE_PATH = basePath;
  process.env.NEXT_PUBLIC_AGENT_API_URL = apiBase;
  return import('./signin');
}

describe('signInUrl', () => {
  const originalBasePath = process.env.NEXT_PUBLIC_BASE_PATH;
  const originalApiBase = process.env.NEXT_PUBLIC_AGENT_API_URL;

  afterEach(() => {
    if (originalBasePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
    else process.env.NEXT_PUBLIC_BASE_PATH = originalBasePath;
    if (originalApiBase === undefined) delete process.env.NEXT_PUBLIC_AGENT_API_URL;
    else process.env.NEXT_PUBLIC_AGENT_API_URL = originalApiBase;
    vi.resetModules();
  });

  it('points at the control plane, not the gateway', async () => {
    const { signInUrl } = await loadSignin('', 'https://agents.example/api');

    const url = new URL(signInUrl('/'));

    expect(url.origin + url.pathname).toBe('https://agents.example/api/v1/session/login');
    expect(url.searchParams.get('return_to')).toBe('/');
  });

  it('is same-origin when no control-plane origin is configured', async () => {
    const { signInUrl } = await loadSignin('', '');

    expect(signInUrl('/')).toBe('/v1/session/login?return_to=%2F');
  });

  it('carries the landing path with the base path stripped', async () => {
    const { signInUrl } = await loadSignin('/agents', '');

    const url = new URL(signInUrl('/agents/ajob_123'), 'https://agents.example');

    expect(url.searchParams.get('return_to')).toBe('/ajob_123');
  });

  it('asks the gateway for its account chooser when switching accounts', async () => {
    const { switchAccountUrl } = await loadSignin('/agents', '');

    const url = new URL(switchAccountUrl('/agents/ajob_123', '01JUSER'), 'https://agents.example');

    expect(url.searchParams.get('return_to')).toBe('/ajob_123');
    expect(url.searchParams.get('prompt')).toBe('select_account');
    expect(url.searchParams.get('login_hint')).toBe('01JUSER');
  });

  it('adds neither prompt nor hint to an ordinary sign-in', async () => {
    const { signInUrl, switchAccountUrl } = await loadSignin('', '');

    expect(signInUrl('/')).toBe('/v1/session/login?return_to=%2F');
    expect(switchAccountUrl('/')).toBe('/v1/session/login?return_to=%2F&prompt=select_account');
  });

  it('refuses to carry an off-site landing path', async () => {
    // `return_to` survives a round trip through the gateway, so it is
    // attacker-influenced. The server checks this too — that copy is the one
    // that counts — but sending it is already wrong.
    const { signInUrl } = await loadSignin('', '');

    for (const hostile of ['//evil.example/x', 'https://evil.example/x']) {
      expect(new URL(signInUrl(hostile), 'https://x.example').searchParams.get('return_to')).toBe(
        '/',
      );
    }
  });
});

// `safeReturnTo` strips `basePath` because it hands a path to a server that
// appends it back. Getting this wrong applied the prefix twice: signing in at
// `/agents` returned to `/agents/agents`, which the app resolved as a job whose
// id was `agents` and rendered as "No such agent job".
describe('safeReturnTo', () => {
  const original = process.env.NEXT_PUBLIC_BASE_PATH;

  afterEach(() => {
    if (original === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
    else process.env.NEXT_PUBLIC_BASE_PATH = original;
    vi.resetModules();
  });

  it('strips the base path the router would re-apply', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    expect(safeReturnTo('/agents')).toBe('/');
    expect(safeReturnTo('/agents/ajob_123')).toBe('/ajob_123');
    expect(safeReturnTo('/agents/archived?x=1')).toBe('/archived?x=1');
  });

  it('leaves a path that merely starts with the same characters alone', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    expect(safeReturnTo('/agentsomething')).toBe('/agentsomething');
    expect(safeReturnTo('/archived')).toBe('/archived');
  });

  it('changes nothing when there is no base path', async () => {
    const { safeReturnTo } = await loadSignin('');
    expect(safeReturnTo('/agents/ajob_1')).toBe('/agents/ajob_1');
    expect(safeReturnTo('/archived')).toBe('/archived');
  });

  it('sends anything that is not a rooted same-site path to the root', async () => {
    const { safeReturnTo } = await loadSignin('/agents');
    expect(safeReturnTo('//evil.example/x')).toBe('/');
    expect(safeReturnTo('https://evil.example/x')).toBe('/');
    expect(safeReturnTo(undefined)).toBe('/');
    expect(safeReturnTo('')).toBe('/');
  });
});

describe('beginSignIn', () => {
  it('navigates rather than routing, because the trip leaves this origin', async () => {
    const { beginSignIn } = await loadSignin('', 'https://agents.example/api');
    const assign = vi.fn();
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign, origin: 'https://agents.example' },
    });

    await beginSignIn('/archived');

    expect(assign).toHaveBeenCalledWith(
      'https://agents.example/api/v1/session/login?return_to=%2Farchived',
    );
  });
});
