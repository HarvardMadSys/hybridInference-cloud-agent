// One palette for every Context surface: the Token Stats ring, the trend's
// areas, the window card's bars and the browser's accordion. Hues follow
// bowenliang123/dsh-context `src/client/categories.ts` (`CATS`, Apache-2.0,
// commit 32982aa6aa2c70fbda6b81281de98a72ea625001); keys are ours.

import type { TranslationKey } from '@/lib/i18n/translate';

export interface Swatch {
  bg: string;
  fill: string;
  stroke: string;
}

const swatch = (bg: string, fill: string, stroke: string): Swatch => ({ bg, fill, stroke });

/** Content categories, by the keys the server uses. */
export const PALETTE: Record<string, Swatch> = {
  system_prompt: swatch('bg-indigo-500', 'fill-indigo-500', 'stroke-indigo-500'),
  tool_schemas: swatch('bg-amber-500', 'fill-amber-500', 'stroke-amber-500'),
  user: swatch('bg-green-500', 'fill-green-500', 'stroke-green-500'),
  inject: swatch('bg-purple-500', 'fill-purple-500', 'stroke-purple-500'),
  skill: swatch('bg-orange-500', 'fill-orange-500', 'stroke-orange-500'),
  summary: swatch('bg-rose-400', 'fill-rose-400', 'stroke-rose-400'),
  assistant: swatch('bg-blue-500', 'fill-blue-500', 'stroke-blue-500'),
  reasoning: swatch('bg-violet-500', 'fill-violet-500', 'stroke-violet-500'),
  tool_calls: swatch('bg-sky-500', 'fill-sky-500', 'stroke-sky-500'),
  tool_results: swatch('bg-teal-500', 'fill-teal-500', 'stroke-teal-500'),
  other: swatch('bg-slate-400 dark:bg-slate-500', 'fill-slate-400', 'stroke-slate-400'),
  /** A DNA column's earliest items, folded into one band by the server. */
  folded: swatch('bg-slate-300 dark:bg-slate-600', 'fill-slate-300', 'stroke-slate-300'),
};

/** The reconstruction's (and Insights') category names, as the dashboard keys them. */
export const RECONSTRUCTED_KEY: Record<string, string> = {
  user_messages: 'user',
  turn_prompts: 'user',
  injected: 'inject',
  platform_context: 'inject',
  skills: 'skill',
  compaction_summary: 'summary',
  assistant: 'assistant',
  reasoning: 'reasoning',
  tool_calls: 'tool_calls',
  tool_results: 'tool_results',
  system_prompt: 'system_prompt',
  tool_definitions: 'tool_schemas',
};

export const CATEGORY_LABEL: Record<string, TranslationKey> = {
  system_prompt: 'dashboard.cat.system_prompt',
  tool_schemas: 'dashboard.cat.tool_schemas',
  user: 'dashboard.cat.user',
  inject: 'dashboard.cat.inject',
  skill: 'dashboard.cat.skill',
  summary: 'dashboard.cat.summary',
  assistant: 'dashboard.cat.assistant',
  reasoning: 'dashboard.cat.reasoning',
  tool_calls: 'dashboard.cat.tool_calls',
  tool_results: 'dashboard.cat.tool_results',
  other: 'dashboard.cat.other',
  folded: 'dashboard.cat.folded',
};

export function swatchOf(key: string): Swatch {
  return PALETTE[RECONSTRUCTED_KEY[key] ?? key] ?? PALETTE.other;
}

export function labelOf(key: string): TranslationKey {
  return CATEGORY_LABEL[RECONSTRUCTED_KEY[key] ?? key] ?? CATEGORY_LABEL.other;
}

/** Timing kinds; hues from dsh-context `timingStrip.tsx` `TIMING_COLOR`. */
export const TIMING_SWATCH: Record<string, Swatch> = {
  ttft: swatch('bg-blue-500', 'fill-blue-500', 'stroke-blue-500'),
  reasoning: swatch('bg-violet-500', 'fill-violet-500', 'stroke-violet-500'),
  text: swatch('bg-pink-500', 'fill-pink-500', 'stroke-pink-500'),
  toolarg: swatch('bg-amber-500', 'fill-amber-500', 'stroke-amber-500'),
  gen: swatch('bg-fuchsia-400', 'fill-fuchsia-400', 'stroke-fuchsia-400'),
  tools: swatch('bg-teal-500', 'fill-teal-500', 'stroke-teal-500'),
  unsplit: swatch('bg-slate-500 dark:bg-slate-400', 'fill-slate-500', 'stroke-slate-500'),
  other: swatch('bg-slate-300 dark:bg-slate-600', 'fill-slate-300', 'stroke-slate-300'),
};
