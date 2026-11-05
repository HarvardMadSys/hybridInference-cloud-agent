'use client';

// Token Stats: the conversation's tokens as a ring. Layout adapted from
// bowenliang123/dsh-context `src/client/components/statsTokens.tsx`
// (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001). Its accounting
// is not: dsh-context scales estimated categories to the billed input
// (`anchoredParts`), which here would invent system-prompt and schema sizes.
// So the ring shows one basis at a time and never mixes their denominators:
//
// - Recorded content (estimate): each stored item once, by category; the
//   system prompt and tool schemas every request carries are listed as not
//   recorded, with no arc.
// - Reported by the providers (exact): every request's input, split by cache,
//   and output, summed.

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { SessionTokensApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import { DashCard, Toggle, shareText } from './DashCard';
import { Donut, type DonutSegment } from './Donut';
import { SliceList, type SliceRow } from './SliceList';
import { PALETTE, labelOf, swatchOf } from './palette';

type Basis = 'recorded' | 'provider';

const PROVIDER_PARTS = [
  ['uncached', 'dashboard.tokens.uncached', 'bg-sky-500', 'stroke-sky-500'],
  ['cache_read', 'dashboard.tokens.cacheRead', 'bg-teal-500', 'stroke-teal-500'],
  ['cache_write', 'dashboard.tokens.cacheWrite', 'bg-amber-500', 'stroke-amber-500'],
  ['output', 'dashboard.tokens.output', 'bg-pink-500', 'stroke-pink-500'],
] as const;

export function providerValues(tokens: SessionTokensApi): Record<string, number> {
  const {
    input_tokens: input,
    cache_read_tokens: read,
    cache_write_tokens: write,
  } = tokens.provider;
  return {
    // Cache reads and writes are parts of the input; the rest was not cached.
    uncached: Math.max(0, input - read - write),
    cache_read: read,
    cache_write: write,
    output: tokens.provider.output_tokens,
  };
}

export function TokenStats({ tokens }: { tokens: SessionTokensApi | null | undefined }) {
  const { t, locale } = useLocale();
  const [basis, setBasis] = useState<Basis>('recorded');
  const [hover, setHover] = useState<string | null>(null);
  const hit = tokens?.provider.cache_hit;
  const aside = (
    <div className="flex flex-wrap items-center gap-2">
      {hit !== null && hit !== undefined ? (
        <span className="text-[11px] text-muted" title={t('dashboard.tokens.cacheHitTip')}>
          {t('dashboard.tokens.cacheHit', { pct: `${Math.round(hit * 100)}%` })}
        </span>
      ) : null}
      <Toggle
        label={t('dashboard.tokens.basis')}
        value={basis}
        onChange={setBasis}
        options={[
          {
            value: 'recorded',
            label: t('dashboard.tokens.recorded'),
            tip: t('dashboard.tokens.recordedTip'),
          },
          {
            value: 'provider',
            label: t('dashboard.tokens.provider'),
            tip: t('dashboard.tokens.providerTip'),
          },
        ]}
      />
    </div>
  );
  if (!tokens) {
    return (
      <DashCard title={t('dashboard.tokens.title')} aside={aside}>
        <p className="text-xs text-muted">{t('dashboard.tokens.none')}</p>
      </DashCard>
    );
  }
  let segments: DonutSegment[];
  let rows: SliceRow[];
  let center: string;
  let sub: string;
  let note: string;
  if (basis === 'recorded') {
    const total = tokens.recorded_total;
    segments = tokens.recorded.map((share) => ({
      key: share.key,
      stroke: swatchOf(share.key).stroke,
      value: share.tokens,
    }));
    rows = [
      ...tokens.not_recorded.map((key) => ({
        key,
        bg: PALETTE[key]?.bg ?? PALETTE.other.bg,
        label: t(labelOf(key)),
        count: t('dashboard.notRecorded'),
        pct: null,
        tip: t('dashboard.notRecordedTip'),
        dim: true,
      })),
      ...tokens.recorded.map((share) => ({
        key: share.key,
        bg: swatchOf(share.key).bg,
        label: t(labelOf(share.key)),
        count: `≈${compactCount(share.tokens)}`,
        pct: shareText(share.tokens, total),
        tip: t('dashboard.tokens.items', { count: share.items.toLocaleString(locale) }),
      })),
    ];
    center = `≈${compactCount(total)}`;
    sub = t('dashboard.tokens.recordedSub');
    note = t('dashboard.tokens.recordedNote', {
      read: tokens.turns_read.toLocaleString(locale),
      turns: tokens.turns.toLocaleString(locale),
    });
  } else {
    const values = providerValues(tokens);
    const total = tokens.provider.input_tokens + tokens.provider.output_tokens;
    segments = PROVIDER_PARTS.map(([key, , , stroke]) => ({ key, stroke, value: values[key] }));
    rows = PROVIDER_PARTS.map(([key, label, bg]) => ({
      key,
      bg,
      label: t(label),
      count: compactCount(values[key]),
      pct: shareText(values[key], total),
    }));
    center = compactCount(total);
    sub = t('dashboard.tokens.providerSub');
    note = t('dashboard.tokens.providerNote', {
      requests: tokens.provider.requests.toLocaleString(locale),
    });
    if (tokens.provider.subagent_requests > 0) {
      note += ` ${t('dashboard.tokens.subagents', {
        count: tokens.provider.subagent_requests.toLocaleString(locale),
        tokens: compactCount(tokens.provider.subagent_input_tokens),
      })}`;
    }
  }
  const empty = segments.every((segment) => segment.value <= 0);
  return (
    <DashCard title={t('dashboard.tokens.title')} aside={aside}>
      <div className="flex flex-wrap items-center gap-3">
        <Donut
          segments={segments}
          centerTop={empty ? '—' : center}
          centerSub={sub}
          hoverKey={hover}
          onHoverKey={setHover}
        />
        <SliceList
          rows={rows}
          hoverKey={hover}
          onHoverKey={setHover}
          label={t('dashboard.tokens.title')}
        />
      </div>
      <p className="mt-2 text-[11px] text-muted">{note}</p>
      {basis === 'recorded' && tokens.truncated ? (
        <p className="text-[11px] text-muted">{t('dashboard.tokens.truncated')}</p>
      ) : null}
    </DashCard>
  );
}
