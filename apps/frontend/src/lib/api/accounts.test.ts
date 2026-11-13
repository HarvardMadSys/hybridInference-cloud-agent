// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AUTH_EXPIRED_EVENT } from './client';
import {
  getAuthConfig,
  loginAccount,
  resetPassword,
  updateAccount,
  type AccountEntry,
} from './accounts';
afterEach(() => vi.unstubAllGlobals());
describe('local account HTTP contract', () => {
  it('uses HttpOnly cookies and JSON, without storing bearer credentials', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ id: 'u', agent_access: { allowed: false, permissions: [] } }),
        ),
      );
    vi.stubGlobal('fetch', fetch);
    await loginAccount('user@example.test', 'Password123');
    const [url, init] = fetch.mock.calls[0];
    expect(url).toContain('/v1/auth/login');
    expect(init.credentials).toBe('include');
    expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
    expect(JSON.parse(init.body)).toEqual({ email: 'user@example.test', password: 'Password123' });
  });
  it('bad login credentials do not sign out a different existing session', async () => {
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { error: { type: 'invalid_credentials', message: 'Invalid credentials.' } },
          }),
          { status: 401 },
        ),
      ),
    );
    await expect(loginAccount('user@example.test', 'wrong')).rejects.toMatchObject({
      code: 'invalid_credentials',
    });
    expect(expired).not.toHaveBeenCalled();
    window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
  });
  it('reports an unavailable sign-in settings service instead of guessing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 503 })));
    await expect(getAuthConfig()).rejects.toMatchObject({ statusCode: 503 });
  });
  it('refuses a control plane without the sign-in settings route rather than guessing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('', { status: 404 })));
    await expect(getAuthConfig()).rejects.toMatchObject({ statusCode: 404 });
  });
  it('keeps reset tokens in a JSON body, never the request URL', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{"ok":true}'));
    vi.stubGlobal('fetch', fetch);
    await resetPassword('mail-token', 'Newpass123');
    expect(fetch.mock.calls[0][0]).not.toContain('mail-token');
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      token: 'mail-token',
      password: 'Newpass123',
    });
  });
  it('sends the exact reviewed version with admin actions and preserves conflicts', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ detail: { error: { type: 'version_conflict', message: 'Changed' } } }),
          { status: 409 },
        ),
      );
    vi.stubGlobal('fetch', fetch);
    const account = { id: 'u', waitlist: { version: 7 } } as AccountEntry;
    await expect(updateAccount(account, 'approve', '')).rejects.toMatchObject({
      code: 'version_conflict',
    });
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      action: 'approve',
      reason: '',
      expected_version: 7,
    });
  });
});
