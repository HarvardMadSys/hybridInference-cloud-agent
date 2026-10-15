import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAgentAdminSettings, setAgentAdminSettings } from './adminSettings';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin settings client', () => {
  it('reads a fresh settings snapshot and forwards cancellation', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(
        new Response('{"revision":3}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();
    expect(await getAgentAdminSettings(controller.signal)).toEqual({ revision: 3 });
    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/admin/agent/settings', {
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('preserves boolean, string, numeric, and reset values in a patch', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response('{}', { headers: { 'Content-Type': 'application/json' } }));
    const controller = new AbortController();
    const settings = {
      enabled: false,
      limit: 24,
      factor: 1.5,
      title: '',
      password: 'a\nb',
      reset: null,
    };
    await setAgentAdminSettings(settings, 7, controller.signal);
    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/admin/agent/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings, expected_revision: 7 }),
      signal: controller.signal,
    });
  });

  it('surfaces backend validation failures', async () => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response('{"detail":"Unknown setting"}', {
        status: 422,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await expect(setAgentAdminSettings({ unknown: true }, 1)).rejects.toMatchObject({
      statusCode: 422,
    });
  });

  it('uses recovery access without cookies, session redirects, caches, or credentials in the URL', async () => {
    const normal = vi.spyOn(client, 'fetchWithAuth');
    const recover = vi
      .spyOn(client, 'safeFetch')
      .mockImplementation(
        async () =>
          new Response('{"revision":1}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const token = 'test-administrator-recovery-token';
    const signal = new AbortController().signal;
    await getAgentAdminSettings(signal, token);
    await setAgentAdminSettings({ gateway_issuer: 'https://identity.example' }, 1, signal, token);
    expect(normal).not.toHaveBeenCalled();
    expect(recover).toHaveBeenCalledTimes(2);
    for (const [url, init] of recover.mock.calls) {
      expect(url).not.toContain(token);
      expect(init).toMatchObject({
        credentials: 'omit',
        cache: 'no-store',
        signal,
        headers: { Authorization: `Bearer ${token}` },
      });
    }
    expect(recover.mock.calls[1][1]?.body).toBe(
      JSON.stringify({
        settings: { gateway_issuer: 'https://identity.example' },
        expected_revision: 1,
      }),
    );
  });
});
