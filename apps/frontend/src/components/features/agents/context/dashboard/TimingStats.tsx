'use client';

// Timing Stats: where the executions' time went, as a ring with its active
// time in the centre and a strip of the time in order. Layout adapted from
// bowenliang123/dsh-context `statsTiming.tsx` and `timingStrip.tsx`
// (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001). The numbers
// come from `GET …/thread/context/timing` (see `cloud_agent.context_timing`):
// TTFT and generation as the Gateway measured them, Claude Code's streamed
// block times inside generation, tool windows from the event ledger, and the
// rest of each execution as "not attributed". A split that was not measured is
// listed as such, never drawn as 0.

import { useMemo, useState } from 'react';
import { useLocale } from '@/components/providers';
import type { ThreadTimingApi, TimingKind } from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import { formatDuration } from '../../TurnFooter';
import { DashCard, shareText } from './DashCard';
import { Donut } from './Donut';
import { SliceList, type SliceRow } from './SliceList';
import { TIMING_SWATCH } from './palette';
import { TIMING_DOM_MAX, TIMING_ORDER, bandAt, stripBands, stripGradient } from './timingModel';

const LABEL: Record<TimingKind, TranslationKey> = {
  ttft: 'dashboard.timing.ttft',
  reasoning: 'dashboard.timing.reasoning',
  text: 'dashboard.timing.text',
  toolarg: 'dashboard.timing.toolarg',
  gen: 'dashboard.timing.gen',
  tools: 'dashboard.timing.tools',
  unsplit: 'dashboard.timing.unsplit',
  other: 'dashboard.timing.other',
};

const TIP: Partial<Record<TimingKind, TranslationKey>> = {
  ttft: 'dashboard.timing.ttftTip',
  gen: 'dashboard.timing.genTip',
  tools: 'dashboard.timing.toolsTip',
  unsplit: 'dashboard.timing.unsplitTip',
  other: 'dashboard.timing.otherTip',
};

/** Rows of the legend: measured kinds, and splits that were not measured. */
export function timingRows(
  timing: ThreadTimingApi,
  t: (key: TranslationKey, vars?: Record<string, string | number>) => string,
): SliceRow[] {
  const split = timing.executions_split > 0;
  const blocks = Object.values(timing.blocks).some((count) => count > 0);
  const rows: SliceRow[] = [];
  for (const kind of TIMING_ORDER) {
    const ms = timing.totals[kind] ?? 0;
    const blockKind = kind === 'reasoning' || kind === 'text' || kind === 'toolarg';
    const missing = (kind === 'ttft' && !split) || (blockKind && !blocks);
    if (missing) {
      rows.push({
        key: kind,
        bg: TIMING_SWATCH[kind].bg,
        label: t(LABEL[kind]),
        count: t('dashboard.notRecorded'),
        pct: null,
        dim: true,
        tip: t(kind === 'ttft' ? 'dashboard.timing.noGateway' : 'dashboard.timing.noBlocks'),
      });
      continue;
    }
    if (ms <= 0) continue;
    const tip = TIP[kind];
    rows.push({
      key: kind,
      bg: TIMING_SWATCH[kind].bg,
      label: t(LABEL[kind]),
      count: formatDuration(ms) ?? '—',
      pct: shareText(ms, timing.active_ms),
      ...(tip ? { tip: t(tip) } : {}),
    });
  }
  return rows;
}

export function TimingStats({
  timing,
  loading,
}: {
  timing: ThreadTimingApi | null;
  loading: boolean;
}) {
  const { t, locale } = useLocale();
  const [hover, setHover] = useState<string | null>(null);
  const aside =
    timing?.output_tps != null ? (
      <span className="text-[11px] text-muted" title={t('dashboard.timing.tpsTip')}>
        {t('dashboard.timing.tps', {
          tps:
            timing.output_tps >= 10 ? Math.round(timing.output_tps) : timing.output_tps.toFixed(1),
        })}
      </span>
    ) : null;
  if (!timing || timing.active_ms <= 0) {
    return (
      <DashCard title={t('dashboard.timing.title')} aside={aside}>
        <p className="text-xs text-muted">
          {t(loading ? 'context.loading' : 'dashboard.timing.empty')}
        </p>
      </DashCard>
    );
  }
  const segments = TIMING_ORDER.map((kind) => ({
    key: kind,
    stroke: TIMING_SWATCH[kind].stroke,
    value: timing.totals[kind] ?? 0,
  }));
  const notes: string[] = [];
  if (timing.gateway !== 'available') {
    notes.push(
      t(`dashboard.timing.gateway.${timing.gateway}` as TranslationKey, {
        split: timing.executions_split.toLocaleString(locale),
        total: timing.executions.toLocaleString(locale),
      }),
    );
  }
  if (timing.tool_calls_unpaired > 0) {
    notes.push(
      t('dashboard.timing.unpaired', { count: timing.tool_calls_unpaired.toLocaleString(locale) }),
    );
  }
  return (
    <DashCard title={t('dashboard.timing.title')} aside={aside}>
      <div className="flex flex-wrap items-center gap-3">
        <Donut
          segments={segments}
          centerTop={formatDuration(timing.active_ms) ?? '—'}
          centerSub={t('dashboard.timing.active')}
          hoverKey={hover}
          onHoverKey={setHover}
        />
        <SliceList
          rows={timingRows(timing, t)}
          hoverKey={hover}
          onHoverKey={setHover}
          label={t('dashboard.timing.title')}
        />
      </div>
      <TimingStrip timing={timing} hover={hover} onHover={setHover} />
      <p className="mt-1 text-[11px] text-muted">
        {t('dashboard.timing.counts', {
          executions: timing.executions.toLocaleString(locale),
          requests: timing.model_requests.toLocaleString(locale),
          tools: timing.tool_calls.toLocaleString(locale),
        })}
        {notes.length ? ` ${notes.join(' ')}` : ''}
      </p>
    </DashCard>
  );
}

function TimingStrip({
  timing,
  hover,
  onHover,
}: {
  timing: ThreadTimingApi;
  hover: string | null;
  onHover: (key: string | null) => void;
}) {
  const { t, locale } = useLocale();
  const [tip, setTip] = useState<number | null>(null);
  const model = useMemo(() => stripBands(timing.segments), [timing.segments]);
  const dense = model.bands.length > TIMING_DOM_MAX;
  const base = useMemo(() => (dense ? stripGradient(model.bands, null) : ''), [dense, model]);
  if (model.bands.length === 0) return null;
  const hovered = hover as TimingKind | null;
  const band = tip !== null ? model.bands[tip] : null;
  const words = band
    ? `${t(LABEL[band.kind])} ${formatDuration(band.ms) ?? ''} · ${new Date(band.start).toLocaleTimeString(locale)}`
    : '';
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  return (
    <div className="mt-3" role="img" aria-label={t('dashboard.timing.strip')}>
      <div className="relative">
        <div
          className="relative h-3 w-full overflow-hidden rounded bg-surface-muted"
          onMouseMove={(event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            if (rect.width <= 0) return;
            const index = bandAt(model.bands, ((event.clientX - rect.left) / rect.width) * 100);
            setTip(index);
            onHover(model.bands[index].kind);
          }}
          onMouseLeave={() => {
            setTip(null);
            onHover(null);
          }}
        >
          {dense ? (
            <>
              <div className="absolute inset-0" style={{ background: base }} />
              {hovered ? (
                <div
                  className="absolute inset-0"
                  style={{ background: stripGradient(model.bands, hovered) }}
                />
              ) : null}
            </>
          ) : (
            model.bands.map((item, index) => (
              <div
                key={index}
                className={`absolute inset-y-0 ${TIMING_SWATCH[item.kind].bg} ${
                  hovered && hovered !== item.kind ? 'opacity-25' : ''
                }`}
                style={{ left: `${item.leftPct}%`, width: `${item.widthPct}%` }}
              />
            ))
          )}
        </div>
        {band ? (
          <div
            className="pointer-events-none absolute -top-7 z-10 -translate-x-1/2 whitespace-nowrap rounded border border-border bg-surface-raised px-1.5 py-0.5 text-[11px] text-foreground shadow-card"
            style={{ left: `${Math.max(10, Math.min(band.leftPct + band.widthPct / 2, 90))}%` }}
          >
            {words}
          </div>
        ) : null}
      </div>
      <div className="relative mt-0.5 h-3 text-[10px] text-muted">
        {ticks.map((frac) => (
          <span
            key={frac}
            className="absolute -translate-x-1/2 first:translate-x-0 last:-translate-x-full"
            style={{ left: `${frac * 100}%` }}
          >
            {frac === 0 ? '0' : formatDuration(model.total * frac)}
          </span>
        ))}
      </div>
      {timing.segments_merged ? (
        <p className="text-[10px] text-muted">{t('dashboard.timing.stripBounded')}</p>
      ) : null}
    </div>
  );
}
