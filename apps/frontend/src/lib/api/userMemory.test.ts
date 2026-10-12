import { afterEach, describe, expect, it, vi } from 'vitest';
import * as client from './client';
import {
  clearUserMemory,
  consolidateUserMemory,
  saveUserMemories,
  deleteUserMemoryEntry,
  editUserMemoryEntry,
  getUserMemory,
  updateUserMemory,
  type UserMemoryView,
} from './userMemory';

afterEach(() => vi.restoreAllMocks());

const view: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: { data_url: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', style: 'illustrated' },
  portrait: {
    summary: '',
    projects: [],
    focus_areas: [],
    preferred_runtime: null,
    preferred_model: null,
  },
  memories: [],
  suggestions: [],
};

function response(body = view) {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
}

describe('user memory client', () => {
  it('refreshes the current user through the authenticated API without caching and supports abort', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response());
    const controller = new AbortController();

    await expect(getUserMemory(controller.signal)).resolves.toEqual(view);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/user-memory', {
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('patches only the preference and returns the canonical server view', async () => {
    const updated = { ...view, enabled: false, revision: 2 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(updateUserMemory(false, controller.signal)).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/user-memory', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false }),
      signal: controller.signal,
    });
  });

  it('clears the authenticated user without sending an identity or memory payload', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response());
    const controller = new AbortController();

    await expect(clearUserMemory(controller.signal)).resolves.toEqual(view);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/user-memory', {
      method: 'DELETE',
      signal: controller.signal,
    });
  });

  it('propagates server errors instead of treating failed mutations as successful', async () => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 }),
    );

    await expect(updateUserMemory(false)).rejects.toMatchObject({ statusCode: 503 });
  });

  it('edits an encoded entry with its revision and returns the canonical server view', async () => {
    const updated: UserMemoryView = {
      ...view,
      revision: 2,
      memories: [
        {
          id: 'preference/job 1?revision=2',
          kind: 'preference',
          content: 'Prefer focused changes.',
          source_job_id: 'job-1',
          source_repo: 'owner/repo',
          user_edited: true,
          created_at: null,
        },
      ],
    };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(
      editUserMemoryEntry(
        'preference/job 1?revision=2',
        '  Prefer focused changes.  ',
        1,
        controller.signal,
      ),
    ).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/user-memory/entries/preference%2Fjob%201%3Frevision%3D2',
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: '  Prefer focused changes.  ', expected_revision: 1 }),
        signal: controller.signal,
      },
    );
  });

  it('deletes an encoded entry with its revision and returns the canonical server view', async () => {
    const updated = { ...view, revision: 3, portrait: { ...view.portrait, summary: 'Updated' } };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(deleteUserMemoryEntry('task/job#1', 2, controller.signal)).resolves.toEqual(
      updated,
    );

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/user-memory/entries/task%2Fjob%231',
      {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: 2 }),
        signal: controller.signal,
      },
    );
  });

  it.each(['edit', 'delete'] as const)(
    'propagates intentional aborts from entry %s',
    async (action) => {
      const abort = new DOMException('Request cancelled', 'AbortError');
      vi.spyOn(client, 'fetchWithAuth').mockRejectedValue(abort);

      const mutation =
        action === 'edit'
          ? editUserMemoryEntry('entry-1', 'Updated', 1)
          : deleteUserMemoryEntry('entry-1', 1);

      await expect(mutation).rejects.toBe(abort);
    },
  );

  it.each(['edit', 'delete'] as const)(
    'preserves revision conflict errors from entry %s',
    async (action) => {
      vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: { code: 'user_memory_conflict', message: 'Refresh before editing.' },
          }),
          { status: 409 },
        ),
      );

      const mutation =
        action === 'edit'
          ? editUserMemoryEntry('entry-1', 'Updated', 1)
          : deleteUserMemoryEntry('entry-1', 1);

      await expect(mutation).rejects.toMatchObject({
        code: 'user_memory_conflict',
        message: 'Refresh before editing.',
        statusCode: 409,
      });
    },
  );
});

it('sends bounded list corrections and consolidation with the examined revision', async () => {
  const fetch = vi.spyOn(client, 'fetchWithAuth').mockImplementation(async () => response(view));
  const signal = new AbortController().signal;
  const edits = [{ id: 'preference-1', content: 'Use small changes.', scope: 'repo' as const }];
  await saveUserMemories(1, edits, signal);
  expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/v1/agent/user-memory', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: 1, memories: edits }),
    signal,
  });
  await consolidateUserMemory(2, 'model-b', signal);
  expect(fetch).toHaveBeenLastCalledWith(expect.any(String), '/v1/agent/user-memory/consolidate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: 2, model: 'model-b' }),
    signal,
  });
});

it('allows consolidation without an explicit model for automatic selection', async () => {
  const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response());
  const signal = new AbortController().signal;
  await consolidateUserMemory(2, undefined, signal);
  expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/user-memory/consolidate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision: 2 }),
    signal,
  });
});
