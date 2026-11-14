import { useEffect, useState } from 'react';
import type { AgentConfigApi } from '@/lib/api/agents';
import { nextAllowanceReset } from './providedModels';

/** The longest delay a browser timer takes; a longer one fires at once. */
const MAX_TIMER_MS = 2 ** 31 - 1;

/**
 * Draws its component again when a provided allowance's day starts over.
 *
 * What an owner has left is read from the clock as well as from `/config`
 * (`allowanceOf`): once `resets_at` has passed, a model used up yesterday can
 * run. Nothing else redraws a page left open across that moment, so without
 * this it would go on holding Run for a model the server now accepts.
 */
export function useAllowanceReset(config: AgentConfigApi | null): void {
  const [, redraw] = useState(0);
  const next = nextAllowanceReset(config);
  useEffect(() => {
    if (next === null) return;
    let timer: ReturnType<typeof setTimeout>;
    const wait = () => {
      const left = next - Date.now();
      if (left <= 0) redraw((count) => count + 1);
      else timer = setTimeout(wait, Math.min(left, MAX_TIMER_MS));
    };
    wait();
    return () => clearTimeout(timer);
  }, [next]);
}
