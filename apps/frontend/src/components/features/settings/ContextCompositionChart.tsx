'use client';

// What the turns stored, each item once, by category: an estimate from
// characters. CLI system prompts and tool definitions are never stored, so no
// category stands for them. It is not billed usage: the tokens and cache the
// providers reported are the history's, above, and each conversation's own
// cost and cache hit are in its row.

import { useLocale } from '@/components/providers';
import type { ContextComposition } from '@/lib/api/ownerUsage';
import { CATEGORY_COLOR, categoryLabel } from '../agents/context/requestPresent';
import { recordedSlices, type CompositionSlice } from './contextInsightsPresent';
import { compactCount } from './usagePresent';

function sliceColor(slice: CompositionSlice): string {
  return CATEGORY_COLOR[slice.key] ?? CATEGORY_COLOR.other;
}

export function ContextCompositionChart({ composition }: { composition: ContextComposition }) {
  const { t, locale } = useLocale();
  const { recorded } = composition;
  const parts = recordedSlices(composition);
  const truncated = t('contextInsights.composition.truncated', {
    count: recorded.turns_truncated.toLocaleString(locale),
  });
  return (
    <section aria-label={t('contextInsights.composition')} className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextInsights.composition')}</h4>
      {recorded.total_tokens === 0 ? (
        <p className="mt-1 text-xs text-muted">{t('contextInsights.composition.none')}</p>
      ) : (
        <div className="mt-2 min-w-0 rounded-lg border border-border p-3">
          <p className="text-xs font-medium text-foreground">
            {t('contextInsights.composition.recorded')}
          </p>
          <p className="mt-0.5 text-sm tabular-nums text-foreground">
            {t('contextInsights.composition.total', {
              tokens: compactCount(recorded.total_tokens),
            })}
          </p>
          <Bar parts={parts} label={t('contextInsights.composition.recorded')} />
          <Legend parts={parts} />
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
      )}
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

/** Every recorded category, its estimated tokens and its share. */
function Legend({ parts }: { parts: CompositionSlice[] }) {
  const { t } = useLocale();
  return (
    <ul className="mt-2 grid grid-cols-1 gap-x-6 gap-y-0.5 text-xs sm:grid-cols-2">
      {parts.map((part) => (
        <li key={part.key} className="flex items-center justify-between gap-2">
          <span className="flex min-w-0 items-center gap-1.5 text-muted">
            <span
              aria-hidden="true"
              className={`h-2 w-2 shrink-0 rounded-full ${sliceColor(part)}`}
            />
            <span className="truncate">{t(categoryLabel(part.key))}</span>
          </span>
          <span className="shrink-0 tabular-nums text-foreground">
            ≈{compactCount(part.tokens)} · {Math.round(part.share * 100)}%
          </span>
        </li>
      ))}
    </ul>
  );
}
