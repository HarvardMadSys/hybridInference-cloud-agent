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
// A turn on an owner's connection runs at the provider's default: nothing says
// which of a provider's models take the parameter, so no control is offered.

/** What this platform offers, wherever it can be delivered. */
export const PLATFORM_EFFORTS = ['low', 'medium', 'high', 'max'];

/** The levels to show for a pair, weakest first, or `[]` for no control. */
export function offeredEfforts(
  _config: AgentConfigApi | null,
  _runtime: string,
  _model: string,
  connectionId: string | null = null,
): string[] {
  void connectionId;
  return [];
}

/** The deepest level on offer, or `''` when the pair offers no control. */
export function highestEffort(offered: string[]): string {
  return offered.length > 0 ? offered[offered.length - 1] : '';
}
