import { afterEach, describe, expect, it, vi } from 'vitest';
import * as client from './client';
import {
  clearProjectMemory,
  consolidateProjectMemory,
  deleteProjectMemoryEntry,
  editProjectMemoryEntry,
  getMemoryProjects,
  getProjectMemory,
  saveProjectMemories,
  updateProjectMemory,
  type ProjectMemoryView,
} from './projectMemory';

afterEach(() => vi.restoreAllMocks());

const repo = 'alice/toolbox & notes?#';
const path = '/v1/agent/project-memory?repo=alice%2Ftoolbox%20%26%20notes%3F%23';
const view: ProjectMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  repo,
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

function response(body: ProjectMemoryView | string[] = view) {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
}

describe('project memory client', () => {
  it('lists the authenticated user’s repositories without caching and supports abort', async () => {
    const projects = [repo, 'alice/other'];
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(projects));
    const controller = new AbortController();

    await expect(getMemoryProjects(controller.signal)).resolves.toEqual(projects);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), '/v1/agent/project-memory/projects', {
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('encodes the complete repository when reading fresh memory and supports abort', async () => {
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response());
    const controller = new AbortController();

    await expect(getProjectMemory(repo, controller.signal)).resolves.toEqual(view);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), path, {
      signal: controller.signal,
      cache: 'no-store',
    });
  });

  it('patches only the preference for the encoded repository and returns the canonical view', async () => {
    const updated = { ...view, enabled: false, revision: 2 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(updateProjectMemory(repo, false, controller.signal)).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: false }),
      signal: controller.signal,
    });
  });

  it('clears only the encoded repository without sending an identity or memory payload', async () => {
    const cleared = { ...view, enabled: false, revision: 3 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(cleared));
    const controller = new AbortController();

    await expect(clearProjectMemory(repo, controller.signal)).resolves.toEqual(cleared);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), path, {
      method: 'DELETE',
      signal: controller.signal,
    });
  });

  it.each([
    ['list', () => getMemoryProjects()],
    ['read', () => getProjectMemory(repo)],
    ['toggle', () => updateProjectMemory(repo, false)],
    ['clear', () => clearProjectMemory(repo)],
    ['edit', () => editProjectMemoryEntry(repo, 'entry-1', 'Updated', 1)],
    ['delete', () => deleteProjectMemoryEntry(repo, 'entry-1', 1)],
    ['save', () => saveProjectMemories(repo, 1, [])],
    ['consolidate', () => consolidateProjectMemory(repo, 1)],
  ])('propagates %s server errors', async (_operation, request) => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Unavailable' }), { status: 503 }),
    );

    await expect(request()).rejects.toMatchObject({ statusCode: 503 });
  });

  it('encodes both repository and entry when editing content with its expected revision', async () => {
    const updated = { ...view, revision: 2 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(
      editProjectMemoryEntry(
        repo,
        'preference/job 1?revision=2',
        '  Updated content.  ',
        1,
        controller.signal,
      ),
    ).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/project-memory/entries/preference%2Fjob%201%3Frevision%3D2?repo=alice%2Ftoolbox%20%26%20notes%3F%23',
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: '  Updated content.  ', expected_revision: 1 }),
        signal: controller.signal,
      },
    );
  });

  it('encodes both repository and entry when deleting with its expected revision', async () => {
    const updated = { ...view, revision: 3 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(
      deleteProjectMemoryEntry(repo, 'task/job#1', 2, controller.signal),
    ).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/project-memory/entries/task%2Fjob%231?repo=alice%2Ftoolbox%20%26%20notes%3F%23',
      {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ expected_revision: 2 }),
        signal: controller.signal,
      },
    );
  });

  it('saves scope corrections with the examined revision and returns the canonical project view', async () => {
    const edits = [{ id: 'preference-1', content: 'Across projects.', scope: 'user' as const }];
    const updated = { ...view, revision: 4 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(saveProjectMemories(repo, 3, edits, controller.signal)).resolves.toEqual(updated);

    expect(fetch).toHaveBeenCalledWith(expect.any(String), path, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision: 3, memories: edits }),
      signal: controller.signal,
    });
  });

  it('consolidates only the encoded project with the examined revision', async () => {
    const updated = { ...view, revision: 5 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(consolidateProjectMemory(repo, 4, undefined, controller.signal)).resolves.toEqual(
      updated,
    );

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/project-memory/consolidate?repo=alice%2Ftoolbox%20%26%20notes%3F%23',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: 4 }),
        signal: controller.signal,
      },
    );
  });

  it('sends the explicitly selected consolidation model for the examined repository revision', async () => {
    const updated = { ...view, revision: 5 };
    const fetch = vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(response(updated));
    const controller = new AbortController();

    await expect(consolidateProjectMemory(repo, 4, 'model-b', controller.signal)).resolves.toEqual(
      updated,
    );

    expect(fetch).toHaveBeenCalledWith(
      expect.any(String),
      '/v1/agent/project-memory/consolidate?repo=alice%2Ftoolbox%20%26%20notes%3F%23',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ revision: 4, model: 'model-b' }),
        signal: controller.signal,
      },
    );
  });

  it.each([
    ['edit', () => editProjectMemoryEntry(repo, 'entry-1', 'Updated', 1)],
    ['delete', () => deleteProjectMemoryEntry(repo, 'entry-1', 1)],
    ['save', () => saveProjectMemories(repo, 1, [])],
    ['consolidate', () => consolidateProjectMemory(repo, 1)],
  ])('propagates intentional aborts from project %s', async (_operation, request) => {
    const abort = new DOMException('Request cancelled', 'AbortError');
    vi.spyOn(client, 'fetchWithAuth').mockRejectedValue(abort);
    await expect(request()).rejects.toBe(abort);
  });

  it.each([
    ['edit', () => editProjectMemoryEntry(repo, 'entry-1', 'Updated', 1)],
    ['delete', () => deleteProjectMemoryEntry(repo, 'entry-1', 1)],
    ['save', () => saveProjectMemories(repo, 1, [])],
    ['consolidate', () => consolidateProjectMemory(repo, 1)],
  ])('preserves revision conflicts from project %s', async (_operation, request) => {
    vi.spyOn(client, 'fetchWithAuth').mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: { code: 'project_memory_conflict', message: 'Refresh before editing.' },
        }),
        { status: 409 },
      ),
    );
    await expect(request()).rejects.toMatchObject({
      code: 'project_memory_conflict',
      message: 'Refresh before editing.',
      statusCode: 409,
    });
  });
});
