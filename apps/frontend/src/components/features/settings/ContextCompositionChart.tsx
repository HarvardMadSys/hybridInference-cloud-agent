'use client';

// Two bars that are never added together or subtracted from each other:
//
// - Recorded content: what the turns stored, each item once, by category.
//   An estimate from characters. CLI system prompts and tool definitions are
//   never stored, so no category stands for them.
// - Request input the providers reported: exact, summed over requests, split
//   the way it was billed (from cache or not). Kept text counts again for
//   every request that carried it, which is why it is the larger number.

import { useLocale } from '@/components/providers';
import type { ContextComposition } from '@/lib/api/ownerUsage';
import type { TranslationKey } from '@/lib/i18n/translate';
import { CATEGORY_COLOR, categoryLabel } from '../agents/context/requestPresent';
import { providerSlices, recordedSlices, type CompositionSlice } from './contextInsightsPresent';
import { compactCount } from './usagePresent';

const PROVIDER_LABEL: Record<string, TranslationKey> = {
  uncached: 'contextInsights.composition.uncached',
  cache_read: 'contextInsights.composition.cacheRead',
  cache_write: 'contextInsights.composition.cacheWrite',
};

const PROVIDER_COLOR: Record<string, string> = {
  uncached: 'bg-sky-600/70',
  cache_read: 'bg-emerald-500/60',
  cache_write: 'bg-amber-500/70',
};

function sliceLabel(slice: CompositionSlice): TranslationKey {
  return PROVIDER_LABEL[slice.key] ?? categoryLabel(slice.key);
}

function sliceColor(slice: CompositionSlice): string {
  return PROVIDER_COLOR[slice.key] ?? CATEGORY_COLOR[slice.key] ?? CATEGORY_COLOR.other;
}

export function ContextCompositionChart({ composition }: { composition: ContextComposition }) {
  const { t, locale } = useLocale();
  const { recorded, provider } = composition;
  const recordedParts = recordedSlices(composition);
  const providerParts = providerSlices(composition);
  const truncated = t('contextInsights.composition.truncated', {
    count: recorded.turns_truncated.toLocaleString(locale),
  });
  if (recorded.total_tokens === 0 && provider.requests === 0) {
    return (
      <section aria-label={t('contextInsights.composition')} className="mt-4">
        <h4 className="text-xs font-semibold text-foreground">
          {t('contextInsights.composition')}
        </h4>
        <p className="mt-1 text-xs text-muted">{t('contextInsights.composition.none')}</p>
      </section>
    );
  }
  return (
    <section aria-label={t('contextInsights.composition')} className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextInsights.composition')}</h4>
      <div className="mt-2 grid gap-4 lg:grid-cols-2">
        <div className="min-w-0 rounded-lg border border-border p-3">
          <p className="text-xs font-medium text-foreground">
            {t('contextInsights.composition.recorded')}
          </p>
          <p className="mt-0.5 text-sm tabular-nums text-foreground">
            {t('contextInsights.composition.total', {
              tokens: compactCount(recorded.total_tokens),
            })}
          </p>
          <Bar parts={recordedParts} label={t('contextInsights.composition.recorded')} />
          <Legend parts={recordedParts} estimated />
          <p className="mt-2 text-[11px] text-muted">
            {t('contextInsights.composition.coverage', {
              profiled: recorded.turns_profiled.toLocaleString(locale),
              turns: recorded.turns.toLocaleString(locale),
            })}
            {recorded.turns_truncated > 0 ? ` · ${truncated}` : ''}
          </p>
          <p className="mt-1 text-[11px] text-muted">
            {t('contextInsights.composition.recordedNote')}
          </p>
        </div>
        <div className="min-w-0 rounded-lg border border-border p-3">
          <p className="text-xs font-medium text-foreground">
            {t('contextInsights.composition.provider')}
          </p>
          <p className="mt-0.5 text-sm tabular-nums text-foreground">
            {t('contextInsights.composition.input', {
              tokens: compactCount(provider.prompt_tokens),
            })}
            {' · '}
            {t('contextInsights.composition.output', {
              tokens: compactCount(provider.output_tokens),
            })}
          </p>
          <Bar parts={providerParts} label={t('contextInsights.composition.provider')} />
          <Legend parts={providerParts} />
          {provider.output_unreported > 0 ? (
            <p className="mt-2 text-[11px] text-muted">
              {t('contextInsights.composition.outputUnreported', {
                count: provider.output_unreported.toLocaleString(locale),
              })}
            </p>
          ) : null}
          {provider.subagent_requests > 0 ? (
            <p className="mt-1 text-[11px] text-muted">
              {t('contextInsights.composition.subagents', {
                count: provider.subagent_requests.toLocaleString(locale),
                tokens: compactCount(provider.subagent_prompt_tokens),
              })}
            </p>
          ) : null}
          <p className="mt-1 text-[11px] text-muted">
            {t('contextInsights.composition.providerNote', {
              count: provider.requests.toLocaleString(locale),
            })}
          </p>
        </div>
      </div>
    </section>
  );
}

/** A stacked bar; each part's width is its share of the bar's own total. */
export function Bar({ parts, label }: { parts: CompositionSlice[]; label: string }) {
  if (parts.length === 0) return null;
  return (
    <div
      role="img"
      aria-label={label}
      className="mt-2 flex h-2.5 w-full overflow-hidden rounded-full bg-surface-muted"
    >
      {parts.map((part) => (
        <span
          key={part.key}
          className={`h-full ${sliceColor(part)}`}
          style={{ width: `${part.share * 100}%` }}
        />
      ))}
    </div>
  );
}

function Legend({ parts, estimated = false }: { parts: CompositionSlice[]; estimated?: boolean }) {
  const { t } = useLocale();
  return (
    <ul className="mt-2 grid grid-cols-1 gap-y-0.5 text-xs">
      {parts.map((part) => (
        <li key={part.key} className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5 text-muted">
            <span
              aria-hidden="true"
              className={`h-2 w-2 shrink-0 rounded-full ${sliceColor(part)}`}
            />
            <span className="truncate">{t(sliceLabel(part))}</span>
          </span>
          <span className="shrink-0 tabular-nums text-foreground">
            {estimated ? '≈' : ''}
            {compactCount(part.tokens)} · {Math.round(part.share * 100)}%
          </span>
        </li>
      ))}
    </ul>
  );
}
