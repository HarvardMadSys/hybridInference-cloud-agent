'use client';

import { useEffect, useState } from 'react';
import { useT } from '@/components/providers';
import { getAgentConfig, type ProvidedModelSourceApi } from '@/lib/api/agents';
import {
  allowanceOf,
  providedSources,
  resetTime,
  type Allowance,
} from '../agents/composer/providedModels';

const cell = 'px-3 py-2 align-top';

type Translate = ReturnType<typeof useT>;

function count(value: number): string {
  return value.toLocaleString();
}

/** What is left of an allowance today, or when it comes back. */
function leftText(allowance: Allowance, t: Translate): string {
  if (allowance.remaining === null) return t('provided.unlimited');
  if (allowance.exhausted) return t('provided.noneLeft', { time: resetTime(allowance.resetsAt) });
  return count(allowance.remaining);
}

/** One provided source: each model this owner may use, its allowance and today's use. */
function SourceTable({ source }: { source: ProvidedModelSourceApi }) {
  const t = useT();
  return (
    <article className="min-w-0 rounded-xl border border-border p-4" aria-label={source.name}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="break-words text-sm font-semibold">{source.name}</h3>
        {source.is_default ? (
          <span className="text-xs text-muted">{t('provided.default')}</span>
        ) : null}
      </div>
      {source.models.length === 0 ? (
        <p className="mt-2 text-sm text-muted">{t('provided.noModels')}</p>
      ) : (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[28rem] text-left text-sm">
            <caption className="sr-only">{source.name}</caption>
            <thead className="border-b border-border text-xs text-muted">
              <tr>
                <th scope="col" className={`${cell} pl-0 font-medium`}>
                  {t('provided.model')}
                </th>
                <th scope="col" className={`${cell} text-right font-medium`}>
                  {t('provided.allowance')}
                </th>
                <th scope="col" className={`${cell} text-right font-medium`}>
                  {t('provided.used')}
                </th>
                <th scope="col" className={`${cell} pr-0 text-right font-medium`}>
                  {t('provided.left')}
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {source.models.map((model) => {
                const allowance = allowanceOf(source, model);
                return (
                  <tr key={model}>
                    <th scope="row" className={`${cell} break-all pl-0 font-mono text-[13px]`}>
                      {model}
                    </th>
                    <td className={`${cell} text-right tabular-nums`}>
                      {allowance.limit === null ? t('provided.unlimited') : count(allowance.limit)}
                    </td>
                    <td className={`${cell} text-right tabular-nums`}>{count(allowance.used)}</td>
                    <td className={`${cell} pr-0 text-right tabular-nums`}>
                      {leftText(allowance, t)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="mt-3 text-xs leading-5 text-muted">
        {t('provided.resets', { time: resetTime(source.resets_at) })}
      </p>
    </article>
  );
}

/**
 * The models this deployment provides to the owner: for each, the daily
 * request allowance, today's use and when the count starts over. Read-only:
 * an administrator sets all of it. Nothing is drawn when the deployment
 * provides none, or while that is not known.
 */
export function ProvidedModelsCard() {
  const t = useT();
  const [sources, setSources] = useState<ProvidedModelSourceApi[]>([]);

  useEffect(() => {
    let active = true;
    void getAgentConfig()
      .then((config) => {
        if (active) setSources(providedSources(config));
      })
      .catch(() => {
        // Without the answer there is nothing to list; the composer says what went wrong.
      });
    return () => {
      active = false;
    };
  }, []);

  if (sources.length === 0) return null;
  return (
    <section
      aria-labelledby="settings-provided-models"
      className="ui-card mt-4 min-w-0 bg-surface p-4 sm:p-5"
    >
      <h2 id="settings-provided-models" className="text-base font-semibold">
        {t('provided.title')}
      </h2>
      <p className="mt-1.5 max-w-2xl text-sm leading-6 text-muted">{t('provided.description')}</p>
      <div className="mt-4 space-y-3">
        {sources.map((source) => (
          <SourceTable key={source.id} source={source} />
        ))}
      </div>
    </section>
  );
}
