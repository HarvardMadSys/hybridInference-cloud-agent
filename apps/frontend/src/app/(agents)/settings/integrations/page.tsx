'use client';

import { Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { IntegrationsView } from '@/components/features/agents/IntegrationsView';

function IntegrationsPageContent() {
  const params = useSearchParams();
  const connected = params?.get('connected');
  const connectedProvider = connected === 'github' || connected === 'gitlab' ? connected : null;
  return <IntegrationsView connectedProvider={connectedProvider} />;
}

export default function IntegrationsPage() {
  return (
    <Suspense>
      <IntegrationsPageContent />
    </Suspense>
  );
}
