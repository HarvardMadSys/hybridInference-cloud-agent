import { afterEach, describe, expect, it, vi } from 'vitest';
import * as client from './client';
import {
  clearUserMemory,
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
});
