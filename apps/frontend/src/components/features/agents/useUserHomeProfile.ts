'use client';

import { getInsights } from '@/lib/api/insights';
import { createAccountResource } from '@/components/features/profile/accountResource';

const insights = createAccountResource(getInsights);

export function useUserHomeProfile(userId?: string) {
  const { data: view, loading, error, retry } = insights.useResource(userId);
  return { view, loading, error, retry };
}
