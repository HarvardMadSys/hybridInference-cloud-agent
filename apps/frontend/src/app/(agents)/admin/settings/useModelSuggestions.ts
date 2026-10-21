'use client';

import { useEffect, useState } from 'react';

import { getModelPreference } from '@/lib/api/modelPreference';

/**
 * The models the signed-in administrator's own account may call, as hints for
 * the deployment's default model.
 *
 * Only hints: the gateway's catalog is per user, so another user may be offered
 * a model this account is not, and the field stays free text. `null` means the
 * catalog is unknown — not loaded, unreadable, or this account cannot use
 * agents — and then nothing is suggested or flagged.
 */
export function useModelSuggestions(enabled: boolean): string[] | null {
  const [models, setModels] = useState<string[] | null>(null);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    getModelPreference(controller.signal)
      .then((preference) => {
        if (controller.signal.aborted) return;
        // An empty list is an unreadable catalog, and flagging every value
        // against it would be wrong for all of them.
        setModels(preference.models.length ? preference.models : null);
      })
      .catch(() => {
        // Suggestions are optional: the field works the same without them.
      });
    return () => controller.abort();
  }, [enabled]);

  return enabled ? models : null;
}
