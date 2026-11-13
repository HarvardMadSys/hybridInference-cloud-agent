'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import type { AgentConfigApi } from '@/lib/api/agents';
import { MODEL_CONNECTIONS_HREF } from '@/components/features/settings/navigation';
import { leadingProvidedSource, modelSetupRequired } from './providedModels';

/**
 * Shown while nothing can run until the owner adds a connection of their own;
 * setup is actionable only where the deployment lets owners add one. A model
 * the deployment provides, and this owner can run, is enough: no notice then.
 * Where a provided source exists but offers this owner nothing, the notice
 * names it instead of suggesting a provider.
 */
export function ModelConnectionNotice({ config }: { config: AgentConfigApi | null }) {
  const t = useT();
  if (!config || !modelSetupRequired(config)) return null;
  const enabled = config.model_connections_enabled === true;
  const provided = leadingProvidedSource(config);
  const description = !enabled
    ? t('modelSource.disabledDescription')
    : provided
      ? t('modelSource.setupDescriptionProvided', { name: provided.name })
      : t('modelSource.setupDescription');
  return (
    <div role="status" className="my-3 min-w-0 space-y-2 rounded-xl border border-border p-4">
      <p className="text-sm font-medium">
        {t(enabled ? 'modelSource.setupTitle' : 'modelSource.disabledTitle')}
      </p>
      <p className="text-sm leading-6 text-muted">{description}</p>
      {enabled ? (
        <Link
          href={MODEL_CONNECTIONS_HREF}
          className="ui-button ui-button-secondary inline-flex min-h-11 max-w-full items-center whitespace-normal"
        >
          {t('modelSource.setupAction')}
        </Link>
      ) : null}
    </div>
  );
}
