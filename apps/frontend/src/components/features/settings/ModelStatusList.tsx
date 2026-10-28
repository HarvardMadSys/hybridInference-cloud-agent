'use client';

import { useT } from '@/components/providers';
import { connectionAgentNames } from './modelConnectionDisplay';
import type { ModelStatus } from '@/lib/api/modelConnections';

/**
 * Each saved model and the agents it works with. Every saved model was
 * verified before it was saved; which APIs the provider offers, and how each
 * agent reaches it, are the platform's concern and are not shown here.
 */
export function ModelStatusList({ statuses }: { statuses: ModelStatus[] }) {
  const t = useT();
  return (
    <ul className="mt-3 divide-y divide-border">
      {statuses.map((status) => (
        <li key={status.model} className="py-2">
          <p className="break-all font-mono text-[13px]">{status.model}</p>
          <p className={`text-xs ${status.runtimes.length ? 'text-muted' : 'text-status-warning'}`}>
            {status.runtimes.length
              ? t('connections.agents', { agents: connectionAgentNames(status.runtimes) })
              : t('connections.notUsable')}
          </p>
        </li>
      ))}
    </ul>
  );
}
