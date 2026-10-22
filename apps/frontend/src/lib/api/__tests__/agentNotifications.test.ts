import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentNotificationState } from '../agents';
import * as client from '../client';

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('agent notification api', () => {
  let fetchWithAuth: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    fetchWithAuth = vi.spyOn(client, 'fetchWithAuth');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it.each([
    [undefined, undefined, '/v1/agent/notifications/state?limit=200'],
    [
      'snapshot/next+?=',
      37,
      '/v1/agent/notifications/state?limit=37&cursor=snapshot%2Fnext%2B%3F%3D',
    ],
    ['delta-checkpoint', 1, '/v1/agent/notifications/state?limit=1&cursor=delta-checkpoint'],
  ])(
    'reads compact notification state with cursor %s and limit %s',
    async (cursor, limit, path) => {
      const response = {
        mode: cursor ? 'delta' : 'snapshot',
        jobs: [],
        threads: [],
        deleted_job_ids: ['deleted-job'],
        deleted_thread_ids: ['deleted-thread'],
        observed_at: '2026-10-22T00:00:00Z',
        next_cursor: null,
        change_cursor: 'next-checkpoint',
        reset_required: false,
      };
      fetchWithAuth.mockResolvedValue(jsonResponse(response));
      await expect(getAgentNotificationState(cursor, limit)).resolves.toEqual(response);
      expect(fetchWithAuth).toHaveBeenCalledExactlyOnceWith(expect.any(String), path);
    },
  );
});
