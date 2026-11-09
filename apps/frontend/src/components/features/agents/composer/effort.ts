import { useEffect, useMemo, useState } from 'react';
import type { AgentConfigApi, ModelEffortOfferApi } from '@/lib/api/agents';

// Which reasoning levels a turn may run at.
//
// **What the model's provider declares, narrowed to what the agent can send.**
// The server derives it from the provider's own model listing, per model, per
// agent, for the API that agent's turn finally calls, and answers it in
// `/config` (`model_connections[].model_efforts`). Nothing here guesses a
// level from a model's name or offers a fixed list. Auto -- no level, the
// provider's default -- is always offered and is the default; a provider that
// declares nothing offers Auto alone, and the composer says why.

/** The picker's value for Auto. The composer state holds `''` for it. */
export const AUTO_EFFORT = 'auto';

/** What the server offers one agent for one connection model; `null` when it cannot run it. */
export function effortOffer(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
  connectionId: string | null = null,
): ModelEffortOfferApi | null {
  if (!config || !model || !connectionId) return null;
  const connection = config.model_connections?.find((option) => option.id === connectionId);
  if (!connection?.model_runtimes?.[model]?.includes(runtime)) return null;
  return (
    connection.model_efforts?.[model]?.[runtime] ?? {
      // A control plane that predates the field: nothing is known.
      status: 'unknown',
      levels: [],
      reason: 'not_read',
    }
  );
}

/** The levels to offer besides Auto, weakest first; `[]` offers Auto alone. */
export function offeredEfforts(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
  connectionId: string | null = null,
): string[] {
  const offer = effortOffer(config, runtime, model, connectionId);
  return offer?.status === 'supported' ? offer.levels : [];
}

/** Whether `effort` (`''` is Auto) can run on `offer`. */
export function effortFits(offer: ModelEffortOfferApi | null, effort: string): boolean {
  return !effort || Boolean(offer && offer.status === 'supported' && offer.levels.includes(effort));
}

/**
 * The level a composer holds for a new task or Side Chat, kept honest as its
 * model changes: a level the newly chosen pair does not offer is set back to
 * Auto *visibly* -- `dropped` names it so the composer can say so -- never to
 * another level. Waits for the config and a chosen model before judging, since
 * before then nothing is known rather than nothing offered.
 */
export function useOfferedEffort(
  config: AgentConfigApi | null,
  runtime: string,
  model: string,
  connectionId: string | null,
  effort: string,
  setEffort: (effort: string) => void,
) {
  const offer = useMemo(
    () => effortOffer(config, runtime, model, connectionId),
    [config, runtime, model, connectionId],
  );
  const [dropped, setDropped] = useState('');
  useEffect(() => {
    if (!config || !model || effortFits(offer, effort)) return;
    setDropped(effort);
    setEffort('');
  }, [config, model, offer, effort, setEffort]);
  return {
    offer,
    dropped,
    choose(next: string) {
      setDropped('');
      setEffort(next);
    },
  };
}

/**
 * What a follow-up sends for its level against the turn it inherits from:
 * nothing when it is the same, `"auto"` to clear an inherited level, else the
 * level.
 */
export function effortOverride(inherited: string | undefined, effort: string) {
  if (effort === (inherited ?? '')) return {};
  return { reasoning_effort: effort || AUTO_EFFORT };
}
