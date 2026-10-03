import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAdminRun, getAdminRuns, getAdminAttemptResources, getAdminSteps } from './adminRuns';
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

describe('admin steps client', () => {
  it('sends the selection and cursor, but not the list page size, to the steps route', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    await getAdminSteps({
      since: '2026-09-20T00:00:00.000Z',
      until: '2026-09-27T00:00:00.000Z',
      state: 'failed',
      model: 'model&a',
      cursor: 'opaque+cursor',
      limit: 50,
    });
    const url = new URL(fetch.mock.calls[0][1], 'https://agent.example');
    expect(url.pathname).toBe('/admin/agent/steps');
    expect(url.searchParams.get('state')).toBe('failed');
    expect(url.searchParams.get('model')).toBe('model&a');
    expect(url.searchParams.get('cursor')).toBe('opaque+cursor');
    // A steps page counts turns, and its size is the server's to choose.
    expect(url.searchParams.has('limit')).toBe(false);
    expect(fetch.mock.calls[0][2]).toMatchObject({ cache: 'no-store' });
  });
});
