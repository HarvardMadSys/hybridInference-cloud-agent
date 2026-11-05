import type { AgentGitWorkspaceMetadataApi } from '@/lib/api/agents';

import type { AgentDiffFile, AgentJob, AgentWorkspaceGitChange } from './types';

export interface WorkspaceGitOptions {
  patch?: string | null;
  gitMetadata?: AgentGitWorkspaceMetadataApi | null;
  gitMetadataPending?: boolean;
}

type WorkspaceGitProjection = Pick<AgentJob, 'diffFiles'> &
  Partial<
    Pick<
      AgentJob,
      | 'workspaceGitChanges'
      | 'workspaceGitRevision'
      | 'workspaceGitMetadataPending'
      | 'workspaceGitAvailable'
    >
  >;

// The adapter projects the job again for every streamed event, and the
// retained workspace panels are drawn again only when a field they read is a
// new object, so one status read keeps one list of paths. The lists are never
// mutated, so sharing them is safe.
const changedPathLists = new WeakMap<AgentWorkspaceGitChange[], string[]>();
const NO_CHANGED_PATHS: string[] = [];

function changedPaths(changes: AgentWorkspaceGitChange[] | undefined): string[] {
  if (!changes) return NO_CHANGED_PATHS;
  let paths = changedPathLists.get(changes);
  if (paths === undefined) {
    paths = [...new Set(changes.map((change) => change.path))];
    changedPathLists.set(changes, paths);
  }
  return paths;
}

/** Map the lightweight status view without parsing or retaining a full patch. */
export function workspaceGitProjection(
  parsedFiles: string[],
  options: WorkspaceGitOptions,
): WorkspaceGitProjection {
  const metadata = options.gitMetadata;
  return {
    diffFiles: typeof options.patch === 'string' ? parsedFiles : changedPaths(metadata?.changes),
    ...(metadata ? { workspaceGitChanges: metadata.changes } : {}),
    ...(metadata?.revision ? { workspaceGitRevision: metadata.revision } : {}),
    ...(options.gitMetadataPending ? { workspaceGitMetadataPending: true } : {}),
    ...(metadata ? { workspaceGitAvailable: metadata.available } : {}),
  };
}

function deletedByDiff(file: AgentDiffFile): boolean {
  let deleted = false;
  for (const line of file.lines) {
    if (line.text.startsWith('deleted file mode ')) deleted = true;
    else if (line.text.startsWith('new file mode ')) deleted = false;
  }
  return deleted;
}

/** Paths a live-workspace read cannot return because the status says they were deleted. */
export function deletedWorkspacePaths(
  details: AgentDiffFile[] | undefined,
  changes: AgentWorkspaceGitChange[] | undefined,
): Set<string> {
  return new Set([
    ...(details ?? []).filter(deletedByDiff).map((entry) => entry.path),
    ...(changes ?? []).filter((change) => change.code.includes('D')).map((change) => change.path),
  ]);
}
