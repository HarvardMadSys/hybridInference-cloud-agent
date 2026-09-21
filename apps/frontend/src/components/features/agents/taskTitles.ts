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

/** Keep a resolved name when an older snapshot of the same conversation arrives. */
export function preserveResolvedTaskTitle(
  current: AgentJobApi | null,
  incoming: AgentJobApi,
): AgentJobApi {
  if (
    current?.title &&
    current.title_source &&
    (current.id === incoming.id ||
      (current.thread_id && current.thread_id === incoming.thread_id)) &&
    !taskTitleNeedsGeneration(current) &&
    (taskTitleNeedsGeneration(incoming) ||
      (current.title_source === 'manual' && incoming.title_source !== 'manual'))
  ) {
    return { ...incoming, title: current.title, title_source: current.title_source };
  }
  return incoming;
}
