'use client';

// The pieces every card of the Usage page is built from: the card, a metric
// tile, a load error, and the one range control the history and the context
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

/** Refusals the server types, in the reader's words. */
const ERROR_KEY: Partial<Record<string, TranslationKey>> = {
  usage_query_too_large: 'usage.error.tooLarge',
  usage_response_too_large: 'usage.error.responseTooLarge',
  usage_temporarily_unavailable: 'usage.error.unavailable',
};

/**
 * A failed read in the reader's language. The server's own message is
 * English prose for logs and is never shown.
 */
export function errorText(cause: unknown, t: (key: TranslationKey) => string): string {
  if (!(cause instanceof APIError)) return t('settings.usage.loadError');
  const key = ERROR_KEY[cause.code];
  if (key) return t(key);
  return t(cause.statusCode === 503 ? 'usage.error.unavailable' : 'settings.usage.loadError');
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

/**
 * What interval the numbers below cover, in UTC days, as the server resolved
 * it, and, for a chart, what one bar stands for.
 */
export function RangeNote({
  period,
  bars = true,
}: {
  period: UsageRange | null | undefined;
  bars?: boolean;
}) {
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
      {bars ? `${text} ${t(BUCKET_NOTE[period.bucket])}` : text}
    </p>
  );
}
