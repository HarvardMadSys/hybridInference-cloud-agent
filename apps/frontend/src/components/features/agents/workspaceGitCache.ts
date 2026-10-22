import {
  getAgentJobGit,
  getAgentJobGitMetadata,
  type AgentGitWorkspaceApi,
  type AgentGitWorkspaceMetadataApi,
} from '@/lib/api/agents';

import type { AgentJob } from './types';

/** At the host's 2 MiB response cap, eight retained full views cost at most 16 MiB. */
export const MAX_WORKSPACE_GIT_CACHE_ENTRIES = 8;

const fullViews = new Map<string, Promise<AgentGitWorkspaceApi>>();

type GitTarget = Pick<AgentJob, 'id' | 'sandboxId' | 'threadId' | 'workspaceGitRevision'>;

function workspaceScope(job: GitTarget): string {
  return job.sandboxId ?? job.threadId ?? job.id;
}

function cacheKey(job: GitTarget, revision = job.workspaceGitRevision): string | null {
  return revision ? `${workspaceScope(job)}\0${revision}` : null;
}

function touch(key: string, value: Promise<AgentGitWorkspaceApi>): void {
  fullViews.delete(key);
  fullViews.set(key, value);
  while (fullViews.size > MAX_WORKSPACE_GIT_CACHE_ENTRIES) {
    const oldest = fullViews.keys().next().value as string | undefined;
    if (oldest === undefined) return;
    fullViews.delete(oldest);
  }
}

function unavailable(metadata: AgentGitWorkspaceMetadataApi): AgentGitWorkspaceApi {
  return { ...metadata, patch: '', commits: [] };
}

async function targetForRefresh(job: GitTarget): Promise<{
  target: GitTarget;
  unavailable?: AgentGitWorkspaceApi;
}> {
  const metadata = await getAgentJobGitMetadata(job.id);
  if (!metadata.available) return { target: job, unavailable: unavailable(metadata) };
  return {
    target: { ...job, workspaceGitRevision: metadata.revision ?? undefined },
  };
}

/**
 * Load one full view per shared workspace revision.
 *
 * The promise itself is cached, so two Fork siblings opening Changes together
 * share the request as well as the fulfilled value. Rejections are evicted.
 */
export async function loadWorkspaceGit(
  job: GitTarget,
  { refresh = false }: { refresh?: boolean } = {},
): Promise<AgentGitWorkspaceApi> {
  const refreshed = refresh ? await targetForRefresh(job) : { target: job };
  if (refreshed.unavailable) return refreshed.unavailable;
  const target = refreshed.target;
  const key = cacheKey(target);
  if (!key) return getAgentJobGit(target.id);
  if (!refresh) {
    const cached = fullViews.get(key);
    if (cached) {
      touch(key, cached);
      return cached;
    }
  }

  const pending = getAgentJobGit(target.id)
    .then((result) => {
      const actualKey = cacheKey(target, result.revision ?? undefined);
      if (actualKey && actualKey !== key && fullViews.get(key) === pending) {
        fullViews.delete(key);
        touch(actualKey, Promise.resolve(result));
      }
      return result;
    })
    .catch((error: unknown) => {
      if (fullViews.get(key) === pending) fullViews.delete(key);
      throw error;
    });
  touch(key, pending);
  return pending;
}

/** Test and sign-out boundary; production invalidation is revision-keyed. */
export function clearWorkspaceGitCache(): void {
  fullViews.clear();
}
