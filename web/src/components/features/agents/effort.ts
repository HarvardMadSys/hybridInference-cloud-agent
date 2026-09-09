import type { AgentConfigApi } from '@/lib/api/agents';

// Which reasoning efforts a turn may be run at.
//
// **A fixed platform list, not the model's own vocabulary.** Deriving the list
// per model was tried and withdrawn: the providers' documented domains
// disagreed with what their APIs actually accept (Z.AI documents low/high/max
// for glm-5.3 and accepts all seven), and a domain copied from a doc is a
// guess wearing a citation. Four levels, the same everywhere, is a claim we
// can keep.
//
// Two guards survive, because both are facts rather than preferences:
//
// - **the runtime must be able to say it.** Verified against each pinned CLI
//   (see `effort_wire_shapes.md`), and load-bearing: claude-code turns a level
//   it does not know into `high` without complaint, so offering one it lacks
//   would run a depth nobody chose.
// - **the model must take the parameter at all.** The gateway forwards
//   `reasoning_effort` only for models whose registry entry lists it, so
//   offering it elsewhere would be a control that changes nothing.

/** What this platform offers, wherever it can be delivered. */
export const PLATFORM_EFFORTS = ['low', 'medium', 'high', 'max'];

/**
 * The levels to show for a pair, weakest first, or `[]` for no control.
 *
 * The model's entry in `model_reasoning_efforts` is read as a yes/no — does
 * this model take the parameter — and not for its contents.
 */
export function offeredEfforts(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
): string[] {
  const runtimeLevels = config?.runtime_reasoning_efforts?.[runtime] ?? [];
  const modelTakesIt = (config?.model_reasoning_efforts?.[model] ?? []).length > 0;
  if (!modelTakesIt) return [];
  return PLATFORM_EFFORTS.filter((level) => runtimeLevels.includes(level));
}

/** The deepest level on offer, or `''` when the pair offers no control. */
export function highestEffort(offered: string[]): string {
  return offered.length > 0 ? offered[offered.length - 1] : '';
}
