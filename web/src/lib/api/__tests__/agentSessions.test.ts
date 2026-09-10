// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  archiveAgentJob,
  deleteAgentSessions,
  listAgentSessions,
  restoreAgentJob,
  renameAgentJob,
  pinAgentJob,
  unpinAgentJob,
  setAgentSessionsArchived,
} from '../agents';
import { onAgentSessionsChanged } from '../agentSessionEvents';
import * as client from '../client';

afterEach(() => vi.restoreAllMocks());

describe('session history API', () => {
  it('sends encoded full-history filters and an abortable cursor request', async () => {
    const fetch = vi
      .spyOn(client, 'fetchWithAuth')
      .mockResolvedValue(
        new Response(JSON.stringify({ sessions: [], projects: [], total: 0, next_cursor: null })),
      );
    const { signal } = new AbortController();
    await listAgentSessions(
      {
        archived: true,
        includeSummary: false,
        limit: 25,
        cursor: 'page/+2=',
        search: 'native & fork',
        repo: 'owner/repo',
        state: 'done',
      },
      signal,
    );
    const query = new URLSearchParams(fetch.mock.calls[0][1].split('?')[1]);
    expect(Object.fromEntries(query)).toEqual({
      archived: 'true',
      include_summary: 'false',
      limit: '25',
      cursor: 'page/+2=',
      search: 'native & fork',
      repo: 'owner/repo',
      state: 'done',
    });
    expect(fetch.mock.calls[0][2]).toEqual({ signal });
  });

  it('announces only committed mutations and sends JSON explicit ID lists', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth');
    const changed = vi.fn();
    const unsubscribe = onAgentSessionsChanged(changed);
    try {
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            sessions: [{ thread_id: 'thread-1', archived: false, archived_at: null }],
          }),
        ),
      );
      await setAgentSessionsArchived(['thread-1'], false);
      expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/v1/agent/sessions/archive', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_ids: ['thread-1'], archived: false }),
      });
      expect(changed).toHaveBeenLastCalledWith({ threadIds: ['thread-1'], archived: false });
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: { error: { type: 'session_active', message: 'Still running' } },
          }),
          { status: 409 },
        ),
      );
      await expect(deleteAgentSessions(['thread-1'])).rejects.toMatchObject({
        code: 'session_active',
        message: 'Still running',
        statusCode: 409,
      });
      expect(changed).toHaveBeenCalledTimes(1);
      fetch.mockResolvedValueOnce(new Response(JSON.stringify({ thread_ids: ['thread-1'] })));
      await deleteAgentSessions(['thread-1']);
      expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/v1/agent/sessions/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_ids: ['thread-1'] }),
      });
      expect(changed).toHaveBeenLastCalledWith({ threadIds: ['thread-1'], deleted: true });
    } finally {
      unsubscribe();
    }
  });

  it('synchronizes saved titles and pins without announcing failed mutations', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth');
    const changed = vi.fn();
    const unsubscribe = onAgentSessionsChanged(changed);
    try {
      fetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ thread_id: 'thread-1', title: 'New title' })),
      );
      await renameAgentJob('job-2', 'New title');
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify({ thread_id: 'thread-1', pinned: true, pinned_at: '2026-09-10' }),
        ),
      );
      await pinAgentJob('job-2');
      fetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ thread_id: 'thread-1', pinned: false, pinned_at: null })),
      );
      await unpinAgentJob('job-2');
      expect(changed.mock.calls.map(([change]) => change)).toEqual([
        { threadIds: ['thread-1'], title: 'New title' },
        { threadIds: ['thread-1'], pinnedAt: '2026-09-10' },
        { threadIds: ['thread-1'], pinnedAt: null },
      ]);
      fetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: 'Save failed' }), { status: 500 }),
      );
      await expect(renameAgentJob('job-2', 'Unsaved title')).rejects.toThrow();
      expect(changed).toHaveBeenCalledTimes(3);
    } finally {
      unsubscribe();
    }
  });

  it('also synchronizes existing single-job archive and restore actions', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth');
    const changed = vi.fn();
    const unsubscribe = onAgentSessionsChanged(changed);
    try {
      fetch.mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            thread_id: 'thread-1',
            archived: true,
            archived_at: '2026-09-10T00:00:00Z',
          }),
        ),
      );
      await archiveAgentJob('job-1');
      fetch.mockResolvedValueOnce(
        new Response(JSON.stringify({ thread_id: 'thread-1', archived: false, archived_at: null })),
      );
      await restoreAgentJob('job-1');
      expect(changed.mock.calls.map(([change]) => change)).toEqual([
        { threadIds: ['thread-1'], archived: true },
        { threadIds: ['thread-1'], archived: false },
      ]);
    } finally {
      unsubscribe();
    }
  });
});
