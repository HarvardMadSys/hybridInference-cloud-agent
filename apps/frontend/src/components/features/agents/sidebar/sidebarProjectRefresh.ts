import type { AgentProjectApi } from '@/lib/api/agents';

type Project = Omit<AgentProjectApi, 'repo'> & { repo: string };
export interface ProjectRefresh {
  refresh: number;
  activityMoved: number;
  refreshKey?: string;
  managedRefresh: boolean;
  retryKey?: number;
}
interface Held extends ProjectRefresh {
  token: string;
  membership: string | null;
  observedRefresh: number;
  observedReorder: number;
}

/** One grouped project snapshot decides which retained folder pages are stale. */
export function createProjectRefreshTracker() {
  const held = new Map<string, Held>();
  let snapshot: readonly Project[] | null = null;
  let projects = new Map<string, Project>();
  let refresh = 0;
  let reorder = 0;
  let retryKey = 0;
  function update(repo: string, token: string, membership: string | null) {
    const previous = held.get(repo);
    const next = previous ?? {
      token,
      membership,
      refresh,
      activityMoved: reorder,
      refreshKey: token,
      managedRefresh: true,
      observedRefresh: refresh,
      observedReorder: reorder,
    };
    if (next.token !== token) {
      // Owner pushes do not identify repo moves. The grouped membership
      // fingerprint must prove that a first-page activity merge is safe.
      const activityOnly =
        membership !== null &&
        next.membership === membership &&
        next.observedRefresh === refresh &&
        next.observedReorder !== reorder;
      if (activityOnly) next.activityMoved = reorder;
      else {
        next.refresh = refresh;
        next.refreshKey = token;
      }
      next.token = token;
    }
    next.membership = membership;
    next.observedRefresh = refresh;
    next.observedReorder = reorder;
    held.set(repo, next);
    return next;
  }
  return {
    observe(next: readonly Project[], full: number, activity: number) {
      refresh = full;
      reorder = activity;
      if (next === snapshot) return;
      snapshot = next;
      retryKey += 1;
      projects = new Map(next.map((project) => [project.repo, project]));
      for (const repo of new Set([...held.keys(), ...projects.keys()])) {
        const project = projects.get(repo);
        const token = project ? project.refresh_token : 'empty';
        if (token != null)
          update(repo, token, project ? (project.membership_token ?? null) : 'empty');
      }
    },
    forRepo(repo: string): ProjectRefresh {
      const project = projects.get(repo);
      // Old servers cannot prove that a folder is unchanged. Their full reads
      // stay correct and run through the same bounded-concurrency read queue.
      if (project && project.refresh_token == null)
        return { refresh, activityMoved: reorder, managedRefresh: false };
      return {
        ...(held.get(repo) ??
          update(
            repo,
            project?.refresh_token ?? 'empty',
            project ? (project.membership_token ?? null) : 'empty',
          )),
        retryKey,
      };
    },
  };
}
