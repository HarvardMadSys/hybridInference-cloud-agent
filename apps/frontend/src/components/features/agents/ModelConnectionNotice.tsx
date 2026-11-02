'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import type { AgentConfigApi } from '@/lib/api/agents';

/** Local accounts have no shared inference: setup is actionable only when enabled. */
export function ModelConnectionNotice({ config }: { config: AgentConfigApi | null }) {
  const t = useT();
  if (!config?.model_connection_required) return null;
  const enabled = config.model_connections_enabled === true;
  return (
    <div role="status" className="my-3 min-w-0 space-y-2 rounded-xl border border-border p-4">
      <p className="text-sm font-medium">
        {t(enabled ? 'modelSource.setupTitle' : 'modelSource.disabledTitle')}
      </p>
      <p className="text-sm leading-6 text-muted">
        {t(enabled ? 'modelSource.setupDescription' : 'modelSource.disabledDescription')}
      </p>
      {enabled ? (
        <Link
          href="/settings/my-settings#settings-connections"
          className="ui-button ui-button-secondary inline-flex min-h-11 max-w-full items-center whitespace-normal"
        >
          {t('modelSource.setupAction')}
        </Link>
      ) : null}
    </div>
  );
}
