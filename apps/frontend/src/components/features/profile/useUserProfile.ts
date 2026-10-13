'use client';

import { useEffect, useState } from 'react';
import { getUserProfile, type UserProfile } from '@/lib/api/userProfile';

export function useUserProfile(userId?: string) {
  const [snapshot, setSnapshot] = useState<{
    userId: string;
    profile: UserProfile | null;
  } | null>(null);

  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    setSnapshot(null);
    void getUserProfile(controller.signal)
      .then((profile) => {
        if (!controller.signal.aborted) setSnapshot({ userId, profile });
      })
      .catch(() => {
        if (!controller.signal.aborted) setSnapshot({ userId, profile: null });
      });
    return () => controller.abort();
  }, [userId]);

  const current = snapshot?.userId === userId ? snapshot : null;
  return { profile: current?.profile ?? null, loading: !!userId && !current };
}
