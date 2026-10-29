// How one request's composition reads. Three sources stay apart:
//
// - the provider's reported input (exact, with cache as a billing split);
// - the CLI's own account, taken when the turn settled (counted by the CLI
//   or estimated from the text it listed, as `token_basis` says);
// - stored content reconstructed in transcript order (always estimated).
//
// A category the CLI does not expose has no size, never 0, and nothing here
// fills a difference between two totals with a category.

import type {
  CompositionEntryApi,
  NativeSnapshotApi,
  NativeSnapshotCategoryApi,
  RequestCompositionApi,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';

/** Display order of the categories, from what the CLI adds to the conversation. */
export const CATEGORY_KEYS = [
  'system_prompt',
  'tool_definitions',
  'mcp_tools',
  'instructions',
  'skills',
  'agents',
  'injected',
  'compaction_summary',
  'turn_prompts',
  'platform_context',
  'user_messages',
  'assistant',
  'reasoning',
  'tool_calls',
  'tool_results',
  'messages',
  'other',
] as const;

export const CATEGORY_COLOR: Record<string, string> = {
  system_prompt: 'bg-pink-500/70',
  tool_definitions: 'bg-indigo-500/70',
  mcp_tools: 'bg-fuchsia-500/70',
  instructions: 'bg-teal-500/70',
  skills: 'bg-cyan-500/70',
  agents: 'bg-lime-600/70',
  injected: 'bg-yellow-500/70',
  compaction_summary: 'bg-rose-400/70',
  turn_prompts: 'bg-sky-700/70',
  platform_context: 'bg-yellow-700/60',
  user_messages: 'bg-sky-500/70',
  assistant: 'bg-emerald-500/70',
  reasoning: 'bg-violet-500/70',
  tool_calls: 'bg-amber-500/70',
  tool_results: 'bg-orange-600/70',
  messages: 'bg-blue-500/70',
  other: 'bg-muted/60',
};

export function categoryLabel(key: string): TranslationKey {
  const known = (CATEGORY_KEYS as readonly string[]).includes(key) ? key : 'other';
  return `contextRequest.category.${known}` as TranslationKey;
}

export interface BarSegment {
  key: string;
  tokens: number;
  share: number;
}

/** Bar segments of the categories that have a size; shares of their sum. */
export function barSegments(
  categories: Array<{ key: string; tokens: number | null }>,
): BarSegment[] {
  const sized = categories.filter(
    (category): category is { key: string; tokens: number } =>
      typeof category.tokens === 'number' && category.tokens > 0,
  );
  const total = sized.reduce((sum, category) => sum + category.tokens, 0);
  return sized.map((category) => ({
    key: category.key,
    tokens: category.tokens,
    share: total > 0 ? category.tokens / total : 0,
  }));
}

/** The CLI categories with a size, and those it does not expose. */
export function nativeCategories(snapshot: NativeSnapshotApi): {
  sized: NativeSnapshotCategoryApi[];
  hidden: string[];
} {
  const categories = snapshot.categories ?? [];
  return {
    sized: categories.filter((category) => category.exposed !== false),
    hidden: categories.filter((category) => category.exposed === false).map((c) => c.key),
  };
}

export type NativeState =
  | { kind: 'snapshot'; snapshot: NativeSnapshotApi }
  | { kind: 'not_collected' }
  | { kind: 'not_offered' }
  | { kind: 'not_settled' }
  | { kind: 'pending' }
  | { kind: 'failed'; status: string };

/** What the composition can say about the CLI's own account. */
export function nativeState(composition: RequestCompositionApi): NativeState {
  const snapshot = composition.native;
  if (snapshot && snapshot.status === 'ok') return { kind: 'snapshot', snapshot };
  if (composition.native_status === 'not_collected') return { kind: 'not_collected' };
  if (composition.native_status === 'not_offered') return { kind: 'not_offered' };
  if (composition.native_status === 'not_settled') return { kind: 'not_settled' };
  if (composition.native_status === 'pending') return { kind: 'pending' };
  return { kind: 'failed', status: snapshot?.status ?? composition.native_status };
}

/** Entries of one category (or all) whose label or preview holds `query`. */
export function filterEntries(
  entries: CompositionEntryApi[],
  category: string | null,
  query = '',
  order: EntryOrder = 'largest',
): CompositionEntryApi[] {
  const needle = query.trim().toLowerCase();
  const kept = entries.filter(
    (entry) =>
      (!category || entry.category === category) &&
      (!needle ||
        entry.label.toLowerCase().includes(needle) ||
        entry.preview.toLowerCase().includes(needle)),
  );
  if (order === 'largest') return kept;
  // Transcript order: the turn, then the stored event; a turn's prompt leads it.
  return [...kept].sort((a, b) => a.turn_no - b.turn_no || (a.event_id ?? -1) - (b.event_id ?? -1));
}

/** How the browser lists entries: largest first (as sent) or as the transcript ran. */
export type EntryOrder = 'largest' | 'transcript';

/** Excluded-content reasons with something in them, in a fixed order. */
export const EXCLUDED_REASONS = [
  'compacted',
  'superseded_attempts',
  'subagent',
  'this_request_output',
] as const;
