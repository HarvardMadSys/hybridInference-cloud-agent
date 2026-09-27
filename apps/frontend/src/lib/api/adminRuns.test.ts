import { afterEach, describe, expect, it, vi } from 'vitest';

import { getAdminRun, getAdminAttemptResources, getAdminLlmRequests } from './adminRuns';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

describe('admin observation client', () => {
  it('uses only admin routes with abort and no-store, and encodes filter values', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockImplementation(
        async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
      );
    const controller = new AbortController();
    await getAdminRun('job/1', controller.signal);
    expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/admin/agent/runs/job%2F1', {
      signal: controller.signal,
      cache: 'no-store',
    });
    await getAdminAttemptResources('job/1', 8, controller.signal);
    expect(fetch.mock.lastCall?.[1]).toBe('/admin/agent/runs/job%2F1/attempts/8/resources');
    await getAdminLlmRequests(
      'job/1',
      { source_id: 'source&1', cursor: 'opaque+cursor', limit: 50 },
      controller.signal,
    );
    const url = new URL(fetch.mock.lastCall![1], 'https://agent.example');
    expect(url.pathname).toBe('/admin/agent/runs/job%2F1/llm-requests');
    expect(url.searchParams.get('source_id')).toBe('source&1');
    expect(url.searchParams.get('cursor')).toBe('opaque+cursor');
    expect(fetch.mock.calls.every(([, , init]) => init?.method !== 'POST')).toBe(true);
  });
});
