import type { Translator } from '@/lib/i18n/translate';
import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import type { AgentJob } from './types';

export interface ConversationRow {
  key: string;
  job: AgentJob;
  jobIds: string[];
}

/** The repo name a folder row shows; the owner is a tooltip, not a label. */
export function projectLabel(repo: string, t: Translator): string {
  if (isTemporaryWorkspace(repo)) return t('temporaryWorkspace.name');
  const name = repo.slice(repo.lastIndexOf('/') + 1);
  return name || repo;
}

/** A project's whole name, for a tooltip or a search result: `owner/name`. */
export function projectTitle(repo: string, t: Translator): string {
  return isTemporaryWorkspace(repo) ? t('temporaryWorkspace.name') : repo;
}
