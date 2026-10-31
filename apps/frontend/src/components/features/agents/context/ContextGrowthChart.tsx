'use client';

import { useState, type KeyboardEvent, type PointerEvent } from 'react';
import { useLocale } from '@/components/providers';
import type { ContextPointApi, ContextTurnApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import { chartScale, signedTokens, turnPoints } from './contextPresent';

type Mode = 'request' | 'turn';

export interface ChartSelection {
  point: ContextPointApi;
  turn: ContextTurnApi | undefined;
}

/**
 * One bar per model request (or per turn's last request), its height that
 * request's input tokens. A marker stands before every request that follows a
 * compaction the CLI reported; a drop without one has no marker. The reported
 * window, when every request shares one, is a dashed line.
 */
export function ContextGrowthChart({
  points,
  turns,
  selected,
  onSelect,
}: {
  points: ContextPointApi[];
  turns: ContextTurnApi[];
  selected: number | null;
  onSelect: (index: number) => void;
}) {
  const { t } = useLocale();
  const [mode, setMode] = useState<Mode>(
    turns.length > 1 && points.length > 400 ? 'turn' : 'request',
  );
  const series = mode === 'turn' ? turnPoints(turns, points).map((entry) => entry.point) : points;
  const scale = chartScale(series);
  const count = series.length;
  const position = series.findIndex((point) => point.index === selected);
  const active = position >= 0 ? position : count - 1;

  const pick = (offset: number) => {
    const next = series[Math.min(count - 1, Math.max(0, offset))];
    if (next) onSelect(next.index);
  };
  const onPointer = (event: PointerEvent<SVGSVGElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    if (box.width <= 0) return;
    pick(Math.floor(((event.clientX - box.left) / box.width) * count));
  };
  const onKey = (event: KeyboardEvent<SVGSVGElement>) => {
    const moves: Record<string, number> = {
      ArrowLeft: active - 1,
      ArrowRight: active + 1,
      Home: 0,
      End: count - 1,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    pick(moves[event.key]);
  };
  if (count === 0) return null;
  const gap = count <= 120 ? 0.18 : 0;
  const label = t(mode === 'turn' ? 'context.chartTurnLabel' : 'context.chartLabel');
  const current = series[active];
  let caption: string | null = null;
  if (current) {
    const tokens = compactCount(current.prompt_tokens);
    const where = t('context.point', { index: current.index + 1, turn: current.turn_no, tokens });
    caption = `${where} · ${signedTokens(current.delta_tokens, compactCount)}`;
  }
  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div
          role="group"
          aria-label={t('context.growth')}
          className="flex gap-1 rounded-lg border border-border bg-surface-muted p-1"
        >
          {(['request', 'turn'] as const).map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={item === mode}
              onClick={() => setMode(item)}
              className={`ui-focus-ring min-h-8 rounded-md px-2.5 text-xs font-medium [@media(pointer:coarse)]:min-h-11 ${
                item === mode
                  ? 'bg-surface text-foreground shadow-subtle'
                  : 'text-muted hover:bg-surface-hover hover:text-foreground'
              }`}
            >
              {t(item === 'turn' ? 'context.view.turn' : 'context.view.request')}
            </button>
          ))}
        </div>
        <p className="text-xs tabular-nums text-muted" aria-live="polite">
          {caption}
        </p>
      </div>
      <div className="relative mt-2">
        <span className="absolute left-0 top-0 text-[11px] tabular-nums text-muted">
          {compactCount(scale.top)}
        </span>
        <svg
          role="img"
          aria-label={label}
          tabIndex={0}
          viewBox={`0 0 ${count} 100`}
          preserveAspectRatio="none"
          onPointerMove={onPointer}
          onPointerDown={onPointer}
          onKeyDown={onKey}
          className="ui-focus-ring mt-4 block h-40 w-full cursor-crosshair touch-none rounded border-b border-border"
        >
          {scale.window !== null ? (
            <line
              x1={0}
              x2={count}
              y1={100 - scale.window * 100}
              y2={100 - scale.window * 100}
              className="stroke-muted"
              strokeDasharray="4 3"
              vectorEffect="non-scaling-stroke"
            />
          ) : null}
          {series.map((point, index) => {
            const height = scale.top > 0 ? (point.prompt_tokens / scale.top) * 100 : 0;
            return (
              <rect
                key={point.index}
                x={index + gap / 2}
                width={1 - gap}
                y={100 - height}
                height={height}
                className={index === active ? 'fill-accent' : 'fill-accent/40'}
              />
            );
          })}
          {series.map((point, index) =>
            point.after_compaction ? (
              <line
                key={`compaction-${point.index}`}
                x1={index}
                x2={index}
                y1={0}
                y2={100}
                className="stroke-status-warning"
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
              >
                <title>{t('context.compactionMark')}</title>
              </line>
            ) : null,
          )}
        </svg>
        <div className="mt-1 flex justify-between text-[11px] tabular-nums text-muted">
          <span>1</span>
          <span>{mode === 'turn' ? `#${series[count - 1].turn_no}` : count}</span>
        </div>
      </div>
    </div>
  );
}
