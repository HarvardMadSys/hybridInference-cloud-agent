// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { config } from '@/config/env';
import { createGitHubRepository, type CreatedGitHubRepository } from '../agents';
import { AUTH_EXPIRED_EVENT } from '../client';

function jsonResponse(body: unknown, status = 201): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const createdRepository: CreatedGitHubRepository = {
  repo: 'octocat/new-project',
  web_url: 'https://github.com/octocat/new-project',
  default_branch: 'main',
  accessible: true,
};

describe('createGitHubRepository', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it.each([true, false])(
    'creates a repository with private=%s using the session cookie',
    async (privateRepo) => {
      fetchMock.mockResolvedValue(jsonResponse(createdRepository));
      const request = {
        name: 'new-project',
        description: 'A project created before the first conversation',
        private: privateRepo,
      };

      await expect(createGitHubRepository(request)).resolves.toEqual(createdRepository);

      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledWith(`${config.apiBase}/v1/agent/repositories`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(request),
        credentials: 'include',
      });
      expect(new Headers(fetchMock.mock.calls[0][1].headers).get('Authorization')).toBeNull();
    },
  );

  it('returns an inaccessible repository so the caller can request App access', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ ...createdRepository, accessible: false }));

    await expect(
      createGitHubRepository({ name: 'new-project', private: true }),
    ).resolves.toMatchObject({ repo: createdRepository.repo, accessible: false });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      name: 'new-project',
      private: true,
    });
  });

  it('announces an expired session and does not replay the creation request', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'Not authenticated' }, 401));
    const expired = vi.fn();
    window.addEventListener(AUTH_EXPIRED_EVENT, expired);
    try {
      await expect(
        createGitHubRepository({ name: 'new-project', private: true }),
      ).rejects.toMatchObject({ statusCode: 401, message: 'Not authenticated' });

      expect(expired).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener(AUTH_EXPIRED_EVENT, expired);
    }
  });

  it.each([
    [409, 'github_reconnect_required', 'Reconnect GitHub to create a repository.'],
    [403, 'repository_create_forbidden', 'GitHub did not permit creating this repository.'],
    [409, 'repository_already_exists', 'A repository with this name already exists.'],
    [422, 'repository_create_invalid', 'The repository name is not accepted by GitHub.'],
    [
      503,
      'repository_creation_unavailable',
      'Check GitHub before trying to create this repository again.',
    ],
  ])('preserves the domain error %s/%s without retrying', async (status, code, message) => {
    fetchMock.mockResolvedValue(
      jsonResponse({ detail: { error: { type: code, message } } }, status),
    );

    await expect(
      createGitHubRepository({ name: 'new-project', private: true }),
    ).rejects.toMatchObject({ statusCode: status, code, message });

    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('does not retry a connectivity failure that may follow creation on GitHub', async () => {
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(
      createGitHubRepository({ name: 'new-project', private: true }),
    ).rejects.toMatchObject({ code: 'NETWORK_ERROR' });

    expect(fetchMock).toHaveBeenCalledOnce();
  });
});
