'use client';

// Context Browser: what the selected request carried, category by category.
// Adapted from bowenliang123/dsh-context `src/client/components/browser.tsx`
// (Apache-2.0, commit 32982aa6aa2c70fbda6b81281de98a72ea625001): an accordion of
// categories with item counts, tokens and shares, their change since the
// previous step or turn, previous/next navigation, search and the item rows.
// Here the items are the platform's reconstruction from stored events
// (`…/thread/context/requests/{id}`): estimates, with the observed input beside
// them as the provider reported it. A change is counted only by item identity
// (the stored event, a turn's prompt). It follows the trend's selection; a
// reply for another request or conversation is dropped.

import { useEffect, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  getRequestComposition,
  type CompositionEntryApi,
  type ContextPointApi,
  type RequestCompositionApi,
} from '@/lib/api/agents';
import { compactCount } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from '../ContextSections';
import {
  BoundaryNote,
  EntryControls,
  EntryRow,
  Excluded,
  NativeSection,
  Note,
  ProviderInput,
} from '../RequestComposition';
import { filterEntries, type EntryOrder } from '../requestPresent';
import { DashCard, Toggle, shareText } from './DashCard';
import { labelOf, swatchOf } from './palette';

type Base = 'step' | 'turn';
type RebuiltApi = RequestCompositionApi['reconstructed'];

export function itemKey(entry: CompositionEntryApi): string {
  return entry.event_id !== null ? `e${entry.event_id}` : `p${entry.job_id}`;
}

/** The point a delta compares with: the previous request, or the previous turn's last. */
export function basePoint(
  points: ContextPointApi[],
  point: ContextPointApi,
  base: Base,
): ContextPointApi | null {
  const at = points.findIndex((p) => p.index === point.index);
  if (at <= 0) return null;
  if (base === 'step') return points[at - 1];
  for (let i = at - 1; i >= 0; i -= 1) if (points[i].turn_no < point.turn_no) return points[i];
  return null;
}

/** One composition per request event, cached per conversation; stale replies dropped. */
function useComposition(jobId: string, conversationKey: string, eventId: number | null) {
  const cache = useRef<{ key: string; map: Map<number, RequestCompositionApi | null> }>({
    key: conversationKey,
    map: new Map(),
  });
  if (cache.current.key !== conversationKey)
    cache.current = { key: conversationKey, map: new Map() };
  const [state, setState] = useState<{
    eventId: number;
    value: RequestCompositionApi | null;
    error: boolean;
  } | null>(null);
  useEffect(() => {
    if (eventId === null) return undefined;
    const store = cache.current;
    if (store.map.has(eventId)) {
      setState({ eventId, value: store.map.get(eventId) ?? null, error: false });
      return undefined;
    }
    const controller = new AbortController();
    getRequestComposition(jobId, eventId, controller.signal).then(
      (value) => {
        if (controller.signal.aborted || cache.current !== store) return;
        store.map.set(eventId, value);
        if (store.map.size > 64) store.map.delete(store.map.keys().next().value as number);
        setState({ eventId, value, error: false });
      },
      () => {
        if (!controller.signal.aborted) setState({ eventId, value: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, eventId, conversationKey]);
  if (eventId === null || !state || state.eventId !== eventId)
    return { value: null, loading: eventId !== null, error: false };
  return { value: state.value, loading: false, error: state.error };
}

export function ContextBrowser({
  jobId,
  conversationKey,
  points,
  selected,
  onSelect,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  conversationKey: string;
  points: ContextPointApi[];
  selected: number | null;
  onSelect: (index: number) => void;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const [base, setBase] = useState<Base>('step');
  const point = points.find((p) => p.index === selected) ?? points.at(-1) ?? null;
  const previous = point ? basePoint(points, point, base) : null;
  const current = useComposition(jobId, conversationKey, point?.event_id ?? null);
  const before = useComposition(jobId, conversationKey, previous?.event_id ?? null);
  const at = point ? points.findIndex((p) => p.index === point.index) : -1;
  const nav = (
    <BrowserNav
      base={base}
      onBase={setBase}
      onStep={(step) => onSelect(points[at + step].index)}
      canPrevious={at > 0}
      canNext={at >= 0 && at < points.length - 1}
    />
  );
  if (!point) {
    return (
      <DashCard title={t('dashboard.browser.title')}>
        <Note>{t('context.empty')}</Note>
      </DashCard>
    );
  }
  const composition = current.value;
  const title = t('dashboard.browser.request', {
    index: (point.index + 1).toLocaleString(locale),
    turn: point.turn_no,
  });
  return (
    <DashCard title={t('dashboard.browser.title')} aside={nav}>
      <p className="text-xs font-medium text-foreground">{title}</p>
      {current.loading ? <Note>{t('contextRequest.loading')}</Note> : null}
      {current.error ? <Note>{t('contextRequest.error')}</Note> : null}
      {point.event_id === null ? <Note>{t('contextRequest.noEvent')}</Note> : null}
      {composition ? (
        <Carried
          composition={composition}
          observed={point.prompt_tokens}
          baseEntries={before.value?.reconstructed.entries ?? null}
          jobId={jobId}
          onOpenPoint={onOpenPoint}
          onOpenTurn={onOpenTurn}
        />
      ) : null}
    </DashCard>
  );
}

/** The selected request's categories, as one bar, its DNA and the accordion. */
function Carried({
  composition,
  observed,
  baseEntries,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  composition: RequestCompositionApi;
  observed: number;
  baseEntries: CompositionEntryApi[] | null;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [order, setOrder] = useState<EntryOrder>('largest');
  const rebuilt = composition.reconstructed;
  const baseKeys = baseEntries ? new Set(baseEntries.map(itemKey)) : null;
  return (
    <>
      <p className="mt-1 text-[11px] tabular-nums text-muted">
        {t('dashboard.browser.totals', {
          observed: compactCount(observed),
          estimate: compactCount(rebuilt.estimated_tokens),
        })}
      </p>
      <div
        role="img"
        aria-label={t('dashboard.browser.bar')}
        className="mt-1 flex h-2.5 overflow-hidden rounded-full bg-surface-muted"
      >
        {rebuilt.categories.map((category) => (
          <span
            key={category.key}
            className={`h-full ${swatchOf(category.key).bg}`}
            style={{
              width: `${(category.tokens / Math.max(1, rebuilt.estimated_tokens)) * 100}%`,
            }}
          />
        ))}
      </div>
      <DnaStrip entries={rebuilt.entries} />
      <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-xs">
        {rebuilt.categories.map((category) => (
          <CategoryRow
            key={category.key}
            category={category}
            entries={rebuilt.entries}
            total={rebuilt.estimated_tokens}
            baseEntries={baseEntries}
            baseKeys={baseKeys}
            expanded={open === category.key}
            onToggle={() => setOpen(open === category.key ? null : category.key)}
            query={query}
            onQuery={setQuery}
            order={order}
            onOrder={setOrder}
            jobId={jobId}
            onOpenPoint={onOpenPoint}
            onOpenTurn={onOpenTurn}
          />
        ))}
        {['system_prompt', 'tool_schemas'].map((key) => (
          <li key={key} className="flex items-center justify-between gap-2 px-2 py-1.5 opacity-70">
            <span className="flex items-center gap-1.5">
              <span
                aria-hidden="true"
                className="h-2 w-2 shrink-0 rounded-full border border-dashed border-muted"
              />
              <span className="text-foreground">{t(labelOf(key))}</span>
            </span>
            <span className="text-muted">{t('dashboard.notRecorded')}</span>
          </li>
        ))}
      </ul>
      <BoundaryNote rebuilt={rebuilt} />
      {/* Where the numbers come from, and what was left out: on request. */}
      <details className="mt-2 text-xs">
        <summary className="ui-focus-ring cursor-pointer rounded text-muted [@media(pointer:coarse)]:min-h-11">
          {t('dashboard.browser.sources')}
        </summary>
        <ProviderInput request={composition.request} />
        <Note>{t('contextRequest.reconstructed.note')}</Note>
        {rebuilt.entries_omitted > 0 ? (
          <Note>{t('contextRequest.native.omitted', { count: rebuilt.entries_omitted })}</Note>
        ) : null}
        <Excluded rebuilt={rebuilt} />
        <NativeSection composition={composition} />
      </details>
    </>
  );
}

function BrowserNav({
  base,
  onBase,
  onStep,
  canPrevious,
  canNext,
}: {
  base: Base;
  onBase: (base: Base) => void;
  onStep: (step: 1 | -1) => void;
  canPrevious: boolean;
  canNext: boolean;
}) {
  const { t } = useLocale();
  const button =
    'ui-focus-ring rounded px-1.5 text-sm disabled:opacity-40 [@media(pointer:coarse)]:min-h-11';
  return (
    <div className="flex items-center gap-1.5">
      <Toggle
        label={t('dashboard.browser.base')}
        value={base}
        onChange={onBase}
        options={[
          { value: 'step', label: t('dashboard.trend.step') },
          { value: 'turn', label: t('dashboard.trend.turn') },
        ]}
      />
      <button
        type="button"
        aria-label={t('dashboard.browser.previous')}
        disabled={!canPrevious}
        onClick={() => onStep(-1)}
        className={button}
      >
        ‹
      </button>
      <button
        type="button"
        aria-label={t('dashboard.browser.next')}
        disabled={!canNext}
        onClick={() => onStep(1)}
        className={button}
      >
        ›
      </button>
    </div>
  );
}

function Change({ value, text }: { value: number | null; text: (value: number) => string }) {
  if (!value) return null;
  const sign = value > 0 ? '+' : '';
  return (
    <span className={`ml-1 ${value > 0 ? 'text-green-500' : 'text-rose-400'}`}>
      {`${sign}${text(value)}`}
    </span>
  );
}

type Category = RebuiltApi['categories'][number];

function CategoryRow({
  category,
  entries,
  total,
  baseEntries,
  baseKeys,
  expanded,
  onToggle,
  query,
  onQuery,
  order,
  onOrder,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  category: Category;
  entries: CompositionEntryApi[];
  total: number;
  baseEntries: CompositionEntryApi[] | null;
  baseKeys: ReadonlySet<string> | null;
  expanded: boolean;
  onToggle: () => void;
  query: string;
  onQuery: (query: string) => void;
  order: EntryOrder;
  onOrder: (order: EntryOrder) => void;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const own = entries.filter((entry) => entry.category === category.key);
  const baseCategory = baseEntries?.filter((entry) => entry.category === category.key) ?? null;
  const countDelta = baseCategory ? category.entries - baseCategory.length : null;
  const tokenDelta = baseCategory
    ? category.tokens - baseCategory.reduce((sum, entry) => sum + entry.tokens, 0)
    : null;
  const isNew = (entry: CompositionEntryApi) => baseKeys !== null && !baseKeys.has(itemKey(entry));
  const added = own.filter(isNew).length;
  const shown = expanded ? filterEntries(entries, category.key, query, order) : [];
  return (
    <li>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={onToggle}
        className="ui-focus-ring flex w-full items-center justify-between gap-2 px-2 py-1.5 text-left [@media(pointer:coarse)]:min-h-11"
      >
        <span className="flex min-w-0 items-center gap-1.5">
          <span aria-hidden="true" className="text-muted">
            {expanded ? '▾' : '▸'}
          </span>
          <span
            aria-hidden="true"
            className={`h-2 w-2 shrink-0 rounded-full ${swatchOf(category.key).bg}`}
          />
          <span className="truncate text-foreground">{t(labelOf(category.key))}</span>
          <span className="tabular-nums text-muted">
            {category.entries.toLocaleString(locale)}
            <Change value={countDelta} text={String} />
          </span>
        </span>
        <span className="shrink-0 tabular-nums text-foreground">
          ≈{compactCount(category.tokens)}
          <Change value={tokenDelta} text={compactCount} />
          {category.truncated_entries > 0 ? (
            <span className="ml-1 text-muted">
              {`${category.truncated_entries} ${t('contextRequest.entry.clipped')}`}
            </span>
          ) : null}
          <span className="ml-1.5 text-muted">{shareText(category.tokens, total)}</span>
        </span>
      </button>
      {expanded ? (
        <div className="border-t border-border px-2 pb-2">
          <EntryControls query={query} onQuery={onQuery} order={order} onOrder={onOrder} />
          {added > 0 ? (
            <p className="mt-1 text-[11px] text-muted">
              {t('dashboard.browser.added', { count: added })}
            </p>
          ) : null}
          {shown.length === 0 ? (
            <p className="mt-1 text-[11px] text-muted">{t('dashboard.browser.noMatch')}</p>
          ) : null}
          <ul className="mt-1 divide-y divide-border rounded border border-border">
            {shown.map((entry) => (
              <EntryRow
                key={itemKey(entry)}
                entry={entry}
                jobId={jobId}
                onOpenPoint={onOpenPoint}
                onOpenTurn={onOpenTurn}
                isNew={isNew(entry)}
              />
            ))}
          </ul>
        </div>
      ) : null}
    </li>
  );
}

/** The selected request's items in order, as one strip (its DNA). */
function DnaStrip({ entries }: { entries: CompositionEntryApi[] }) {
  const { t } = useLocale();
  const total = entries.reduce((sum, entry) => sum + entry.tokens, 0);
  if (total <= 0) return null;
  return (
    <div className="mt-1" title={t('dashboard.browser.dnaTip')}>
      <div
        role="img"
        aria-label={t('dashboard.browser.dna')}
        className="flex h-1.5 overflow-hidden rounded-full"
      >
        {entries.map((entry) => (
          <span
            key={itemKey(entry)}
            className={`h-full ${swatchOf(entry.category).bg}`}
            style={{ width: `${(entry.tokens / total) * 100}%` }}
          />
        ))}
      </div>
    </div>
  );
}
