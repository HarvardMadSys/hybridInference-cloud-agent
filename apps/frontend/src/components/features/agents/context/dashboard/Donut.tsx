'use client';

// A ring of shares. Adapted from bowenliang123/dsh-context
// `src/client/components/donut.tsx` (Apache-2.0, commit
// 32982aa6aa2c70fbda6b81281de98a72ea625001): the same circle (r = 15.9155, so
// its circumference is 100), arcs laid with stroke-dasharray, a 0.5% gap
// between arcs and the hover dimming. The arcs are hidden from assistive
// technology; the legend beside the ring is the accessible list.

import type { ReactNode } from 'react';

const SEG_GAP = 0.5;

export interface DonutSegment {
  key: string;
  /** A Tailwind stroke class. */
  stroke: string;
  value: number;
}

export function donutArcs(
  segments: DonutSegment[],
): { key: string; stroke: string; len: number; offset: number }[] {
  let total = 0;
  for (const s of segments) if (Number.isFinite(s.value) && s.value > 0) total += s.value;
  const arcs: { key: string; stroke: string; len: number; offset: number }[] = [];
  let acc = 0;
  if (total <= 0) return arcs;
  for (const s of segments) {
    const value = Number.isFinite(s.value) && s.value > 0 ? s.value : 0;
    if (value === 0) continue;
    const pct = (value / total) * 100;
    arcs.push({ key: s.key, stroke: s.stroke, len: pct, offset: 100 - acc + 25 });
    acc += pct;
  }
  if (arcs.length > 1) {
    for (const arc of arcs) {
      const cut = Math.min(SEG_GAP / 2, arc.len / 4);
      arc.len -= cut * 2;
      arc.offset -= cut;
    }
  }
  return arcs;
}

export function Donut({
  segments,
  centerTop,
  centerSub,
  size = 104,
  hoverKey,
  onHoverKey,
}: {
  segments: DonutSegment[];
  centerTop: ReactNode;
  centerSub?: ReactNode;
  size?: number;
  hoverKey?: string | null;
  onHoverKey?: (key: string | null) => void;
}) {
  const arcs = donutArcs(segments);
  const hovering = hoverKey != null && arcs.some((arc) => arc.key === hoverKey);
  return (
    <div
      className="relative shrink-0"
      style={{ width: size, height: size }}
      onMouseLeave={() => onHoverKey?.(null)}
    >
      <svg viewBox="0 0 42 42" width={size} height={size} aria-hidden="true">
        {arcs.length === 0 ? (
          <circle className="fill-none stroke-border" strokeWidth={4} cx="21" cy="21" r="15.9155" />
        ) : (
          arcs.map((arc) => (
            <circle
              key={arc.key}
              className={`fill-none transition-opacity ${arc.stroke} ${
                hovering && hoverKey !== arc.key ? 'opacity-30' : ''
              }`}
              strokeWidth={hoverKey === arc.key ? 5 : 4}
              cx="21"
              cy="21"
              r="15.9155"
              strokeDasharray={`${arc.len} ${100 - arc.len}`}
              strokeDashoffset={arc.offset}
              onMouseEnter={() => onHoverKey?.(arc.key)}
            />
          ))
        )}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center leading-tight">
        <b
          className="tabular-nums text-foreground"
          style={{ fontSize: Math.max(12, Math.round(size * 0.14)) }}
        >
          {centerTop}
        </b>
        {centerSub !== undefined ? (
          <span className="text-muted" style={{ fontSize: Math.max(9, Math.round(size * 0.1)) }}>
            {centerSub}
          </span>
        ) : null}
      </div>
    </div>
  );
}
