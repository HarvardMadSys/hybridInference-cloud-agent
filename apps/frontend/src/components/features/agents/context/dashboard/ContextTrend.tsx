'use client';

// Context Trend: how each request's context was made up, request by request
// or turn by turn. Adapted from bowenliang123/dsh-context `trendChart.tsx`,
// `trendOverview.tsx`, `trendDurationCurve.tsx` and `trendDnaBars.tsx`
// (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001): Step/Turn,
// Total/Delta, Adaptive (the y-scale fits the visible window), Duration (a
// curve over the columns), DNA (each request as the items it carried, keyed
// by identity) and an overview with a draggable window for long histories.
//
// Two kinds of number are drawn and labelled apart: the stacked areas are the
// recorded content before each request (estimate); the line on top is the
// observed input the provider reported (exact). A request whose profile kept
// no categories breaks the areas instead of drawing zero. Duration is the
// Gateway's measured latency where a request joins one by its response id,
// otherwise the event-derived step time, each drawn in its own style.

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react';
import { useLocale } from '@/components/providers';
import type { ContextPointApi } from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import { formatDuration } from '../../TurnFooter';
import { signedTokens } from '../contextPresent';
import { DashCard, Toggle } from './DashCard';
import { labelOf, swatchOf } from './palette';
import {
  clampWindow,
  columnOf,
  localOf,
  stepColumn,
  dnaDelta,
  deltaColumns,
  initialWindow,
  lineRuns,
  maxima,
  stackedRuns,
  trendColumns,
  type DnaItems,
  type Granularity,
  type Maxima,
  type Mode,
  type TrendColumn,
  MIN_WINDOW,
  windowAround,
  zoomWindow,
} from './trendModel';
import { DNA_MAX_COLUMNS, FOLDED_KEY, useDnaBands } from './useDnaBands';

const H = 168;
const PAD_TOP = 8;
const OVERVIEW_H = 30;

type Overlay = 'none' | 'duration';
type View = 'areas' | 'dna';

interface TrendState {
  granularity: Granularity;
  mode: Mode;
  adaptive: boolean;
  overlay: Overlay;
  view: View;
}

function useWidth(ref: React.RefObject<HTMLDivElement>): number {
  const [width, setWidth] = useState(600);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === 'undefined') return undefined;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(240, entry.contentRect.width)),
    );
    observer.observe(element);
    return () => observer.disconnect();
  }, [ref]);
  return width;
}

export function ContextTrend({
  jobId,
  conversationKey,
  points,
  keys,
  latency,
  selected,
  onSelect,
  footer,
}: {
  jobId: string;
  conversationKey: string;
  points: ContextPointApi[];
  keys: string[];
  latency: ReadonlyMap<string, number>;
  selected: number | null;
  onSelect: (index: number) => void;
  /** The selected request's strip, drawn inside the card under the chart. */
  footer?: React.ReactNode;
}) {
  const { t, locale } = useLocale();
  const [state, setState] = useState<TrendState>({
    granularity: 'step',
    mode: 'total',
    adaptive: false,
    overlay: 'none',
    view: 'areas',
  });
  const update = (patch: Partial<TrendState>) => setState((current) => ({ ...current, ...patch }));
  const [hover, setHover] = useState<number | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const width = useWidth(plotRef);
  const columns = useMemo(() => {
    const base = trendColumns(points, state.granularity, latency);
    return state.mode === 'delta' ? deltaColumns(base) : base;
  }, [points, state.granularity, latency, state.mode]);
  const n = columns.length;
  const [win, setWin] = useState(() => initialWindow(n));
  // A new conversation, a new granularity or new requests: show the latest.
  useEffect(() => setWin(initialWindow(n)), [n, conversationKey, state.granularity]);
  // A request picked elsewhere (the browser, an event, the keys) is brought into
  // view. By turn a column stands for the turn's last request, so a request
  // inside that turn resolves to its containing column; the exact request
  // stays what the browser and the strip show.
  const selectedAt = columnOf(columns, points, selected);
  useEffect(() => {
    setWin((current) => windowAround(current.from, current.to, selectedAt, n));
  }, [selectedAt, n]);
  const { from, to } = clampWindow(win.from, win.to, n);
  const visible = columns.slice(from, to + 1);
  const scale = state.adaptive ? maxima(columns, from, to) : maxima(columns);
  const dnaAllowed = state.granularity === 'step' && visible.length <= DNA_MAX_COLUMNS;
  const dnaOn = state.view === 'dna' && dnaAllowed;
  const dna = useDnaBands(jobId, conversationKey, dnaOn ? visible.map((c) => c.index) : []);
  const selectedLocal = localOf(selectedAt, from, to);
  const shown = hover ?? (selectedLocal >= 0 ? selectedLocal : null);
  const onKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const next = stepColumn(selectedAt, n, event.key === 'ArrowRight' ? 1 : -1);
    // Nothing is drawn to move along; leave the key to the page.
    if (next < 0) return;
    event.preventDefault();
    onSelect(columns[next].index);
  };
  const controls = (
    <TrendControls
      state={state}
      update={update}
      dnaAllowed={dnaAllowed}
      hasDuration={scale.durationMs > 0}
    />
  );
  return (
    <DashCard title={t('dashboard.trend.title')} aside={controls}>
      <div
        ref={plotRef}
        tabIndex={0}
        role="group"
        aria-label={t('dashboard.trend.aria', { count: n })}
        onKeyDown={onKey}
        className="ui-focus-ring relative rounded"
      >
        {n === 0 ? (
          <p className="py-8 text-center text-xs text-muted">{t('context.empty')}</p>
        ) : null}
        {n > 0 ? (
          <TrendPlot
            visible={visible}
            keys={keys}
            width={width}
            scale={scale}
            delta={state.mode === 'delta'}
            overlay={state.overlay}
            dna={dnaOn ? dna.bands : null}
            shown={shown}
            onHover={setHover}
            onPick={(i) => onSelect(visible[i].index)}
          />
        ) : null}
        {shown !== null && visible[shown] ? (
          <TrendTip
            column={visible[shown]}
            keys={keys}
            delta={state.mode === 'delta'}
            granularity={state.granularity}
            overlay={state.overlay}
          />
        ) : null}
      </div>
      <Legend
        keys={keys}
        missing={visible.filter((c) => !c.cats).length}
        dna={dnaOn}
        dnaLoading={dna.loading}
        folded={dnaOn && [...dna.bands.values()].some((items) => items[0]?.[0] === FOLDED_KEY)}
        overlay={state.overlay}
      />
      {n > 0 ? (
        <Overview
          columns={columns.map((c) => c.observed)}
          from={from}
          to={to}
          onWindow={(a, b) => setWin(clampWindow(a, b, n))}
          onZoom={(zoomIn) => setWin(zoomWindow(from, to, n, zoomIn))}
          label={t('dashboard.trend.window', {
            from: (from + 1).toLocaleString(locale),
            to: (to + 1).toLocaleString(locale),
            count: n.toLocaleString(locale),
          })}
        />
      ) : null}
      <p className="mt-1 text-[11px] text-muted">{t('dashboard.trend.note')}</p>
      {footer}
    </DashCard>
  );
}

function TrendControls({
  state,
  update,
  dnaAllowed,
  hasDuration,
}: {
  state: TrendState;
  update: (patch: Partial<TrendState>) => void;
  dnaAllowed: boolean;
  hasDuration: boolean;
}) {
  const { t } = useLocale();
  const dnaTip = dnaAllowed
    ? t('dashboard.trend.dnaTip')
    : t('dashboard.trend.dnaDisabled', { max: DNA_MAX_COLUMNS });
  const adaptiveClass = state.adaptive
    ? 'bg-accent-subtle font-semibold text-accent'
    : 'bg-surface-muted text-muted';
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Toggle
        label={t('dashboard.trend.granularity')}
        value={state.granularity}
        onChange={(granularity) => update({ granularity })}
        options={[
          { value: 'step', label: t('dashboard.trend.step') },
          { value: 'turn', label: t('dashboard.trend.turn') },
        ]}
      />
      <Toggle
        label={t('dashboard.trend.mode')}
        value={state.mode}
        onChange={(mode) => update({ mode })}
        options={[
          { value: 'total', label: t('dashboard.trend.total') },
          { value: 'delta', label: t('dashboard.trend.delta') },
        ]}
      />
      <Toggle
        label={t('dashboard.trend.view')}
        value={state.view}
        onChange={(view) => update({ view })}
        options={[
          { value: 'areas', label: t('dashboard.trend.areas') },
          { value: 'dna', label: 'DNA', disabled: !dnaAllowed, tip: dnaTip },
        ]}
      />
      <Toggle
        label={t('dashboard.trend.extras')}
        value={state.overlay}
        onChange={(overlay) => update({ overlay })}
        options={[
          { value: 'none', label: t('dashboard.trend.noOverlay') },
          {
            value: 'duration',
            label: t('dashboard.trend.duration'),
            disabled: !hasDuration,
            tip: t('dashboard.trend.durationTip'),
          },
        ]}
      />
      <button
        type="button"
        aria-pressed={state.adaptive}
        onClick={() => update({ adaptive: !state.adaptive })}
        title={t('dashboard.trend.adaptiveTip')}
        className={`ui-focus-ring rounded-md px-2 py-0.5 text-[11px] [@media(pointer:coarse)]:min-h-11 ${adaptiveClass}`}
      >
        {t('dashboard.trend.adaptive')}
      </button>
    </div>
  );
}

function TrendPlot({
  visible,
  keys,
  width,
  scale,
  delta,
  overlay,
  dna,
  shown,
  onHover,
  onPick,
}: {
  visible: TrendColumn[];
  keys: string[];
  width: number;
  scale: Maxima;
  delta: boolean;
  overlay: Overlay;
  dna: ReadonlyMap<number, DnaItems> | null;
  shown: number | null;
  onHover: (i: number | null) => void;
  onPick: (i: number) => void;
}) {
  const span = delta ? Math.max(1, scale.up + scale.down) : Math.max(1, scale.total);
  const zeroY = delta ? PAD_TOP + ((H - PAD_TOP) * scale.up) / span : H;
  const y = (value: number) => zeroY - (value / span) * (H - PAD_TOP);
  const pitch = visible.length > 1 ? width / (visible.length - 1) : width;
  const x = (i: number) => (visible.length > 1 ? i * pitch : width / 2);
  const durationScale = Math.max(1, scale.durationMs);
  const dy = (ms: number) => H - (ms / durationScale) * (H - PAD_TOP);
  const pick = (clientX: number, rect: DOMRect) => {
    const ratio = (clientX - rect.left) / Math.max(1, rect.width);
    const i = Math.round(ratio * Math.max(0, visible.length - 1));
    return Math.max(0, Math.min(visible.length - 1, i));
  };
  let body: React.ReactNode;
  if (dna) {
    body = (
      <DnaColumns
        visible={visible.map((c) => c.index)}
        bands={dna}
        delta={delta}
        x={x}
        y={y}
        width={Math.max(2, Math.min(14, pitch * 0.8))}
      />
    );
  } else {
    body = stackedRuns(visible, keys, x, y).map((area) =>
      area.polygons.map((polygon, i) => (
        <polygon
          key={`${area.key}-${i}`}
          points={polygon}
          className={`${swatchOf(area.key).fill} opacity-80`}
        />
      )),
    );
  }
  let dots: React.ReactNode = null;
  if (visible.length <= 80) {
    dots = visible.map((column, i) => (
      <circle
        key={column.index}
        cx={x(i)}
        cy={y(column.observed)}
        r={1.8}
        className="fill-foreground"
      />
    ));
  }
  let durations: React.ReactNode = null;
  if (overlay === 'duration') {
    const latency = lineRuns(
      visible.map((c) => c.latencyMs),
      x,
      dy,
    );
    const steps = lineRuns(
      visible.map((c) => (c.latencyMs === null ? c.stepMs : null)),
      x,
      dy,
    );
    durations = (
      <>
        {latency.map((run, i) => (
          <polyline
            key={`l${i}`}
            points={run}
            className="fill-none stroke-amber-500"
            strokeWidth={1.5}
          />
        ))}
        {steps.map((run, i) => (
          <polyline
            key={`s${i}`}
            points={run}
            className="fill-none stroke-amber-500"
            strokeDasharray="4 3"
            strokeWidth={1.25}
          />
        ))}
      </>
    );
  }
  return (
    <svg
      width={width}
      height={H}
      className="block cursor-crosshair overflow-visible"
      aria-hidden="true"
      onPointerMove={(event: PointerEvent<SVGSVGElement>) =>
        onHover(pick(event.clientX, event.currentTarget.getBoundingClientRect()))
      }
      onPointerLeave={() => onHover(null)}
      onClick={(event) => onPick(pick(event.clientX, event.currentTarget.getBoundingClientRect()))}
    >
      {delta ? <line x1={0} x2={width} y1={zeroY} y2={zeroY} className="stroke-border" /> : null}
      {body}
      {lineRuns(
        visible.map((c) => c.observed),
        x,
        y,
      ).map((run, i) => (
        <polyline key={i} points={run} className="fill-none stroke-foreground" strokeWidth={1.25} />
      ))}
      {dots}
      {visible.map((column, i) =>
        column.afterCompaction ? <CompactionMark key={`c${column.index}`} x={x(i)} /> : null,
      )}
      {durations}
      {shown !== null ? (
        <line x1={x(shown)} x2={x(shown)} y1={0} y2={H} className="stroke-accent" strokeWidth={1} />
      ) : null}
    </svg>
  );
}

function CompactionMark({ x }: { x: number }) {
  return (
    <g>
      <line x1={x} x2={x} y1={PAD_TOP} y2={H} className="stroke-rose-400" strokeDasharray="3 3" />
      <path d={`M${x - 4},0 L${x + 4},0 L${x},6 Z`} className="fill-rose-400" />
    </g>
  );
}

function TrendTip({
  column,
  keys,
  delta,
  granularity,
  overlay,
}: {
  column: TrendColumn;
  keys: string[];
  delta: boolean;
  granularity: Granularity;
  overlay: Overlay;
}) {
  const { t } = useLocale();
  const sign = (v: number) => (delta ? signedTokens(v, compactCount) : compactCount(v));
  return (
    <div className="mt-1 rounded-md border border-border bg-surface-raised px-2 py-1 text-[11px] shadow-card">
      <p className="font-medium text-foreground">
        {granularity === 'turn'
          ? t('dashboard.trend.tipTurn', { turn: column.turnNo, requests: column.requests })
          : t('dashboard.trend.tipStep', { index: column.index + 1, turn: column.turnNo })}
        {' · '}
        {t('dashboard.trend.observed')} {sign(column.observed)}
      </p>
      {column.cats ? (
        <p className="text-muted">
          {keys
            .map((key, k) => ({ key, value: column.cats![k] }))
            .filter((part) => part.value !== 0)
            .map((part) => `${t(labelOf(part.key))} ≈${sign(part.value)}`)
            .join(' · ')}
        </p>
      ) : (
        <p className="text-muted">{t('dashboard.trend.noCategories')}</p>
      )}
      {overlay === 'duration' ? (
        <p className="text-muted">
          {column.latencyMs !== null
            ? t('dashboard.trend.latency', { ms: formatDuration(column.latencyMs) ?? '—' })
            : column.stepMs !== null
              ? t('dashboard.trend.stepTime', { ms: formatDuration(column.stepMs) ?? '—' })
              : t('dashboard.trend.noDuration')}
        </p>
      ) : null}
    </div>
  );
}

function Legend({
  keys,
  missing,
  dna,
  dnaLoading,
  folded,
  overlay,
}: {
  keys: string[];
  missing: number;
  dna: boolean;
  dnaLoading: boolean;
  folded: boolean;
  overlay: Overlay;
}) {
  const { t } = useLocale();
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
      {keys.map((key) => (
        <span key={key} className="inline-flex items-center gap-1">
          <span aria-hidden="true" className={`h-2 w-2 rounded-sm ${swatchOf(key).bg}`} />
          {t(labelOf(key))}
        </span>
      ))}
      <span className="inline-flex items-center gap-1">
        <span aria-hidden="true" className="h-0.5 w-3 bg-foreground" />
        {t('dashboard.trend.observedLegend')}
      </span>
      {overlay === 'duration' ? (
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true" className="h-0.5 w-3 bg-amber-500" />
          {t('dashboard.trend.durationLegend')}
        </span>
      ) : null}
      {folded ? (
        <span className="inline-flex items-center gap-1">
          <span aria-hidden="true" className={`h-2 w-2 rounded-sm ${swatchOf(FOLDED_KEY).bg}`} />
          {t('dashboard.cat.folded')}
        </span>
      ) : null}
      {missing > 0 ? <span>{t('dashboard.trend.missing', { count: missing })}</span> : null}
      {dna ? (
        <span>{t(dnaLoading ? 'dashboard.trend.dnaLoading' : 'dashboard.trend.dnaNote')}</span>
      ) : null}
    </div>
  );
}

function DnaColumns({
  visible,
  bands,
  delta,
  x,
  y,
  width,
}: {
  visible: number[];
  bands: ReadonlyMap<number, DnaItems>;
  delta: boolean;
  x: (i: number) => number;
  y: (value: number) => number;
  width: number;
}) {
  return (
    <>
      {visible.map((index, i) => {
        const items = bands.get(index);
        if (!items) return null;
        const previous = i > 0 ? (bands.get(visible[i - 1]) ?? null) : null;
        const stacks = dnaStacks(items, delta ? previous : null, delta);
        return stacks.map(({ items: stack, dir }) => {
          let acc = 0;
          return stack.map(([key, category, tokens]) => {
            const from = acc;
            acc += tokens * dir;
            const top = Math.min(y(from), y(acc));
            const height = Math.max(0.5, Math.abs(y(from) - y(acc)));
            return (
              <rect
                key={`${index}-${dir}-${key}`}
                x={x(i) - width / 2}
                y={top}
                width={width}
                height={height}
                className={`${swatchOf(category).fill} stroke-surface`}
                strokeWidth={0.3}
              />
            );
          });
        });
      })}
    </>
  );
}

function dnaStacks(
  items: DnaItems,
  previous: DnaItems | null,
  delta: boolean,
): { items: DnaItems; dir: 1 | -1 }[] {
  if (!delta) return [{ items, dir: 1 }];
  const change = dnaDelta(items, previous);
  return [
    { items: change.up, dir: 1 },
    { items: change.down, dir: -1 },
  ];
}

const OVERVIEW_BUTTON =
  'ui-focus-ring rounded px-1.5 hover:text-foreground disabled:opacity-40 [@media(pointer:coarse)]:min-h-11';

/** The whole history in miniature, with the shown window draggable over it. */
function Overview({
  columns,
  from,
  to,
  onWindow,
  onZoom,
  label,
}: {
  columns: number[];
  from: number;
  to: number;
  onWindow: (from: number, to: number) => void;
  onZoom: (zoomIn: boolean) => void;
  label: string;
}) {
  const { t } = useLocale();
  const ref = useRef<HTMLDivElement>(null);
  const drag = useRef<{ start: number; from: number } | null>(null);
  const n = columns.length;
  if (n < 2) return null;
  const max = Math.max(1, ...columns.map((v) => Math.abs(v)));
  const path = columns
    .map(
      (v, i) =>
        `${((i / (n - 1)) * 100).toFixed(3)},${(OVERVIEW_H - (Math.abs(v) / max) * OVERVIEW_H).toFixed(2)}`,
    )
    .join(' ');
  const left = (from / (n - 1)) * 100;
  const right = (to / (n - 1)) * 100;
  const size = to - from;
  const toIndex = (clientX: number) => {
    const rect = ref.current?.getBoundingClientRect();
    if (!rect || rect.width <= 0) return 0;
    return Math.round(((clientX - rect.left) / rect.width) * (n - 1));
  };
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between gap-2 text-[11px] text-muted">
        <span>{label}</span>
        <span className="flex gap-1">
          <button
            type="button"
            className="ui-focus-ring rounded px-1.5 hover:text-foreground [@media(pointer:coarse)]:min-h-11"
            onClick={() =>
              onWindow(
                from - Math.max(1, Math.floor(size / 2)),
                to - Math.max(1, Math.floor(size / 2)),
              )
            }
          >
            ‹ {t('dashboard.trend.earlier')}
          </button>
          <button
            type="button"
            aria-label={t('dashboard.trend.zoomIn')}
            disabled={size + 1 <= MIN_WINDOW}
            className={OVERVIEW_BUTTON}
            onClick={() => onZoom(true)}
          >
            +
          </button>
          <button
            type="button"
            aria-label={t('dashboard.trend.zoomOut')}
            disabled={size + 1 >= n}
            className={OVERVIEW_BUTTON}
            onClick={() => onZoom(false)}
          >
            −
          </button>
          <button type="button" className={OVERVIEW_BUTTON} onClick={() => onWindow(0, n - 1)}>
            {t('dashboard.trend.all')}
          </button>
          <button
            type="button"
            className="ui-focus-ring rounded px-1.5 hover:text-foreground [@media(pointer:coarse)]:min-h-11"
            onClick={() =>
              onWindow(
                from + Math.max(1, Math.floor(size / 2)),
                to + Math.max(1, Math.floor(size / 2)),
              )
            }
          >
            {t('dashboard.trend.later')} ›
          </button>
        </span>
      </div>
      <div
        ref={ref}
        className="relative mt-0.5 h-[30px] touch-none select-none rounded bg-surface-muted"
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          const at = toIndex(event.clientX);
          if (at < from || at > to) onWindow(at - Math.floor(size / 2), at + Math.ceil(size / 2));
          drag.current = {
            start: at,
            from: at < from || at > to ? at - Math.floor(size / 2) : from,
          };
        }}
        onPointerMove={(event) => {
          if (!drag.current) return;
          const moved = toIndex(event.clientX) - drag.current.start;
          onWindow(drag.current.from + moved, drag.current.from + moved + size);
        }}
        onPointerUp={() => {
          drag.current = null;
        }}
      >
        <svg
          viewBox={`0 0 100 ${OVERVIEW_H}`}
          preserveAspectRatio="none"
          className="absolute inset-0 h-full w-full"
          aria-hidden="true"
        >
          <polygon
            points={`0,${OVERVIEW_H} ${path} 100,${OVERVIEW_H}`}
            className="fill-slate-400 opacity-60"
          />
        </svg>
        <div
          className="absolute inset-y-0 rounded border-2 border-accent bg-accent/10"
          style={{ left: `${left}%`, width: `${Math.max(1, right - left)}%` }}
        />
      </div>
    </div>
  );
}
