// Where sign-in returns the browser to, and how another account signs in.

import { describe, expect, it } from 'vitest';

import { accountSignInUrl, safeReturnTo } from './signin';

describe('safeReturnTo', () => {
  it('keeps a rooted same-site path as it is', () => {
    expect(safeReturnTo('/ajob_123')).toBe('/ajob_123');
    expect(safeReturnTo('/archived?x=1')).toBe('/archived?x=1');
  });

  it('sends anything that is not a rooted same-site path to the root', () => {
    expect(safeReturnTo('//evil.example/x')).toBe('/');
    expect(safeReturnTo('https://evil.example/x')).toBe('/');
    expect(safeReturnTo(undefined)).toBe('/');
    expect(safeReturnTo('')).toBe('/');
  });

  it('rejects backslashes and control characters before direct navigation', () => {
    for (const value of ['/\\evil.test', '/\n/evil.test', '/\t/evil.test'])
      expect(safeReturnTo(value)).toBe('/');
  });
});

describe('local account navigation', () => {
  it('another account signs in on the login page, returning here', () => {
    expect(accountSignInUrl('/job')).toBe('/login?return_to=%2Fjob');
    expect(accountSignInUrl(null)).toBe('/login?return_to=%2F');
    expect(accountSignInUrl('//evil.example/x')).toBe('/login?return_to=%2F');
  });
});
