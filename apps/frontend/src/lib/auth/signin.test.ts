// @vitest-environment jsdom

// Where sign-in returns the browser to, and how another account signs in.
//
// The prefix rule is what these cases are mostly about. `basePath` is read at
// module scope from `process.env`, so each case re-imports the module with the
// environment already set; `vi.resetModules()` is what makes that re-import
// real rather than a cache hit.

import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadSignin(basePath: string | undefined, apiBase = '') {
  vi.resetModules();
  if (basePath === undefined) delete process.env.NEXT_PUBLIC_BASE_PATH;
  else process.env.NEXT_PUBLIC_BASE_PATH = basePath;
  process.env.NEXT_PUBLIC_AGENT_API_URL = apiBase;
  return import('./signin');
}

afterEach(() => {
  delete process.env.NEXT_PUBLIC_BASE_PATH;
  delete process.env.NEXT_PUBLIC_AGENT_API_URL;
  vi.resetModules();
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

describe('local account navigation', () => {
  it('rejects backslashes and control characters before direct navigation', async () => {
    const { safeReturnTo } = await loadSignin('');
    for (const value of ['/\\evil.test', '/\n/evil.test', '/\t/evil.test'])
      expect(safeReturnTo(value)).toBe('/');
  });
  it('another account signs in on the login page, returning here', async () => {
    const { accountSignInUrl } = await loadSignin('/agents');
    expect(accountSignInUrl('/agents/job')).toBe('/agents/login?return_to=%2Fjob');
    expect(accountSignInUrl(null)).toBe('/agents/login?return_to=%2F');
    expect(accountSignInUrl('//evil.example/x')).toBe('/agents/login?return_to=%2F');
  });
});
