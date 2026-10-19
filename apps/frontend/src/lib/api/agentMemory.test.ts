import { beforeEach, describe, expect, it, vi } from 'vitest';
import { expectRequestsMatchContract } from './__tests__/contract';
import {
  addMemory,
  clearMemories,
  deleteMemory,
  getMemories,
  getMemoryProjects,
  restoreMemory,
  setMemoryEnabled,
  updateMemory,
  type AgentMemory,
} from './agentMemory';
import { fetchWithAuth } from './client';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  fetchWithAuth: vi.fn(),
}));

const memory: AgentMemory = {
  id: 'amem/1',
  repo: 'acme/app',
  content: 'Run make check before a PR.',
  description: 'Gate',
  status: 'active',
  origin: 'manual',
  source_job_ids: [],
  version: 7,
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
  last_used_at: null,
  archived_at: null,
};

function answer(body: unknown) {
  vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(JSON.stringify(body)));
}

function lastCall() {
  const [, path, init] = vi.mocked(fetchWithAuth).mock.calls.at(-1)!;
  return { path, init: init ?? {}, body: init?.body ? JSON.parse(String(init.body)) : undefined };
}

describe('agent memory API client', () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
  });

  it('sends only requests the server accepts', async () => {
    answer(memory);
    await getMemories(null);
    await getMemories('acme/app');
    await addMemory(null, 'Prefer uv.');
    await updateMemory(memory, 'Run make check.', 'acme/app');
    await restoreMemory(memory);
    await setMemoryEnabled('acme/app', false);
    answer({ projects: [] });
    await getMemoryProjects();
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(null, { status: 204 }));
    await deleteMemory(memory);
    await clearMemories(null);
    await clearMemories('acme/app');
    expectRequestsMatchContract();
  });

  it('reads a scope by its repository, and the user scope with no query', async () => {
    answer({ repo: null, enabled: true, memories: [], archived: [] });
    await getMemories(null);
    expect(lastCall().path).toBe('/v1/agent/memories');
    await getMemories('acme/app');
    expect(lastCall().path).toBe('/v1/agent/memories?repo=acme%2Fapp');
    // A memory page must never show a list an earlier visit cached.
    expect(lastCall().init.cache).toBe('no-store');
  });

  it('sends the version the page read with every change, so a stale edit is refused', async () => {
    answer(memory);
    await updateMemory(memory, 'Run make check.', null);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/memories/amem%2F1',
      init: { method: 'PATCH' },
      body: { content: 'Run make check.', repo: null, expected_version: 7 },
    });
    await restoreMemory(memory);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/memories/amem%2F1/restore',
      body: { expected_version: 7 },
    });
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(null, { status: 204 }));
    await deleteMemory(memory);
    expect(lastCall()).toMatchObject({
      init: { method: 'DELETE' },
      body: { expected_version: 7 },
    });
  });

  it('clears one scope at a time', async () => {
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(null, { status: 204 }));
    await clearMemories('acme/app');
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/memories?repo=acme%2Fapp',
      init: { method: 'DELETE' },
    });
    expect(lastCall().init.body).toBeUndefined();
  });

  it('unwraps the project list', async () => {
    answer({ projects: ['acme/app', 'acme/web'] });
    await expect(getMemoryProjects()).resolves.toEqual(['acme/app', 'acme/web']);
  });

  it('surfaces a conflict from an empty-answer route as an error', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(
      new Response(
        JSON.stringify({ error: { type: 'memory_conflict', message: 'Changed elsewhere.' } }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    await expect(deleteMemory(memory)).rejects.toMatchObject({
      code: 'memory_conflict',
      statusCode: 409,
    });
  });
});
