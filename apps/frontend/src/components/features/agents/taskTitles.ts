import type { AgentJobApi } from '@/lib/api/agents';

/** Mirror the backend's eligibility, including titles saved by the warmup bug. */
export function taskTitleNeedsGeneration(
  job: Pick<AgentJobApi, 'title' | 'title_source'>,
): boolean {
  if (job.title_source === 'fallback') return true;
  if (job.title_source !== 'generated' && job.title_source !== 'inherited') return false;
  return (job.title ?? '')
    .trim()
    .replace(/^["'“”‘’` ]+/, '')
    .startsWith('⏳ The model is starting up');
}
