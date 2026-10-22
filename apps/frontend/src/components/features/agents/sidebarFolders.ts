import type { SidebarProject } from './useAgentJobs';
import type { SidebarConversation } from './useConversationList';

/** Keep persisted empty projects and the selected repository in the folder tree. */
export function sidebarFolders(
  projects: SidebarProject[],
  added: string[],
  rows: SidebarConversation[],
  activeRepo: string | null,
) {
  const repos = new Map(projects.map((project) => [project.repo, project]));
  for (const repo of added)
    if (!repos.has(repo))
      repos.set(repo, { repo, task_count: 0, active_count: 0, last_activity_at: null });
  for (const row of rows)
    if (!repos.has(row.job.repo))
      repos.set(row.job.repo, {
        repo: row.job.repo,
        task_count: 1,
        active_count: 0,
        last_activity_at: row.lastActivityAt,
      });
  if (activeRepo && !repos.has(activeRepo))
    repos.set(activeRepo, {
      repo: activeRepo,
      task_count: 0,
      active_count: 0,
      last_activity_at: null,
    });
  return [...repos.values()].sort(
    (a, b) =>
      (Date.parse(b.pinned_at ?? '') || 0) - (Date.parse(a.pinned_at ?? '') || 0) ||
      (Date.parse(b.last_activity_at ?? '') || 0) - (Date.parse(a.last_activity_at ?? '') || 0),
  );
}
