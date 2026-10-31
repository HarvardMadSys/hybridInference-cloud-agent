// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ACCOUNT_SWITCHED_EVENT, fetchWithAuth, pinAccount } from '../client';

afterEach(() => {
  pinAccount(null);
  vi.unstubAllGlobals();
});

describe('the account a tab shows', () => {
  it('names the pinned account on every request and nothing when none is pinned', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'));
    vi.stubGlobal('fetch', fetchMock);
    await fetchWithAuth('/api', '/v1/x', { headers: { 'Content-Type': 'application/json' } });
    expect(fetchMock.mock.calls[0][1].headers).toEqual({ 'Content-Type': 'application/json' });
    pinAccount('acct-1');
    await fetchWithAuth('/api', '/v1/x', { headers: { 'Content-Type': 'application/json' } });
    const headers = new Headers(fetchMock.mock.calls[1][1].headers);
    expect(headers.get('X-Agent-Account')).toBe('acct-1');
    expect(headers.get('Content-Type')).toBe('application/json');
  });
  it('tells the app when the browser switched to another account', async () => {
    const body = { detail: { error: { type: 'account_switched', message: 'Reload' } } };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status: 409 })),
    );
    const heard = vi.fn();
    window.addEventListener(ACCOUNT_SWITCHED_EVENT, heard);
    pinAccount('acct-1');
    const response = await fetchWithAuth('/api', '/v1/x');
    window.removeEventListener(ACCOUNT_SWITCHED_EVENT, heard);
    expect(response.status).toBe(409);
    expect(heard).toHaveBeenCalledTimes(1);
    expect((await response.json()).detail.error.type).toBe('account_switched');
  });
});
