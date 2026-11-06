import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJobGit,
  getAgentJobGitMetadata,
  type AgentGitWorkspaceApi,
} from '@/lib/api/agents';
import { clearWorkspaceGitCache, loadWorkspaceGit } from './workspaceGitCache';

vi.mock('@/lib/api/agents', () => ({
  getAgentJobGit: vi.fn(),
  getAgentJobGitMetadata: vi.fn(),
}));

const revisionA = 'a'.repeat(64);
const revisionB = 'b'.repeat(64);

function target(id: string, revision = revisionA, sandboxId = 'asbx_shared') {
  return { id, sandboxId, threadId: `thread-${id}`, workspaceGitRevision: revision };
}

function view(revision = revisionA): AgentGitWorkspaceApi {
  return {
    available: true,
    revision,
    branch: 'agent/work',
    changes: [{ code: ' M', path: 'src/app.ts' }],
    change_count: 1,
    patch: 'diff --git a/src/app.ts b/src/app.ts\n',
    commits: [],
  };
}

beforeEach(() => {
  clearWorkspaceGitCache();
  vi.resetAllMocks();
});

describe('workspace Git cache', () => {
  it('reuses one full view when the Changes panel is reopened', async () => {
    vi.mocked(getAgentJobGit).mockResolvedValue(view());

    const first = await loadWorkspaceGit(target('job-one'));
    const reopened = await loadWorkspaceGit(target('job-one'));

    expect(reopened).toBe(first);
    expect(getAgentJobGit).toHaveBeenCalledTimes(1);
    expect(getAgentJobGitMetadata).not.toHaveBeenCalled();
  });

  it('coalesces concurrent Fork siblings sharing a workspace revision', async () => {
    let resolve!: (result: AgentGitWorkspaceApi) => void;
    vi.mocked(getAgentJobGit).mockImplementation(
      () => new Promise<AgentGitWorkspaceApi>((done) => (resolve = done)),
    );

    const first = loadWorkspaceGit(target('job-one'));
    const sibling = loadWorkspaceGit(target('job-two'));
    expect(getAgentJobGit).toHaveBeenCalledTimes(1);
    resolve(view());

    await expect(first).resolves.toEqual(view());
    await expect(sibling).resolves.toEqual(view());
  });

  it('invalidates on revision change and evicts rejected reads', async () => {
    vi.mocked(getAgentJobGit)
      .mockResolvedValueOnce(view(revisionA))
      .mockRejectedValueOnce(new Error('temporary'))
      .mockResolvedValueOnce(view(revisionB));

    await loadWorkspaceGit(target('job-one', revisionA));
    await expect(loadWorkspaceGit(target('job-one', revisionB))).rejects.toThrow('temporary');
    await expect(loadWorkspaceGit(target('job-one', revisionB))).resolves.toEqual(view(revisionB));
    expect(getAgentJobGit).toHaveBeenCalledTimes(3);
  });

  it('refreshes metadata first and shares the refreshed full result', async () => {
    vi.mocked(getAgentJobGitMetadata).mockResolvedValue({
      available: true,
      revision: revisionB,
      branch: 'agent/work',
      changes: [{ code: ' M', path: 'src/app.ts' }],
      change_count: 1,
    });
    vi.mocked(getAgentJobGit).mockResolvedValue(view(revisionB));

    const refreshed = await loadWorkspaceGit(target('job-one', revisionA), { refresh: true });
    const sibling = await loadWorkspaceGit(target('job-two', revisionB));

    expect(refreshed).toEqual(view(revisionB));
    expect(sibling).toBe(refreshed);
    expect(getAgentJobGitMetadata).toHaveBeenCalledWith('job-one');
    expect(getAgentJobGit).toHaveBeenCalledTimes(1);
  });

  it('bounds retained full views with least-recently-used eviction', async () => {
    vi.mocked(getAgentJobGit).mockImplementation(async (id) => {
      const number = Number(id.slice('job-'.length));
      return view(String(number).repeat(64));
    });

    for (let index = 0; index <= 8; index += 1) {
      await loadWorkspaceGit(target(`job-${index}`, String(index).repeat(64)));
    }
    expect(getAgentJobGit).toHaveBeenCalledTimes(9);

    await loadWorkspaceGit(target('job-0', '0'.repeat(64)));
    expect(getAgentJobGit).toHaveBeenCalledTimes(10);
  });

  it('does not ask for a full view when refreshed metadata says unavailable', async () => {
    vi.mocked(getAgentJobGitMetadata).mockResolvedValue({
      available: false,
      branch: '',
      changes: [],
      change_count: 0,
    });

    await expect(loadWorkspaceGit(target('job-one'), { refresh: true })).resolves.toMatchObject({
      available: false,
      patch: '',
      commits: [],
    });
    expect(getAgentJobGit).not.toHaveBeenCalled();
  });
});
