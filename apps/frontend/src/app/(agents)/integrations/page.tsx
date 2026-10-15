'use client';

import { Suspense, useEffect } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { legacySettingsDestination } from '@/components/features/settings/navigation';

function LegacySettingsRedirect() {
  const router = useRouter();
  const params = useSearchParams();
  const search = params?.toString() ?? '';
  useEffect(() => {
    router.replace(legacySettingsDestination(search, window.location.hash));
  }, [router, search]);
  return null;
}

export default function IntegrationsPage() {
  return (
    <Suspense>
      <LegacySettingsRedirect />
    </Suspense>
  );
}
