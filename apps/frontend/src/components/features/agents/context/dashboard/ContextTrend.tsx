'use client';

// Context Trend: how each request's context was made up, request by request
// or turn by turn. Adapted from bowenliang123/dsh-context `trendChart.tsx`,
// `trendOverview.tsx`, `trendDurationCurve.tsx` and `trendDnaBars.tsx`
// (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001): Step/Turn,
// Total/Delta, Adaptive (the y-scale fits the visible window), Duration (a
// curve over the columns), DNA (each request as the items it carried, keyed
// by identity) and an Overview toggle that fits the whole conversation in the
// plot while keeping the browsing window for when it is turned off.
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
  restoreBrowsingWindow,
  savedRangeOf,
  type SavedRange,
  stackedRuns,
  trendColumns,
  type DnaItems,
  type Granularity,
  type Maxima,
  type Mode,
  type TrendColumn,
  windowAround,
} from './trendModel';
import { DNA_MAX_COLUMNS, FOLDED_KEY, useDnaBands } from './useDnaBands';

const H = 168;
const PAD_TOP = 8;

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

/**
 * The browsing window, and Overview as the whole conversation shown over it.
 * Overview remembers the columns that were being browsed and puts them back,
 * translated to the current granularity and to a request picked meanwhile.
 * Appending requests, changing the conversation and changing granularity each
 * have one consistent effect on what is saved.
 */
function useWindowView({
  columns,
  points,
  selected,
  conversationKey,
  granularity,
}: {
  columns: TrendColumn[];
  points: ContextPointApi[];
  selected: number | null;
  conversationKey: string;
  granularity: Granularity;
}) {
  const n = columns.length;
  const [win, setWin] = useState(() => initialWindow(n));
  const [overview, setOverview] = useState(false);
  // Effects that save or restore the window read these refs, so toggling
  // Overview itself never looks like an append, a pick or a granularity change.
  const overviewRef = useRef(overview);
  overviewRef.current = overview;
  const granularityRef = useRef(granularity);
  granularityRef.current = granularity;
  // The current columns, read when a pick moves the saved range. A rebuild of
  // equal columns must not rerun that effect.
  const columnsRef = useRef(columns);
  columnsRef.current = columns;
  const saved = useRef<SavedRange | null>(null);
  // A new conversation starts over, with no overview borrowed from the last one.
  useEffect(() => {
    saved.current = null;
    setWin(initialWindow(columns.length));
    setOverview(false);
    // `columns.length` is deliberately not a dependency: appending requests in a
    // conversation must not discard the window saved before Overview was on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationKey]);
  // New requests show the latest, unless Overview is holding the whole history.
  useEffect(() => {
    if (!overviewRef.current) setWin(initialWindow(n));
  }, [n]);
  // A new granularity starts from the latest columns of that basis.
  useEffect(() => {
    setWin(initialWindow(n));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [granularity]);
  // A request picked elsewhere (the browser, an event, the keys) is brought into
  // view. By turn a column stands for the turn's last request, so a request
  // inside that turn resolves to its containing column; the exact request
  // stays what the browser and the strip show. While Overview is on the whole
  // conversation is shown, so only the saved range moves, just as far as that
  // request would have needed: turning Overview off still shows it.
  //
  // What counts is the selected request's identity, not its column: changing
  // granularity remaps every column without anyone picking a request, so the
  // saved bounds are only translated, never moved.
  const selectedAt = columnOf(columns, points, selected);
  // The column depends on the granularity; the effect below runs on the request
  // identity and reads the column current at that moment.
  const selectedAtRef = useRef(selectedAt);
  selectedAtRef.current = selectedAt;
  const latestRequest = points.at(-1)?.index ?? null;
  const latestRequestRef = useRef(latestRequest);
  latestRequestRef.current = latestRequest;
  const lastPick = useRef({ selected, latest: latestRequest });
  useEffect(() => {
    const previous = lastPick.current;
    const latestAdvanced =
      previous.latest !== latestRequestRef.current &&
      previous.selected === previous.latest &&
      selected === latestRequestRef.current;
    lastPick.current = { selected, latest: latestRequestRef.current };
    if (overviewRef.current) {
      const at = selectedAtRef.current;
      if (latestAdvanced || selected === null || at < 0) return;
      const current = restoreBrowsingWindow(
        saved.current,
        columnsRef.current,
        granularityRef.current,
      );
      const around = windowAround(current.from, current.to, at, columnsRef.current.length);
      if (around.from !== current.from || around.to !== current.to)
        saved.current = savedRangeOf(
          columnsRef.current,
          around.from,
          around.to,
          granularityRef.current,
        );
      return;
    }
    setWin((current) =>
      windowAround(current.from, current.to, selectedAtRef.current, columnsRef.current.length),
    );
    // Only an actual selection change may move the window: a refresh that
    // rebuilds equal columns, a Total/Delta switch or a granularity change is
    // not a pick. Appends are filtered above as the default latest selection.
  }, [selected]);
  const browsing = clampWindow(win.from, win.to, n);
  const shown = overview ? { from: 0, to: Math.max(0, n - 1) } : browsing;
  const toggleOverview = (next: boolean) => {
    if (next === overviewRef.current) return;
    if (next) {
      saved.current = savedRangeOf(columns, browsing.from, browsing.to, granularity);
    } else {
      setWin(restoreBrowsingWindow(saved.current, columns, granularity));
      saved.current = null;
    }
    setOverview(next);
  };
  return { ...shown, overview, toggleOverview, selectedAt };
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
  const [hover, setHover] = useState<number | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);
  const width = useWidth(plotRef);
  const columns = useMemo(() => {
    const base = trendColumns(points, state.granularity, latency);
    return state.mode === 'delta' ? deltaColumns(base) : base;
  }, [points, state.granularity, latency, state.mode]);
  const n = columns.length;
  const { from, to, overview, toggleOverview, selectedAt } = useWindowView({
    columns,
    points,
    selected,
    conversationKey,
    granularity: state.granularity,
  });
  // These two only make sense over a window, so choosing either leaves Overview.
  const update = (patch: Partial<TrendState>) => {
    setState((current) => ({ ...current, ...patch }));
    if (patch.view === 'dna' || patch.adaptive === true) toggleOverview(false);
  };
  // The whole conversation replaces the windowed plot, so the window-only view
  // and scale are left first (as dsh-context's overview does); turning Overview
  // off restores the browsing range, not those two modes.
  const showOverview = (next: boolean) => {
    if (next) setState((current) => ({ ...current, view: 'areas', adaptive: false }));
    toggleOverview(next);
  };
  const visible = columns.slice(from, to + 1);
  const scale = state.adaptive ? maxima(columns, from, to) : maxima(columns);
  // DNA draws single requests inside the browsing window. Overview fits every
  // request in the plot, so it stays off there rather than drawing thousands of
  // bands or reading more of them.
  const dnaAllowed = !overview && state.granularity === 'step' && visible.length <= DNA_MAX_COLUMNS;
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
      overview={overview}
      hasPoints={n > 0}
      onOverview={showOverview}
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
      {n > 0 ? (
        <p className="mt-1 text-[11px] text-muted">
          {t('dashboard.trend.window', {
            from: (from + 1).toLocaleString(locale),
            to: (to + 1).toLocaleString(locale),
            count: n.toLocaleString(locale),
          })}
        </p>
      ) : null}
      <Legend
        keys={keys}
        missing={visible.filter((c) => !c.cats).length}
        dna={dnaOn}
        dnaLoading={dna.loading}
        folded={dnaOn && [...dna.bands.values()].some((items) => items[0]?.[0] === FOLDED_KEY)}
        overlay={state.overlay}
      />
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
  overview,
  hasPoints,
  onOverview,
}: {
  state: TrendState;
  update: (patch: Partial<TrendState>) => void;
  dnaAllowed: boolean;
  hasDuration: boolean;
  overview: boolean;
  hasPoints: boolean;
  onOverview: (overview: boolean) => void;
}) {
  const { t } = useLocale();
  const dnaTip = dnaAllowed
    ? t('dashboard.trend.dnaTip')
    : overview
      ? t('dashboard.trend.overviewTip')
      : t('dashboard.trend.dnaDisabled', { max: DNA_MAX_COLUMNS });
  const onClass = 'bg-accent-subtle font-semibold text-accent';
  const offClass = 'bg-surface-muted text-muted';
  const buttonClass = (on: boolean) =>
    `ui-focus-ring rounded-md px-2 py-0.5 text-[11px] [@media(pointer:coarse)]:min-h-11 ${
      on ? onClass : offClass
    }`;
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
        aria-pressed={overview}
        disabled={!hasPoints}
        onClick={() => onOverview(!overview)}
        title={t('dashboard.trend.overviewTip')}
        className={`${buttonClass(overview)} disabled:cursor-not-allowed disabled:opacity-50`}
      >
        {t('dashboard.trend.overview')}
      </button>
      <button
        type="button"
        aria-pressed={state.adaptive}
        disabled={overview}
        onClick={() => update({ adaptive: !state.adaptive })}
        title={overview ? t('dashboard.trend.overviewTip') : t('dashboard.trend.adaptiveTip')}
        className={`${buttonClass(state.adaptive)} disabled:cursor-not-allowed disabled:opacity-50`}
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
