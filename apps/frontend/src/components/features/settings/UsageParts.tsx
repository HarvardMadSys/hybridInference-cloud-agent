'use client';

// The pieces every card of the Usage page is built from: the card, a metric
// tile, a load error, and the range control the history and the context
// insights share.

import { useLocale } from '@/components/providers';
import { APIError } from '@/lib/utils/errors';
import { USAGE_RANGE_PRESETS, type UsageRange, type UsageRangePreset } from '@/lib/api/ownerUsage';
import type { TranslationKey } from '@/lib/i18n/translate';
import { Select } from '@/components/ui/Select';
import { rangeSpan } from './usagePresent';

export const RANGE_LABEL: Record<UsageRangePreset, TranslationKey> = {
  today: 'usage.range.today',
  '7d': 'usage.range.7d',
  '30d': 'usage.range.30d',
  all: 'usage.range.all',
};

const BUCKET_NOTE: Record<UsageRange['bucket'], TranslationKey> = {
  day: 'usage.range.bucket.day',
  week: 'usage.range.bucket.week',
  month: 'usage.range.bucket.month',
};

/** A failed read in the reader's language; the server's own words only as a last resort. */
export function errorText(cause: unknown, t: (key: TranslationKey) => string): string | null {
  if (cause instanceof APIError && cause.code === 'usage_too_large')
    return t('usage.error.tooLarge');
  if (cause instanceof APIError && cause.code === 'usage_unavailable')
    return t('usage.error.unavailable');
  return cause instanceof Error && cause.message ? cause.message : null;
}

export function Card({
  id,
  title,
  action,
  children,
}: {
  id: string;
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section aria-labelledby={id} className="ui-card mt-4 bg-surface p-3 sm:p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id={id} className="text-base font-semibold text-foreground">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Tile({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border px-3 py-2">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 break-words text-base font-semibold tabular-nums text-foreground sm:text-lg">
        {value}
      </dd>
      {detail ? <dd className="mt-0.5 truncate text-xs text-muted">{detail}</dd> : null}
    </div>
  );
}

export function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  const { t } = useLocale();
  return (
    <div
      role="alert"
      className="mt-3 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
    >
      <span className="min-w-0 break-words">{message}</span>
      <button type="button" onClick={onRetry} className="ui-button ui-focus-ring min-h-11 shrink-0">
        {t('settings.usage.retry')}
      </button>
    </div>
  );
}

/** The one range the history and the context insights read, as a dropdown. */
export function RangeSelect({
  id,
  value,
  onChange,
}: {
  id: string;
  value: UsageRangePreset;
  onChange: (value: UsageRangePreset) => void;
}) {
  const { t } = useLocale();
  return (
    <Select
      id={id}
      value={value}
      onValueChange={onChange}
      aria-label={t('usage.range.label')}
      className="ui-input min-h-9 w-40 text-sm [@media(pointer:coarse)]:min-h-11"
      options={USAGE_RANGE_PRESETS.map((preset) => ({
        value: preset,
        label: t(RANGE_LABEL[preset]),
      }))}
    />
  );
}

/** What interval the numbers below cover, in UTC days, as the server resolved it. */
export function RangeNote({ period }: { period: UsageRange | null | undefined }) {
  const { t, locale } = useLocale();
  if (!period) return null;
  const span = rangeSpan(period, locale);
  let text: string;
  if (period.preset === 'all') {
    text = period.first_record_at
      ? t('usage.range.allSince', { since: span.since })
      : t('usage.range.allEmpty');
  } else {
    text = t('usage.range.span', span);
  }
  return (
    <p role="note" className="mt-2 text-xs text-muted">
      {text} {t(BUCKET_NOTE[period.bucket])}
    </p>
  );
}
