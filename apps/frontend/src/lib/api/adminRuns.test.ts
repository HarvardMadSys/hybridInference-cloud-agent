import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAdminRun, getAdminRuns, getAdminAttemptResources } from './adminRuns';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin observation client', () => {
  it('uses only admin routes with abort and no-store, and encodes list filter values', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();
    await getAdminRuns(
      {
        user_id: 'person&admin',
        repo: 'org/repo',
        state: 'failed',
        cursor: 'opaque+cursor',
        limit: 50,
      },
      controller.signal,
    );
    const url = new URL(fetch.mock.calls[0][1], 'https://agent.example');
    expect(url.pathname).toBe('/admin/agent/runs');
    expect(url.searchParams.get('user_id')).toBe('person&admin');
    expect(url.searchParams.get('repo')).toBe('org/repo');
    expect(url.searchParams.get('cursor')).toBe('opaque+cursor');
    await getAdminRun('job/1', controller.signal);
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/admin/agent/runs/job%2F1', {
      signal: controller.signal,
      cache: 'no-store',
    });
    await getAdminAttemptResources('job/1', 8, controller.signal);
    expect(fetch.mock.lastCall?.[1]).toBe('/admin/agent/runs/job%2F1/attempts/8/resources');
    expect(fetch.mock.calls.every(([, , init]) => init?.method !== 'POST')).toBe(true);
  });
});
