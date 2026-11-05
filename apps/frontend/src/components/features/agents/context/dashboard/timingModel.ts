// The Timing Stats strip's geometry. Adapted from bowenliang123/dsh-context
// `src/client/components/timingStrip.tsx` (Apache-2.0, commit
// 32982aa6aa2c70fbda6b81281de98a72ea625001): segments laid end to end by
// duration (gaps between executions are not drawn), a binary search for the
// band under the pointer, and past `TIMING_DOM_MAX` one CSS gradient instead
// of an element per band.

import type { ThreadTimingApi, TimingKind } from '@/lib/api/agents';

export const TIMING_DOM_MAX = 600;

export interface StripBand {
  kind: TimingKind;
  leftPct: number;
  widthPct: number;
  start: number;
  ms: number;
}

export function stripBands(segments: ThreadTimingApi['segments']): {
  bands: StripBand[];
  total: number;
} {
  const valid = segments.filter(
    (s) => Number.isFinite(s.start_ms) && Number.isFinite(s.end_ms) && s.end_ms > s.start_ms,
  );
  let total = 0;
  for (const s of valid) total += s.end_ms - s.start_ms;
  if (total <= 0) return { bands: [], total: 0 };
  let cursor = 0;
  const bands = valid.map((s) => {
    const ms = s.end_ms - s.start_ms;
    const band = {
      kind: s.kind,
      leftPct: (cursor / total) * 100,
      widthPct: (ms / total) * 100,
      start: s.start_ms,
      ms,
    };
    cursor += ms;
    return band;
  });
  return { bands, total };
}

/** The band under `pct` (0–100), by binary search over sorted bands. */
export function bandAt(bands: readonly { leftPct: number }[], pct: number): number {
  let lo = 0;
  let hi = bands.length - 1;
  let hit = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >>> 1;
    if (bands[mid].leftPct <= pct) {
      hit = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return hit;
}

/** CSS colours for the gradient (Tailwind classes cannot be interpolated into one). */
export const TIMING_CSS: Record<TimingKind, string> = {
  ttft: 'rgb(59 130 246)',
  reasoning: 'rgb(139 92 246)',
  text: 'rgb(236 72 153)',
  toolarg: 'rgb(245 158 11)',
  gen: 'rgb(232 121 249)',
  tools: 'rgb(20 184 166)',
  unsplit: 'rgb(100 116 139)',
  other: 'rgb(203 213 225)',
};

export function stripGradient(bands: readonly StripBand[], only: TimingKind | null): string {
  const runs: { color: string; start: number; end: number }[] = [];
  for (const band of bands) {
    const color = only === null || band.kind === only ? TIMING_CSS[band.kind] : 'transparent';
    const end = band.leftPct + band.widthPct;
    const last = runs.at(-1);
    if (last && last.color === color) last.end = end;
    else runs.push({ color, start: band.leftPct, end });
  }
  return `linear-gradient(to right, ${runs
    .map((r) => `${r.color} ${r.start.toFixed(4)}% ${r.end.toFixed(4)}%`)
    .join(', ')})`;
}

/** The ring's order. */
export const TIMING_ORDER: TimingKind[] = [
  'ttft',
  'reasoning',
  'text',
  'toolarg',
  'gen',
  'tools',
  'unsplit',
  'other',
];
