'use client';

import { useCallback, useState } from 'react';
import { useT } from '@/components/providers';
import { isGoalRevisionConflict } from './goalApi';

/**
 * One goal write at a time, with its failure kept where the owner can read it.
 *
 * Every write re-reads the conversation afterwards rather than believing its
 * own answer: the goal belongs to the thread, the thread is what the band and
 * the page draw, and a second reader of the same endpoint would drift from the
 * first. A compare-and-set refusal is the one failure that must also re-read
 * *before* the owner tries again — the revision this screen holds is exactly
 * the stale thing the server just refused, so leaving it in place would refuse
 * the retry too.
 *
 * `run` answers whether the write landed. Pause, resume and clear can act and
 * forget; the inline editor cannot, because the words it was given are the
 * retry and they have to stay on screen until the server has them.
 */
export function useGoalWrite(onChanged: () => void) {
  const t = useT();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (action: () => Promise<unknown>): Promise<boolean> => {
      setBusy(true);
      setError(null);
      try {
        await action();
        onChanged();
        return true;
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : t('goal.actionFailed'));
        if (isGoalRevisionConflict(cause)) onChanged();
        // A caller holding a draft — the band's inline editor, above all —
        // needs to know the write did not land, because the draft is the retry.
        return false;
      } finally {
        setBusy(false);
      }
    },
    [onChanged, t],
  );

  const clearError = useCallback(() => setError(null), []);

  return { busy, error, run, clearError };
}
