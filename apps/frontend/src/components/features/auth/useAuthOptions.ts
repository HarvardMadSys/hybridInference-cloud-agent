'use client';
import { useEffect, useState } from 'react';
import { getAuthOptions, type AuthOptions } from '@/lib/api/accounts';

/** What this deployment's account pages offer; null until read, or when it cannot be. */
export function useAuthOptions(): AuthOptions | null {
  const [options, setOptions] = useState<AuthOptions | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    // A read that fails, or throws, leaves the pages on their defaults.
    void Promise.resolve()
      .then(() => getAuthOptions(controller.signal))
      .then((value) => {
        if (!controller.signal.aborted) setOptions(value);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);
  return options;
}
