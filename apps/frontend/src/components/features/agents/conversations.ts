import type { AgentJob } from './types';

export interface ConversationRow {
  key: string;
  job: AgentJob;
  jobIds: string[];
}

/** The repo name a folder row shows; the owner is a tooltip, not a label. */
export function projectLabel(repo: string): string {
  const name = repo.slice(repo.lastIndexOf('/') + 1);
  return name || repo;
}
