import { describe, expect, it } from 'vitest';

import type { ContextPointApi, SessionTokensApi, ThreadTimingApi } from '@/lib/api/agents';
import { translate, type TranslationKey } from '@/lib/i18n/translate';
import { basePoint, itemKey } from './ContextBrowser';
import { TOP_PAIR_PX, TWO_COLUMNS_PX, layoutFor } from './ContextDashboard';
import { shareText } from './DashCard';
import { centerFont, donutArcs } from './Donut';
import { timingRows } from './TimingStats';
import { providerValues } from './TokenStats';
import { coverageOf } from './WindowCard';
import { bandAt, stripBands, stripGradient } from './timingModel';
import { FOLDED_KEY, dnaRanges, withFolded } from './useDnaBands';
import {
  clampWindow,
  deltaColumns,
  dnaDelta,
  initialWindow,
  lineRuns,
  maxima,
  stackedRuns,
  trendColumns,
  columnOf,
  localOf,
  stepColumn,
  windowAround,
  restoreBrowsingWindow,
  savedRangeOf,
} from './trendModel';

function point(index: number, overrides: Partial<ContextPointApi> = {}): ContextPointApi {
  return {
    index,
    job_id: 'j',
    turn_no: 1,
    attempt_id: 1,
    seq: index + 1,
    event_id: 100 + index,
    at: null,
    prompt_tokens: 1000,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    uncached_input_tokens: 1000,
    output_tokens: 10,
    context_window: null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
    ...overrides,
  };
}

const t = (key: TranslationKey, vars?: Record<string, string | number>) =>
  translate('en', key, vars);

describe('donutArcs', () => {
  it('lays arcs from the top with a gap, skipping zero and invalid values', () => {
    const arcs = donutArcs([
      { key: 'a', stroke: 's', value: 75 },
      { key: 'zero', stroke: 's', value: 0 },
      { key: 'nan', stroke: 's', value: Number.NaN },
      { key: 'b', stroke: 's', value: 25 },
    ]);
    expect(arcs.map((arc) => arc.key)).toEqual(['a', 'b']);
    expect(arcs[0].len).toBeCloseTo(74.5);
    expect(arcs[0].offset).toBeCloseTo(124.75);
    expect(arcs[1].len).toBeCloseTo(24.5);
    expect(arcs[1].offset).toBeCloseTo(49.75);
  });

  it('draws one value as a closed ring, and nothing for no values', () => {
    expect(donutArcs([{ key: 'a', stroke: 's', value: 3 }])).toEqual([
      { key: 'a', stroke: 's', len: 100, offset: 125 },
    ]);
    expect(donutArcs([{ key: 'a', stroke: 's', value: 0 }])).toEqual([]);
  });
});

describe('centerFont', () => {
  it('shrinks a long centre value to stay inside the ring', () => {
    expect(centerFont('13m', 104)).toBe(15);
    expect(centerFont('≈146.5K', 104)).toBeLessThan(15);
    expect(centerFont('≈146.5K', 104) * 7 * 0.6).toBeLessThanOrEqual(104 * 0.58);
  });
});

describe('shareText', () => {
  it('never rounds a positive share to 0%', () => {
    expect(shareText(1, 1000)).toBe('<1%');
    expect(shareText(0, 1000)).toBe('0%');
    expect(shareText(5, 0)).toBe('0%');
    expect(shareText(333, 1000)).toBe('33%');
  });
});

describe('coverageOf', () => {
  const keys = ['user', 'tool_results'];

  it('hatches what no record explains, without sizing it', () => {
    const coverage = coverageOf(keys, [100, 300], 1000)!;
    expect(coverage.estimate).toBe(400);
    expect(coverage.exceeds).toBe(false);
    expect(coverage.parts.map((part) => part.widthPct)).toEqual([10, 30]);
    expect(coverage.uncoveredPct).toBeCloseTo(60);
  });

  it('scales to the estimate when it exceeds the observed input, with no hatch', () => {
    const coverage = coverageOf(keys, [600, 600], 1000)!;
    expect(coverage.exceeds).toBe(true);
    expect(coverage.uncoveredPct).toBe(0);
    expect(coverage.parts.map((part) => part.widthPct)).toEqual([50, 50]);
  });

  it('has no track for unknown or mismatched categories', () => {
    expect(coverageOf(keys, null, 1000)).toBeNull();
    expect(coverageOf(keys, [1], 1000)).toBeNull();
    expect(coverageOf(keys, [0, 0], 1000)).toBeNull();
  });
});

describe('trend model', () => {
  const points = [
    point(0, { turn_no: 1, prompt_tokens: 100, categories: [10, 20], usage_id: 'r0' }),
    point(1, { turn_no: 1, prompt_tokens: 300, categories: [30, 40], step_ms: 500 }),
    point(2, { turn_no: 2, prompt_tokens: 200, categories: null, after_compaction: true }),
    point(3, { turn_no: 2, prompt_tokens: 250, categories: [5, 60], usage_id: 'r3' }),
  ];
  const latency = new Map([
    ['r0', 1200],
    ['r3', 900],
  ]);

  it('takes latency only by response id, and step time otherwise', () => {
    const columns = trendColumns(points, 'step', latency);
    expect(columns.map((c) => c.latencyMs)).toEqual([1200, null, null, 900]);
    expect(columns.map((c) => c.stepMs)).toEqual([null, 500, null, null]);
  });

  it('stands a turn for its last request, summing time only when all of it is known', () => {
    const turns = trendColumns(points, 'turn', latency);
    expect(turns.map((c) => [c.index, c.observed, c.requests])).toEqual([
      [1, 300, 2],
      [3, 250, 2],
    ]);
    expect(turns[1].afterCompaction).toBe(true);
    expect(turns[0].latencyMs).toBeNull();
    expect(turns[0].cats).toEqual([30, 40]);
  });

  it('changes are unknown where either side is', () => {
    const delta = deltaColumns(trendColumns(points, 'step', latency));
    expect(delta.map((c) => c.observed)).toEqual([0, 200, -100, 50]);
    expect(delta.map((c) => c.cats)).toEqual([null, [20, 20], null, null]);
  });

  it('scales to the visible window when adaptive', () => {
    const columns = trendColumns(points, 'step', latency);
    expect(maxima(columns).total).toBe(300);
    expect(maxima(columns, 2, 3).total).toBe(250);
    expect(maxima(columns).durationMs).toBe(1200);
    const delta = maxima(deltaColumns(columns));
    expect(delta.up).toBe(200);
    expect(delta.down).toBe(100);
  });

  it('opens on the latest columns and keeps a moved window in range', () => {
    expect(initialWindow(10, 4)).toEqual({ from: 6, to: 9 });
    expect(initialWindow(3, 4)).toEqual({ from: 0, to: 2 });
    expect(clampWindow(8, 12, 10)).toEqual({ from: 5, to: 9 });
    expect(clampWindow(-3, 1, 10)).toEqual({ from: 0, to: 4 });
  });

  it('remembers the browsing range, and puts it back after the whole history', () => {
    const steps = trendColumns(
      Array.from({ length: 130 }, (_, i) => point(i, { turn_no: 1 + Math.floor(i / 10) })),
      'step',
      new Map(),
    );
    const saves = savedRangeOf(steps, 10, 129, 'step')!;
    expect(saves).toMatchObject({ fromIndex: 10, toIndex: 129, fromTurn: 2, toTurn: 13 });
    expect(restoreBrowsingWindow(saves, steps, 'step')).toEqual({ from: 10, to: 129 });
    // Requests appended meanwhile stay outside the restored range.
    const grewPoints = Array.from({ length: 135 }, (_, i) =>
      point(i, { turn_no: 1 + Math.floor(i / 10) }),
    );
    const grown = trendColumns(grewPoints, 'step', new Map());
    expect(restoreBrowsingWindow(saves, grown, 'step')).toEqual({ from: 10, to: 129 });
    // A different granularity restores the turns the range covered.
    const turns = trendColumns(grewPoints, 'turn', new Map());
    expect(restoreBrowsingWindow(saves, turns, 'turn')).toEqual({ from: 1, to: 12 });
  });

  it('falls back to the latest columns when the saved range is gone', () => {
    const steps = trendColumns(
      Array.from({ length: 200 }, (_, i) => point(i, { turn_no: 1 + i })),
      'step',
      new Map(),
    );
    expect(restoreBrowsingWindow(null, steps, 'step')).toEqual(initialWindow(200));
    expect(restoreBrowsingWindow(savedRangeOf(steps, 0, 3, 'step'), [], 'step')).toEqual({
      from: 0,
      to: 0,
    });
    const kept = savedRangeOf(steps, 2, 4, 'step')!;
    // 195 retained columns, all after the saved range: the latest 120, not all.
    expect(restoreBrowsingWindow(kept, steps.slice(5), 'step')).toEqual(initialWindow(195));
    // Retained history entirely before it, and partially overlapping it.
    expect(restoreBrowsingWindow(kept, steps.slice(0, 2), 'step')).toEqual(initialWindow(2));
    expect(restoreBrowsingWindow(kept, steps.slice(3), 'step')).toEqual({ from: 0, to: 1 });
  });

  it('resolves a selected request to its own column, or its containing turn column', () => {
    const points = [
      { index: 0, turn_no: 1 },
      { index: 1, turn_no: 1 },
      { index: 2, turn_no: 2 },
      { index: 3, turn_no: 2 },
    ] as ContextPointApi[];
    const steps = trendColumns(points, 'step', new Map());
    const turns = trendColumns(points, 'turn', new Map());
    // Step: the exact request. Turn: the column of the turn containing it,
    // whose own index is that turn's last request.
    expect(columnOf(steps, points, 1)).toBe(1);
    expect(columnOf(turns, points, 1)).toBe(0);
    expect(columnOf(turns, points, 3)).toBe(1);
    expect(columnOf(turns, points, 2)).toBe(1);
    // Not in this chart, and nothing selected.
    expect(columnOf(turns, points, 9)).toBe(-1);
    expect(columnOf(turns, points, null)).toBe(-1);
  });

  it('moves one column with the arrow keys, and nowhere on an empty chart', () => {
    // From a selection, bounded at both ends.
    expect(stepColumn(3, 10, 1)).toBe(4);
    expect(stepColumn(3, 10, -1)).toBe(2);
    expect(stepColumn(0, 10, -1)).toBe(0);
    expect(stepColumn(9, 10, 1)).toBe(9);
    // No selection starts from the latest column.
    expect(stepColumn(-1, 10, 1)).toBe(9);
    expect(stepColumn(-1, 10, -1)).toBe(8);
    // Nothing drawn: no column to move to.
    expect(stepColumn(-1, 0, 1)).toBe(-1);
  });

  it('locates the shown column inside the window, or -1 outside it', () => {
    expect(localOf(12, 10, 20)).toBe(2);
    expect(localOf(10, 10, 20)).toBe(0);
    expect(localOf(9, 10, 20)).toBe(-1);
    expect(localOf(21, 10, 20)).toBe(-1);
    expect(localOf(-1, 10, 20)).toBe(-1);
  });

  it('moves the window just far enough to show a selection made elsewhere', () => {
    // Already shown: unchanged.
    expect(windowAround(10, 19, 15, 100)).toEqual({ from: 10, to: 19 });
    // Before it: the selection becomes its first column; after it: its last.
    expect(windowAround(10, 19, 3, 100)).toEqual({ from: 3, to: 12 });
    expect(windowAround(10, 19, 40, 100)).toEqual({ from: 31, to: 40 });
    // Never past either end, and a missing selection moves nothing.
    expect(windowAround(10, 19, 99, 100)).toEqual({ from: 90, to: 99 });
    expect(windowAround(10, 19, -1, 100)).toEqual({ from: 10, to: 19 });
  });

  it('breaks an area where a request has no recorded categories', () => {
    const columns = trendColumns(points, 'step', latency);
    const runs = stackedRuns(
      columns,
      ['user', 'tool_results'],
      (i) => i * 10,
      (v) => -v,
    );
    expect(runs[0].polygons).toHaveLength(2);
    // The second category stacks on the first.
    expect(runs[1].polygons[0].split(' ')[0]).toBe('0.00,-30.00');
    expect(
      lineRuns(
        [1, null, 2, 3],
        (i) => i,
        (v) => v,
      ),
    ).toEqual(['0.00,1.00', '2.00,2.00 3.00,3.00']);
  });

  it('counts a DNA change by item identity', () => {
    const before: [string, string, number][] = [
      ['e1', 'user', 10],
      ['e2', 'tool_results', 50],
    ];
    const after: [string, string, number][] = [
      ['e1', 'user', 10],
      ['e3', 'assistant', 20],
    ];
    expect(dnaDelta(after, before)).toEqual({
      up: [['e3', 'assistant', 20]],
      down: [['e2', 'tool_results', 50]],
    });
    expect(dnaDelta(after, null)).toEqual({ up: [], down: [] });
  });
});

describe('timing strip', () => {
  const segments: ThreadTimingApi['segments'] = [
    { kind: 'ttft', start_ms: 0, end_ms: 100 },
    { kind: 'gen', start_ms: 100, end_ms: 400 },
    { kind: 'tools', start_ms: 5000, end_ms: 5600 },
    { kind: 'other', start_ms: 7000, end_ms: 7000 },
  ];

  it('lays segments end to end without the gaps between them', () => {
    const { bands, total } = stripBands(segments);
    expect(total).toBe(1000);
    expect(bands.map((b) => [b.kind, b.leftPct, b.widthPct])).toEqual([
      ['ttft', 0, 10],
      ['gen', 10, 30],
      ['tools', 40, 60],
    ]);
    expect(bandAt(bands, 0)).toBe(0);
    expect(bandAt(bands, 39.9)).toBe(1);
    expect(bandAt(bands, 99)).toBe(2);
  });

  it('dims all but the focused kind in the gradient', () => {
    const { bands } = stripBands(segments);
    const gradient = stripGradient(bands, 'gen');
    expect(gradient).toContain('transparent 0.0000% 10.0000%');
    expect(gradient).toContain('% 40.0000%');
    expect(gradient.match(/transparent/g)).toHaveLength(2);
  });
});

describe('timingRows', () => {
  function timing(overrides: Partial<ThreadTimingApi> = {}): ThreadTimingApi {
    return {
      active_ms: 1000,
      totals: {
        ttft: 0,
        reasoning: 0,
        text: 0,
        toolarg: 0,
        gen: 0,
        tools: 400,
        unsplit: 500,
        other: 100,
      },
      executions: 1,
      executions_split: 0,
      model_requests: 2,
      tool_calls: 1,
      tool_calls_unpaired: 0,
      blocks: {},
      output_tps: null,
      gateway: 'unavailable',
      gateway_reason: null,
      segments: [],
      segments_merged: false,
      requests: [],
      ...overrides,
    };
  }

  it('lists unmeasured splits as not recorded, never as 0', () => {
    const rows = timingRows(timing(), t);
    const ttft = rows.find((row) => row.key === 'ttft')!;
    expect(ttft.count).toBe('not recorded');
    expect(ttft.pct).toBeNull();
    expect(rows.find((row) => row.key === 'reasoning')?.count).toBe('not recorded');
    expect(rows.find((row) => row.key === 'gen')).toBeUndefined();
    expect(rows.find((row) => row.key === 'tools')?.pct).toBe('40%');
    expect(rows.find((row) => row.key === 'other')?.label).toBe('Not attributed');
  });

  it('shows measured splits with their shares', () => {
    const rows = timingRows(
      timing({
        executions_split: 1,
        blocks: { reasoning: 1 },
        totals: {
          ttft: 100,
          reasoning: 200,
          text: 0,
          toolarg: 0,
          gen: 100,
          tools: 400,
          unsplit: 0,
          other: 200,
        },
      }),
      t,
    );
    expect(rows.map((row) => row.key)).toEqual(['ttft', 'reasoning', 'gen', 'tools', 'other']);
    expect(rows[0].pct).toBe('10%');
  });
});

describe('providerValues', () => {
  it('splits input into cache reads, writes and the uncached rest', () => {
    const tokens = {
      provider: {
        input_tokens: 1000,
        cache_read_tokens: 700,
        cache_write_tokens: 100,
        output_tokens: 50,
      },
    } as SessionTokensApi;
    expect(providerValues(tokens)).toEqual({
      uncached: 200,
      cache_read: 700,
      cache_write: 100,
      output: 50,
    });
  });
});

describe('browser helpers', () => {
  it('keys an item by its stored event, or by its turn prompt', () => {
    const entry = { event_id: 7, job_id: 'j' } as Parameters<typeof itemKey>[0];
    expect(itemKey(entry)).toBe('e7');
    expect(itemKey({ ...entry, event_id: null })).toBe('pj');
  });

  it('compares with the previous request, or the previous turn’s last', () => {
    const points = [
      point(0, { turn_no: 1 }),
      point(1, { turn_no: 1 }),
      point(2, { turn_no: 2 }),
      point(3, { turn_no: 2 }),
    ];
    expect(basePoint(points, points[3], 'step')?.index).toBe(2);
    expect(basePoint(points, points[3], 'turn')?.index).toBe(1);
    expect(basePoint(points, points[1], 'turn')).toBeNull();
    expect(basePoint(points, points[0], 'step')).toBeNull();
  });
});

describe('layoutFor', () => {
  it('uses two columns at a wide panel, a top pair at ~850px sidebar width', () => {
    expect(layoutFor(1100)).toBe('wide');
    expect(layoutFor(TWO_COLUMNS_PX)).toBe('wide');
    // The side panel at ~850px leaves ~800px of content.
    expect(layoutFor(800)).toBe('wide');
    expect(layoutFor(TWO_COLUMNS_PX - 1)).toBe('medium');
    expect(layoutFor(TOP_PAIR_PX)).toBe('medium');
    expect(layoutFor(400)).toBe('narrow');
  });
});

describe('dnaRanges', () => {
  it('covers scattered missing requests in reads of at most 60', () => {
    expect(dnaRanges([])).toEqual([]);
    expect(dnaRanges([5, 3, 4])).toEqual([[3, 5]]);
    const missing = [...Array(11).keys(), ...Array.from({ length: 51 }, (_, i) => 50 + i)];
    const ranges = dnaRanges(missing);
    expect(ranges).toEqual([
      [0, 59],
      [60, 100],
    ]);
    for (const [from, to] of ranges) expect(to - from).toBeLessThan(60);
  });
});

describe('withFolded', () => {
  it('keeps a column its size: folded earliest items become one band at its base', () => {
    const request = {
      index: 4,
      event_id: 9,
      items: [['e1', 'user', 5]] as [string, string, number][],
    };
    expect(withFolded({ ...request, folded_items: 0, folded_tokens: 0 })).toEqual([
      ['e1', 'user', 5],
    ]);
    expect(withFolded({ ...request, folded_items: 300, folded_tokens: 4200 })).toEqual([
      [FOLDED_KEY, FOLDED_KEY, 4200],
      ['e1', 'user', 5],
    ]);
  });
});
