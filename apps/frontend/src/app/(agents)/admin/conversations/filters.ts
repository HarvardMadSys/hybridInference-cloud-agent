import type { AdminRunFilters } from '@/lib/api/adminRuns';

const KEYS = ['user_id', 'model', 'runtime', 'repo', 'state', 'host'] as const;

/** Reject broken links instead of silently widening the selection. */
export function conversationLink(
  query: string,
  requireWindow = true,
): {
  filters: AdminRunFilters | null;
  error: string | null;
  linked: boolean;
} {
  const params = new URLSearchParams(query);
  for (const key of params.keys()) {
    if (key !== 'since' && key !== 'until' && !(KEYS as readonly string[]).includes(key)) {
      return { filters: null, error: `Unsupported filter: ${key}`, linked: true };
    }
  }
  if (!params.size) return { filters: null, error: null, linked: false };
  const since = params.get('since');
  const until = params.get('until');
  if ((requireWindow || since || until) && (!since || !until)) {
    return { filters: null, error: 'This link is missing one end of its window.', linked: true };
  }
  if (since && until && !(Date.parse(since) < Date.parse(until))) {
    return { filters: null, error: 'This link has an empty or inverted window.', linked: true };
  }
  const filters: AdminRunFilters = {};
  if (since && until) Object.assign(filters, { since, until });
  for (const key of KEYS) {
    const value = params.get(key);
    if (value) Object.assign(filters, { [key]: value });
  }
  return { filters, error: null, linked: true };
}
