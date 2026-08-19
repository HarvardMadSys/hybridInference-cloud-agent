import type { AgentConfigApi } from '@/lib/api/agents';

// Which reasoning efforts a (runtime, model) pair can honour, and how the
// composer spells "let them decide".
//
// The intersection is computed here rather than by either picker, so the two
// composers cannot disagree about what is on offer — and the backend validates
// the same rule again at create time, because a UI is not a permission.

/**
 * The option meaning "no instruction", and the value a follow-up sends to
 * clear an inherited effort — one string for one state, because on the wire
 * they are the same request.
 *
 * Defined here rather than beside the request types so this module imports
 * only a type from the API layer. A value import would make every component
 * that computes an offered set depend on that module at runtime, which is a
 * dependency a pure intersection has no use for.
 */
export const AUTO_EFFORT = 'auto';

/**
 * Levels both halves accept. Empty means no control belongs on screen: either
 * this runtime cannot express one, this model takes none, or the backend
 * predates the feature and reports neither.
 */
export function offeredEfforts(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
): string[] {
  const runtimeLevels = config?.runtime_reasoning_efforts?.[runtime] ?? [];
  const modelLevels = config?.model_reasoning_efforts?.[model] ?? [];
  return runtimeLevels.filter((level) => modelLevels.includes(level));
}

/**
 * The picker's options: Auto first, then the offered levels. Empty when
 * nothing is offered, which is what makes the control disappear rather than
 * render a menu with one useless entry.
 */
export function effortOptions(offered: string[]): string[] {
  return offered.length > 0 ? [AUTO_EFFORT, ...offered] : [];
}
