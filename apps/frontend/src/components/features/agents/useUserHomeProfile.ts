'use client';

import { useEffect, useState } from 'react';
import { getInsights, type WorkInsights } from '@/lib/api/insights';

export function useUserHomeProfile(userId?: string) {
  const [snapshot, setSnapshot] = useState<{
    userId: string;
    view: WorkInsights | null;
    error: boolean;
  } | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    setSnapshot({ userId, view: null, error: false });
    void getInsights(controller.signal)
      .then((view) => {
        if (!controller.signal.aborted) setSnapshot({ userId, view, error: false });
      })
      .catch(() => {
        if (!controller.signal.aborted) setSnapshot({ userId, view: null, error: true });
      });
    return () => controller.abort();
  }, [userId, attempt]);

  // Hide another account's profile before effects run on an account change.
  const current = snapshot?.userId === userId ? snapshot : null;
  return {
    view: current?.view ?? null,
    loading: !!userId && !current?.view && !current?.error,
    error: current?.error ?? false,
    retry: () => setAttempt((value) => value + 1),
  };
}
