'use client';

import { Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { PluginsSettingsView } from '@/components/features/settings/PluginsSettingsView';
import type { McpAuthorizationResult } from '@/components/features/agents/mcp/McpConnectionsSection';

function PluginsPageContent() {
  const params = useSearchParams();
  const authorized = params?.get('mcp_authorized');
  let authorizationResult: McpAuthorizationResult | undefined;
  if (authorized === '1' || authorized === '0') {
    authorizationResult = {
      ok: authorized === '1',
      connectionId: params?.get('mcp_connection') ?? null,
      message: params?.get('mcp_message') ?? null,
    };
  }
  return <PluginsSettingsView authorizationResult={authorizationResult} />;
}

export default function PluginsPage() {
  return (
    <Suspense>
      <PluginsPageContent />
    </Suspense>
  );
}
