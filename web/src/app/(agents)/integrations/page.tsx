'use client';

import { Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { IntegrationsView } from '@/components/features/agents/IntegrationsView';
import type { McpAuthorizationResult } from '@/components/features/agents/mcp/McpConnectionsSection';
import type { AgentIntegrationProvider } from '@/lib/api/agents';

function IntegrationsPageContent() {
  const params = useSearchParams();
  const connected = params?.get('connected');
  const connectedProvider: AgentIntegrationProvider | null =
    connected === 'github' || connected === 'gitlab' ? connected : null;
  const authorized = params?.get('mcp_authorized');
  let mcpAuthorization: McpAuthorizationResult | undefined;
  if (authorized === '1' || authorized === '0') {
    mcpAuthorization = {
      ok: authorized === '1',
      connectionId: params?.get('mcp_connection') ?? null,
      message: params?.get('mcp_message') ?? null,
    };
  }

  return (
    <IntegrationsView connectedProvider={connectedProvider} mcpAuthorization={mcpAuthorization} />
  );
}

export default function IntegrationsPage() {
  return (
    <Suspense fallback={<div className="h-full bg-white dark:bg-gray-900" />}>
      <IntegrationsPageContent />
    </Suspense>
  );
}
