'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers';
import { getAgentConfig, type ProvidedModelSourceApi } from '@/lib/api/agents';
import { providedSources } from '../agents/composer/providedModels';

/** How long the page waits for `/config` before going on without it. */
export const PROVIDED_WAIT_MS = 4000;

/**
 * The provided sources that offer this owner a model. `null` while `/config`
 * is awaited; none where the deployment provides nothing, when that cannot be
 * read (the composer says what went wrong), or when the answer is slow enough
 * that the page should not keep an owner from their own connections. A late
 * answer still counts when it comes.
 */
export function useProvidedSources(): ProvidedModelSourceApi[] | null {
  const [sources, setSources] = useState<ProvidedModelSourceApi[] | null>(null);

  useEffect(() => {
    let active = true;
    const waited = window.setTimeout(() => setSources((known) => known ?? []), PROVIDED_WAIT_MS);
    void getAgentConfig()
      .then((config) => providedSources(config).filter((source) => source.models.length > 0))
      .catch(() => [])
      .then((found) => {
        window.clearTimeout(waited);
        if (active) setSources(found);
      });
    return () => {
      active = false;
      window.clearTimeout(waited);
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
