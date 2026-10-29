// How the Context view and Context insights read their numbers. Kept apart from
// the components so the semantics are tested directly:
//
// - a size is one request's input tokens as an agent CLI reported it;
// - a share of the window exists only when the CLI reported the window;
// - a compaction is one the CLI reported, never inferred from a drop;
// - the composition is an estimate of stored text, labelled as one.

import type {
  ContextCategory,
  ContextCompositionApi,
  ContextPointApi,
  ContextTurnApi,
} from '@/lib/api/agents';

/** Where a conversation's Context view opens: its latest turn, on the Context tab. */
export function contextHref(jobId: string): string {
  return `/${encodeURIComponent(jobId)}?view=context`;
}

/** Whether a page was opened on its Context view. */
export function opensContext(search: string): boolean {
  return new URLSearchParams(search).get('view') === 'context';
}

function fraction(value: number | null, scale: number): number | null {
  if (value === null || !(scale > 0) || !Number.isFinite(value)) return null;
  return Math.min(1, Math.max(0, value / scale));
}

/**
 * A conversation's bar in the insights list: its largest and latest request,
 * against the window when one was reported and otherwise against the widest
 * conversation shown, so a missing window is never drawn as a full one.
 */
export function contextBar(
  peak: number | null,
  latest: number | null,
  window: number | null,
  widest: number,
): { peak: number; latest: number | null; againstWindow: boolean } {
  const againstWindow = window !== null && window > 0;
  const scale = againstWindow ? Math.max(window, peak ?? 0) : widest;
  return { peak: fraction(peak, scale) ?? 0, latest: fraction(latest, scale), againstWindow };
}

/** `+12.3k`, `−4.1k`, `0`: a signed change in compact tokens. */
export function signedTokens(delta: number | null, compact: (value: number) => string): string {
  if (delta === null || !Number.isFinite(delta)) return '—';
  if (delta === 0) return '0';
  return `${delta > 0 ? '+' : '−'}${compact(Math.abs(delta))}`;
}

export interface ChartScale {
  /** The value the top of the chart stands for. */
  top: number;
  /** Where the reported window sits, as a share of the chart's height; null without one. */
  window: number | null;
}

/**
 * The chart's top: the largest request, or the reported window when every
 * request ran under the same one and it is not far above them (a 1M window
 * would flatten 20k requests to nothing).
 */
export function chartScale(
  points: Pick<ContextPointApi, 'prompt_tokens' | 'context_window'>[],
): ChartScale {
  const peak = Math.max(0, ...points.map((point) => point.prompt_tokens));
  const windows = new Set(points.map((point) => point.context_window));
  const [only] = windows;
  if (windows.size === 1 && typeof only === 'number' && only > 0 && only <= peak * 4) {
    const top = Math.max(only, peak);
    return { top, window: only / top };
  }
  return { top: peak, window: null };
}

/** Each turn's last request, for the by-turn chart; turns without one are left out. */
export function turnPoints(
  turns: ContextTurnApi[],
  points: ContextPointApi[],
): Array<{ turn: ContextTurnApi; point: ContextPointApi }> {
  const lastOfTurn = new Map<string, ContextPointApi>();
  for (const point of points) lastOfTurn.set(point.job_id, point);
  return turns.flatMap((turn) => {
    const point = lastOfTurn.get(turn.job_id);
    return point ? [{ turn, point }] : [];
  });
}

export const CATEGORY_ORDER: ContextCategory[] = [
  'user',
  'assistant',
  'reasoning',
  'tool_calls',
  'tool_results',
];

export interface CompositionSegment {
  key: ContextCategory | 'unattributed';
  tokens: number;
  share: number;
}

/**
 * The composition as bar segments: each estimated category and, when the
 * server could attribute it, the part of the latest request the stored text
 * does not explain. Shares are of their sum, which is the latest request when
 * that part is shown and the estimate otherwise.
 */
export function compositionSegments(composition: ContextCompositionApi): CompositionSegment[] {
  const parts: Array<{ key: CompositionSegment['key']; tokens: number }> = CATEGORY_ORDER.map(
    (key) => ({ key, tokens: composition.categories[key] ?? 0 }),
  );
  if (composition.unattributed_tokens !== null && composition.unattributed_tokens > 0) {
    parts.push({ key: 'unattributed', tokens: composition.unattributed_tokens });
  }
  const total = parts.reduce((sum, part) => sum + part.tokens, 0);
  return parts
    .filter((part) => part.tokens > 0)
    .map((part) => ({ ...part, share: total > 0 ? part.tokens / total : 0 }));
}
