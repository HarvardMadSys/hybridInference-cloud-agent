'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers';
import { getAgentConfig, type ProvidedModelSourceApi } from '@/lib/api/agents';
import { providedSources } from '../agents/composer/providedModels';

/**
 * The provided sources that offer this owner a model. `null` until `/config`
 * answers; none where the deployment provides nothing, or when that cannot be
 * read (the composer says what went wrong).
 */
export function useProvidedSources(): ProvidedModelSourceApi[] | null {
  const [sources, setSources] = useState<ProvidedModelSourceApi[] | null>(null);

  useEffect(() => {
    let active = true;
    void getAgentConfig()
      .then((config) => providedSources(config).filter((source) => source.models.length > 0))
      .catch(() => [])
      .then((found) => {
        if (active) setSources(found);
      });
    return () => {
      active = false;
    };
  }, []);

  return sources;
}

/**
 * The models this deployment provides to the owner, by name and nothing else.
 * Where they come from is not the owner's concern, so the list has no heading
 * and says nothing of the deployment; the model picker says how many requests
 * are left today. A source is named only when there is more than one, to tell
 * apart two that list the same model.
 */
export function ProvidedModelsCard({ sources }: { sources: ProvidedModelSourceApi[] }) {
  const t = useT();
  if (sources.length === 0) return null;
  const named = sources.length > 1;
  return (
    <section
      aria-label={t('provided.title')}
      className="ui-card mt-4 min-w-0 bg-surface px-4 py-1.5 sm:px-5"
    >
      {sources.map((source) => (
        <div key={source.id}>
          {named ? <h2 className="pt-3 text-xs font-medium text-muted">{source.name}</h2> : null}
          <ul aria-label={source.name} className="divide-y divide-border">
            {source.models.map((model) => (
              <li key={model} className="flex min-h-11 items-center break-all py-2 text-sm">
                {model}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </section>
  );
}
