'use client';
import { useEffect, useState } from 'react';
import { getAuthConfig, type AuthConfig } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';

export function useAuthConfig(initial: AuthConfig | null = null) {
  const [config, setConfig] = useState<AuthConfig | null>(initial);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setError(null);
    void getAuthConfig(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setConfig(value);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(getErrorMessage(cause));
      });
    return () => controller.abort();
  }, [attempt]);
  return { config, error, retry: () => setAttempt((value) => value + 1) };
}
