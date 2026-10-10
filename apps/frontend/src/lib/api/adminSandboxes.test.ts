import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAdminSandboxes } from './adminSandboxes';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin sandbox inventory client', () => {
  it('uses an admin-only uncached read with encoded pagination and abort support', async () => {
    const response = {
      sandboxes: [],
      next_cursor: 'next+page',
      observed_at: '2026-10-10T10:00:00Z',
      total_count: 150,
    };
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(
        new Response(JSON.stringify(response), { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();
    expect(
      await getAdminSandboxes({ cursor: 'opaque+cursor&next', limit: 100 }, controller.signal),
    ).toEqual(response);
    const url = new URL(fetch.mock.calls[0][1], 'https://agent.example');
    expect(url.pathname).toBe('/admin/agent/sandboxes');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      cursor: 'opaque+cursor&next',
      limit: '100',
    });
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('requests the full cross-user inventory when no filters are supplied', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response('{"sandboxes":[],"next_cursor":null,"total_count":0}', {
        headers: { 'Content-Type': 'application/json' },
      }),
    );
    await getAdminSandboxes();
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes');
    expect(fetch.mock.calls[0][2]?.cache).toBe('no-store');
    expect(fetch.mock.calls[0][2]?.method).toBeUndefined();
  });

  it.each([401, 403])(
    'preserves HTTP %i so the page can clear private inventory',
    async (status) => {
      vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
        new Response('{"detail":"Admin access required"}', {
          status,
          headers: { 'Content-Type': 'application/json' },
        }),
      );
      await expect(getAdminSandboxes()).rejects.toMatchObject({ statusCode: status });
    },
  );
});
