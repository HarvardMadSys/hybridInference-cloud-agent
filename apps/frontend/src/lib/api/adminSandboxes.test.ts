import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  archiveAdminSandbox,
  deleteAdminSandbox,
  getAdminSandbox,
  getAdminSandboxes,
  restoreAdminSandbox,
  stopAdminSandbox,
} from './adminSandboxes';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin sandbox inventory client', () => {
  it('filters the complete inventory by lifecycle state', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(new Response('{}'));
    await getAdminSandboxes({ state: 'all', limit: 200 });
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes?limit=200&state=all');
  });

  it('explicitly includes removed rows for inventory recovery', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(new Response('{}'));
    await getAdminSandboxes({ state: 'all', include_archived: true });
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes?state=all&include_archived=true');
  });

  it('reads member metadata with an encoded sandbox identity and abort support', async () => {
    const response = { sandbox: { id: 'sandbox/one' }, members: [], observed_at: 'now' };
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response(JSON.stringify(response)));
    const controller = new AbortController();
    expect(await getAdminSandbox('sandbox/one', controller.signal)).toEqual(response);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes/sandbox%2Fone');
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('posts exactly the reviewed identity and activity fence to stop a sandbox', async () => {
    const response = { sandbox: { state: 'stopping' }, members: [], observed_at: 'now' };
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response(JSON.stringify(response), { status: 202 }));
    const reviewed = {
      generation: 3,
      container_id: 'container-one',
      last_activity_at: '2026-10-10T09:59:00Z',
    };
    const controller = new AbortController();
    expect(await stopAdminSandbox('sandbox/one', reviewed, controller.signal)).toEqual(response);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes/sandbox%2Fone/stop');
    expect(fetch.mock.calls[0][2]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reviewed),
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('requests S3 archival and physical deletion with the reviewed container and abort signal', async () => {
    const response = {
      sandbox: { state: 'stopped', deletion_state: 'pending', deletion_request_id: 'request-one' },
      members: [],
      observed_at: 'now',
    };
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(new Response(JSON.stringify(response), { status: 202 }));
    const reviewed = {
      generation: 3,
      container_id: 'container-one',
      last_activity_at: '2026-10-10T09:59:00Z',
    };
    const controller = new AbortController();
    expect(await deleteAdminSandbox('sandbox/one', reviewed, controller.signal)).toEqual(response);
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/sandboxes/sandbox%2Fone/delete');
    expect(fetch.mock.calls[0][2]).toEqual({
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reviewed),
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it.each([
    ['archive', archiveAdminSandbox],
    ['restore', restoreAdminSandbox],
  ] as const)(
    'posts a reviewed nullable container fence to %s inventory membership',
    async (route, update) => {
      const response = {
        sandbox: { state: 'stopped', archived_at: route === 'archive' ? 'now' : null },
        members: [],
        observed_at: 'now',
      };
      const fetch = vi
        .spyOn(client, 'fetchWithAuth')
        .mockResolvedValue(new Response(JSON.stringify(response)));
      const reviewed = {
        generation: 3,
        container_id: null,
        last_activity_at: '2026-10-10T09:59:00Z',
      };
      const controller = new AbortController();
      expect(await update('sandbox/one', reviewed, controller.signal)).toEqual(response);
      expect(fetch.mock.calls[0][1]).toBe(`/admin/agent/sandboxes/sandbox%2Fone/${route}`);
      expect(fetch.mock.calls[0][2]).toEqual({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(reviewed),
        signal: controller.signal,
        cache: 'no-store',
      });
    },
  );

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
