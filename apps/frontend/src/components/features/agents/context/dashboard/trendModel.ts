// The Context Trend's data model. Adapted from bowenliang123/dsh-context
// `trendChart.tsx` (`aggregateByTurn`, `deltaOf`, `trendMaxima`), `trendWindow.ts`
// and `dna.ts` (`deltaBandsOf`) (Apache-2.0, commit
// 32982aa6aa2c70fbda6b81281de98a72ea625001). Inputs are ours: each request's
// recorded categories (estimate), its observed input (exact), the Gateway's
// latency joined by response id, and the event-derived step time.

import type { ContextPointApi, DnaBandsApi } from '@/lib/api/agents';

export type Granularity = 'step' | 'turn';
export type Mode = 'total' | 'delta';

export interface TrendColumn {
  /** The view index of the request this column stands for (a turn: its last). */
  index: number;
  turnNo: number;
  /** Recorded categories (estimate); null when unknown. */
  cats: number[] | null;
  /** Observed input (exact) or its change. */
  observed: number;
  afterCompaction: boolean;
  /** Gateway-measured latency of the request, ms, when joined by id. */
  latencyMs: number | null;
  /** Event-derived step time, ms. */
  stepMs: number | null;
  /** Requests a turn column stands for. */
  requests: number;
}

/** One column per request, or per turn (its last request; its steps summed for time). */
export function trendColumns(
  points: ContextPointApi[],
  granularity: Granularity,
  latency: ReadonlyMap<string, number>,
): TrendColumn[] {
  const step = points.map((point) => ({
    index: point.index,
    turnNo: point.turn_no,
    cats: point.categories ?? null,
    observed: point.prompt_tokens,
    afterCompaction: point.after_compaction,
    latencyMs: point.usage_id ? (latency.get(point.usage_id) ?? null) : null,
    stepMs: point.step_ms ?? null,
    requests: 1,
  }));
  if (granularity === 'step') return step;
  const turns: TrendColumn[] = [];
  for (const column of step) {
    const last = turns.at(-1);
    if (last && last.turnNo === column.turnNo) {
      turns[turns.length - 1] = {
        ...column,
        afterCompaction: last.afterCompaction || column.afterCompaction,
        latencyMs: sumKnown(last.latencyMs, column.latencyMs),
        stepMs: sumKnown(last.stepMs, column.stepMs),
        requests: last.requests + 1,
      };
    } else {
      turns.push(column);
    }
  }
  return turns;
}

function sumKnown(a: number | null, b: number | null): number | null {
  return a === null || b === null ? null : a + b;
}

/** Each column's change from the column before; unknown when either side is. */
export function deltaColumns(columns: TrendColumn[]): TrendColumn[] {
  return columns.map((column, i) => {
    const previous = i > 0 ? columns[i - 1] : null;
    return {
      ...column,
      observed: previous ? column.observed - previous.observed : 0,
      cats:
        previous && column.cats && previous.cats
          ? column.cats.map((value, k) => value - (previous.cats as number[])[k])
          : null,
    };
  });
}

export interface Maxima {
  total: number;
  up: number;
  down: number;
  durationMs: number;
}

/** The y-scale over `from`..`to` (inclusive): the visible window when adaptive. */
export function maxima(columns: TrendColumn[], from = 0, to = columns.length - 1): Maxima {
  const m: Maxima = { total: 1, up: 0, down: 0, durationMs: 0 };
  for (let i = Math.max(0, from); i <= Math.min(to, columns.length - 1); i += 1) {
    const column = columns[i];
    let up = Math.max(0, column.observed);
    let down = Math.max(0, -column.observed);
    let stacked = 0;
    if (column.cats) {
      let cu = 0;
      let cd = 0;
      for (const value of column.cats) {
        if (value > 0) cu += value;
        else cd -= value;
      }
      up = Math.max(up, cu);
      down = Math.max(down, cd);
      stacked = cu;
    }
    m.total = Math.max(m.total, column.observed, stacked);
    m.up = Math.max(m.up, up);
    m.down = Math.max(m.down, down);
    m.durationMs = Math.max(m.durationMs, column.latencyMs ?? 0, column.stepMs ?? 0);
  }
  return m;
}

/** The overview's window: the latest `size` columns by default. */
export const OVERVIEW_DEFAULT = 120;

export function initialWindow(n: number, size = OVERVIEW_DEFAULT): { from: number; to: number } {
  return n <= size ? { from: 0, to: Math.max(0, n - 1) } : { from: n - size, to: n - 1 };
}

export function clampWindow(from: number, to: number, n: number): { from: number; to: number } {
  const span = Math.max(1, Math.min(n, to - from + 1));
  const start = Math.max(0, Math.min(from, n - span));
  return { from: start, to: start + span - 1 };
}

/** Stacked areas: per category, the polygon points of each unbroken run. */
export function stackedRuns(
  columns: TrendColumn[],
  keys: string[],
  x: (i: number) => number,
  y: (value: number) => number,
): { key: string; polygons: string[] }[] {
  return keys.map((key, k) => {
    const polygons: string[] = [];
    let top: string[] = [];
    let bottom: string[] = [];
    const flush = () => {
      if (top.length > 0) polygons.push([...top, ...bottom.reverse()].join(' '));
      top = [];
      bottom = [];
    };
    columns.forEach((column, i) => {
      if (!column.cats) {
        flush();
        return;
      }
      const sign = (column.cats[k] ?? 0) >= 0 ? 1 : -1;
      let base = 0;
      for (let j = 0; j < k; j += 1) {
        const v = column.cats[j] ?? 0;
        if ((v >= 0 ? 1 : -1) === sign) base += v;
      }
      const value = column.cats[k] ?? 0;
      const cx = x(i).toFixed(2);
      top.push(`${cx},${y(base + value).toFixed(2)}`);
      bottom.push(`${cx},${y(base).toFixed(2)}`);
    });
    flush();
    return { key, polygons };
  });
}

/** A polyline per unbroken run of known values. */
export function lineRuns(
  values: (number | null)[],
  x: (i: number) => number,
  y: (value: number) => number,
): string[] {
  const runs: string[] = [];
  let run: string[] = [];
  values.forEach((value, i) => {
    if (value === null) {
      if (run.length) runs.push(run.join(' '));
      run = [];
      return;
    }
    run.push(`${x(i).toFixed(2)},${y(value).toFixed(2)}`);
  });
  if (run.length) runs.push(run.join(' '));
  return runs;
}

export type DnaItems = DnaBandsApi['requests'][number]['items'];

/** Items that grew (up) or shrank/left (down) since the previous request, by identity. */
export function dnaDelta(
  items: DnaItems,
  previous: DnaItems | null,
): { up: DnaItems; down: DnaItems } {
  if (!previous) return { up: [], down: [] };
  const before = new Map(previous.map(([key, , tokens]) => [key, tokens] as const));
  const after = new Map(items.map(([key, , tokens]) => [key, tokens] as const));
  const up: DnaItems = [];
  const down: DnaItems = [];
  for (const [key, category, tokens] of items) {
    const change = tokens - (before.get(key) ?? 0);
    if (change > 0) up.push([key, category, change]);
  }
  for (const [key, category, tokens] of previous) {
    const change = (after.get(key) ?? 0) - tokens;
    if (change < 0) down.push([key, category, -change]);
  }
  return { up, down };
}
