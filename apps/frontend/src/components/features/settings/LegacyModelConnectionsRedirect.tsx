'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import { MODEL_CONNECTIONS_ANCHOR, SETTINGS_PATHS } from './navigation';

/** An old My Settings link to its model connections opens the Models page instead. */
export function LegacyModelConnectionsRedirect() {
  const router = useRouter();
  useEffect(() => {
    const { hash, search } = window.location;
    if (hash === MODEL_CONNECTIONS_ANCHOR)
      router.replace(`${SETTINGS_PATHS.models}${search}${hash}`);
  }, [router]);
  return null;
}
