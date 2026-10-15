'use client';

import { useEffect } from 'react';
import { onProfileSaved } from '@/lib/api/profileEvents';
import { getUserProfile, type UserProfile } from '@/lib/api/userProfile';
import { createAccountResource } from './accountResource';

const profiles = createAccountResource(getUserProfile);

export function cacheUserProfile(userId: string, profile: UserProfile) {
  profiles.write(userId, profile);
}

export function useUserProfile(userId?: string) {
  const { data: profile, loading } = profiles.useResource(userId);

  useEffect(() => {
    if (!userId) return;
    // Another view saving the profile in this tab: take its answer rather
    // than showing the old name until the next page load.
    return onProfileSaved((profile) => cacheUserProfile(userId, profile));
  }, [userId]);

  return { profile, loading };
}
