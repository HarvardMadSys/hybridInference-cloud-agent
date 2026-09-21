import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAdminUsage } from './adminUsage';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin usage client', () => {
  it('reads the aggregate route with no-store and omits empty filters', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();
    await getAdminUsage(
      {
        since: '2026-09-03T00:00:00.000Z',
        until: '2026-09-10T00:00:00.000Z',
        user_id: 'person&admin',
        model: '',
        runtime: undefined,
        repo: 'org/repo',
      },
      controller.signal,
    );
    const path = fetch.mock.calls[0][1];
    const url = new URL(path, 'https://agent.example');
    expect(url.pathname).toBe('/admin/agent/usage');
    expect(url.searchParams.get('since')).toBe('2026-09-03T00:00:00.000Z');
    expect(url.searchParams.get('until')).toBe('2026-09-10T00:00:00.000Z');
    expect(url.searchParams.get('user_id')).toBe('person&admin');
    expect(url.searchParams.get('repo')).toBe('org/repo');
    // An absent filter is omitted rather than sent as an empty selector.
    expect(url.searchParams.has('model')).toBe(false);
    expect(url.searchParams.has('runtime')).toBe(false);
    expect(fetch.mock.calls[0][2]).toEqual({ signal: controller.signal, cache: 'no-store' });
  });

  it('sends no query at all when every filter is absent', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    await getAdminUsage({});
    expect(fetch.mock.calls[0][1]).toBe('/admin/agent/usage');
  });
});
