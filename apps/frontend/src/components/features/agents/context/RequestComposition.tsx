'use client';

// What the selected request carried. Follows the request picked on the growth
// chart; the newest observed request is the default, and none of it is called
// the live window. Each stored item opens its trajectory step, or its turn,
// and expands into the text the platform stored.

import { useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  getEventContent,
  getRequestComposition,
  type CompositionEntryApi,
  type ContextPointApi,
  type EventContentApi,
  type NativeSnapshotApi,
  type ReconstructedCompositionApi,
  type RequestCompositionApi,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import { compactCount } from '@/components/features/settings/usagePresent';
import type { OpenPoint, OpenTurn } from './ContextSections';
import {
  CATEGORY_COLOR,
  EXCLUDED_REASONS,
  barSegments,
  categoryLabel,
  filterEntries,
  nativeCategories,
  nativeState,
  type BarSegment,
} from './requestPresent';

export function RequestComposition({
  jobId,
  point,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  point: ContextPointApi;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const eventId = point.event_id;
  const [state, setState] = useState<{
    eventId: number;
    composition: RequestCompositionApi | null;
    error: boolean;
  } | null>(null);
  useEffect(() => {
    if (eventId === null) return undefined;
    const controller = new AbortController();
    getRequestComposition(jobId, eventId, controller.signal).then(
      (composition) => setState({ eventId, composition, error: false }),
      () => {
        if (!controller.signal.aborted) setState({ eventId, composition: null, error: true });
      },
    );
    return () => controller.abort();
  }, [jobId, eventId]);

  let body: React.ReactNode;
  if (eventId === null) body = <Note>{t('contextRequest.noEvent')}</Note>;
  else if (!state || state.eventId !== eventId) body = <Note>{t('contextRequest.loading')}</Note>;
  else if (state.error || !state.composition) body = <Note>{t('contextRequest.error')}</Note>;
  else
    body = (
      <CompositionBody
        composition={state.composition}
        jobId={jobId}
        onOpenPoint={onOpenPoint}
        onOpenTurn={onOpenTurn}
      />
    );
  return (
    <section className="mt-6" aria-labelledby="context-request-title">
      <h3 id="context-request-title" className="text-sm font-semibold text-foreground">
        {t('contextRequest.title')}
      </h3>
      {body}
    </section>
  );
}

function Note({ children }: { children: React.ReactNode }) {
  return <p className="mt-2 text-xs text-muted">{children}</p>;
}

function CompositionBody({
  composition,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  composition: RequestCompositionApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const request = composition.request;
  return (
    <>
      <div className="mt-2 rounded-lg border border-border px-3 py-2 text-xs">
        <p className="font-medium text-foreground">
          {t('contextRequest.provider')}: {compactCount(request.prompt_tokens)}
        </p>
        <p className="tabular-nums text-muted">
          {t('contextRequest.providerDetail', {
            read: compactCount(request.cache_read_tokens),
            write: compactCount(request.cache_write_tokens),
            uncached: compactCount(request.uncached_input_tokens),
          })}
        </p>
      </div>
      <NativeSection composition={composition} />
      <Reconstructed
        rebuilt={composition.reconstructed}
        jobId={jobId}
        onOpenPoint={onOpenPoint}
        onOpenTurn={onOpenTurn}
      />
    </>
  );
}

function Bar({ segments, label }: { segments: BarSegment[]; label: string }) {
  const { t } = useLocale();
  if (segments.length === 0) return null;
  return (
    <div
      role="img"
      aria-label={label}
      className="mt-2 flex h-3 overflow-hidden rounded-full bg-surface-muted"
    >
      {segments.map((segment) => (
        <span
          key={segment.key}
          title={`${t(categoryLabel(segment.key))} ≈${compactCount(segment.tokens)}`}
          className={CATEGORY_COLOR[segment.key] ?? CATEGORY_COLOR.other}
          style={{ width: `${segment.share * 100}%` }}
        />
      ))}
    </div>
  );
}

function NativeSection({ composition }: { composition: RequestCompositionApi }) {
  const { t } = useLocale();
  const state = nativeState(composition);
  let body: React.ReactNode;
  if (state.kind === 'not_collected')
    body = <Note>{t('contextRequest.native.not_collected')}</Note>;
  else if (state.kind === 'not_offered')
    body = (
      <Note>{t('contextRequest.native.not_offered', { runtime: composition.runtime ?? '—' })}</Note>
    );
  else if (state.kind === 'failed')
    body = <Note>{t('contextRequest.native.failed', { status: state.status })}</Note>;
  else
    body = (
      <NativeSnapshot
        snapshot={state.snapshot}
        partial={state.partial}
        turn={composition.request.turn_no}
        requestsAfter={composition.native_relation?.requests_after ?? 0}
      />
    );
  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextRequest.native')}</h4>
      {body}
    </div>
  );
}

function NativeSnapshot({
  snapshot,
  partial,
  turn,
  requestsAfter,
}: {
  snapshot: NativeSnapshotApi;
  partial: boolean;
  turn: number;
  requestsAfter: number;
}) {
  const { t } = useLocale();
  const { sized, hidden } = nativeCategories(snapshot);
  const meter = snapshot.token_basis === 'cli_meter';
  const relation =
    requestsAfter > 0
      ? t('contextRequest.native.later', { turn, count: requestsAfter })
      : t('contextRequest.native.after', { turn });
  const total = snapshot.total_tokens;
  return (
    <>
      <Note>
        {relation} {t(meter ? 'contextRequest.native.cli_meter' : 'contextRequest.native.estimate')}
      </Note>
      {typeof total === 'number' ? (
        <p className="mt-1 text-xs tabular-nums text-foreground">
          {t('contextRequest.native.total', { tokens: compactCount(total) })}
          {snapshot.context_window
            ? ` · ${t('contextRequest.native.window', { window: compactCount(snapshot.context_window) })}`
            : ''}
        </p>
      ) : null}
      {partial ? <Note>{t('contextRequest.native.partial')}</Note> : null}
      <Bar segments={barSegments(sized)} label={t('contextRequest.native')} />
      <ul className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        {sized.map((category) => (
          <CategoryRow key={category.key} name={category.key} tokens={category.tokens} />
        ))}
        {hidden.map((key) => (
          <li key={key} className="flex justify-between gap-2 text-muted">
            <span>{t(categoryLabel(key))}</span>
            <span>{t('contextRequest.native.notExposed')}</span>
          </li>
        ))}
      </ul>
      <NativeEntries snapshot={snapshot} />
    </>
  );
}

function CategoryRow({ name, tokens }: { name: string; tokens: number | null }) {
  const { t } = useLocale();
  return (
    <li className="flex items-center justify-between gap-2">
      <span className="flex min-w-0 items-center gap-1.5 text-muted">
        <span
          aria-hidden="true"
          className={`h-2 w-2 shrink-0 rounded-full ${CATEGORY_COLOR[name] ?? CATEGORY_COLOR.other}`}
        />
        {t(categoryLabel(name))}
      </span>
      <span className="tabular-nums text-foreground">{compactCount(tokens)}</span>
    </li>
  );
}

/** The CLI's named sources: files, tools, skills, messages; largest first. */
function NativeEntries({ snapshot }: { snapshot: NativeSnapshotApi }) {
  const { t } = useLocale();
  const entries = snapshot.entries ?? [];
  if (entries.length === 0) return null;
  return (
    <details className="mt-2 text-xs">
      <summary className="ui-focus-ring cursor-pointer rounded text-accent">
        {t('contextRequest.filter.all')} ({entries.length})
      </summary>
      <ul className="mt-1 max-h-80 divide-y divide-border overflow-y-auto rounded-lg border border-border">
        {entries.map((entry, index) => (
          <li key={`${entry.category}:${entry.label}:${index}`} className="px-2 py-1.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="min-w-0 truncate text-foreground" title={entry.label}>
                <span className="text-muted">{t(categoryLabel(entry.category))} · </span>
                {entry.label}
                {entry.source ? <span className="text-muted"> · {entry.source}</span> : null}
              </span>
              <span className="shrink-0 tabular-nums">{compactCount(entry.tokens)}</span>
            </div>
            {entry.preview ? <p className="truncate text-muted">{entry.preview}</p> : null}
          </li>
        ))}
      </ul>
      {snapshot.entries_omitted ? (
        <Note>{t('contextRequest.native.omitted', { count: snapshot.entries_omitted })}</Note>
      ) : null}
    </details>
  );
}

function Reconstructed({
  rebuilt,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  rebuilt: ReconstructedCompositionApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const [category, setCategory] = useState<string | null>(null);
  const entries = filterEntries(rebuilt.entries, category);
  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextRequest.reconstructed')}</h4>
      <Note>{t('contextRequest.reconstructed.note')}</Note>
      <BoundaryNote rebuilt={rebuilt} />
      <p className="mt-1 text-xs tabular-nums text-foreground">
        {t('contextRequest.reconstructed.total', {
          tokens: compactCount(rebuilt.estimated_tokens),
        })}
      </p>
      <Bar segments={barSegments(rebuilt.categories)} label={t('contextRequest.reconstructed')} />
      <div
        role="group"
        aria-label={t('contextRequest.filter')}
        className="mt-2 flex flex-wrap gap-1 text-xs"
      >
        <FilterChip active={category === null} onClick={() => setCategory(null)}>
          {t('contextRequest.filter.all')} {rebuilt.entries_total.toLocaleString(locale)}
        </FilterChip>
        {rebuilt.categories.map((item) => (
          <FilterChip
            key={item.key}
            active={category === item.key}
            onClick={() => setCategory(item.key)}
          >
            {t(categoryLabel(item.key))} ≈{compactCount(item.tokens)} · {item.entries}
          </FilterChip>
        ))}
      </div>
      <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-xs">
        {entries.map((entry) => (
          <EntryRow
            key={`${entry.event_id ?? `prompt-${entry.job_id}`}`}
            entry={entry}
            jobId={jobId}
            onOpenPoint={onOpenPoint}
            onOpenTurn={onOpenTurn}
          />
        ))}
      </ul>
      {rebuilt.entries_omitted > 0 ? (
        <Note>{t('contextRequest.native.omitted', { count: rebuilt.entries_omitted })}</Note>
      ) : null}
      <Excluded rebuilt={rebuilt} />
    </div>
  );
}

function BoundaryNote({ rebuilt }: { rebuilt: ReconstructedCompositionApi }) {
  const { t } = useLocale();
  const boundary = rebuilt.boundary;
  if (!boundary) return null;
  return (
    <Note>
      {t('contextRequest.reconstructed.boundary', { turn: boundary.turn_no })}
      {boundary.may_preserve_recent ? ` ${t('contextRequest.reconstructed.preserve')}` : ''}
    </Note>
  );
}

function FilterChip({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={`ui-focus-ring rounded-md border px-2 py-1 [@media(pointer:coarse)]:min-h-11 ${
        active
          ? 'border-accent bg-surface font-medium text-foreground'
          : 'border-border text-muted hover:bg-surface-hover'
      }`}
    >
      {children}
    </button>
  );
}

function EntryRow({
  entry,
  jobId,
  onOpenPoint,
  onOpenTurn,
}: {
  entry: CompositionEntryApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const label =
    entry.event_id === null
      ? t('contextRequest.entry.prompt', { turn: entry.turn_no })
      : entry.label;
  return (
    <li className="px-2 py-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="min-w-0 truncate text-foreground" title={label}>
          <span className="text-muted">{t(categoryLabel(entry.category))} · </span>
          {label}
          <span className="text-muted">
            {' '}
            · {t('contextRequest.entry.turn', { turn: entry.turn_no })}
          </span>
          {entry.truncated ? (
            <span className="ml-1 rounded border border-border px-1 text-[10px] text-muted">
              {t('contextRequest.entry.clipped')}
            </span>
          ) : null}
        </span>
        <span className="shrink-0 tabular-nums">≈{compactCount(entry.tokens)}</span>
      </div>
      <p className="truncate text-muted">{entry.preview}</p>
      <div className="mt-1 flex flex-wrap gap-2">
        {entry.event_id !== null ? (
          <button
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
            className="ui-focus-ring rounded text-accent hover:underline [@media(pointer:coarse)]:min-h-11"
          >
            {t(open ? 'contextRequest.entry.hide' : 'contextRequest.entry.show')}
          </button>
        ) : null}
        <button
          type="button"
          onClick={() =>
            entry.event_id === null
              ? onOpenTurn(entry.job_id, entry.turn_no)
              : onOpenPoint({ jobId: entry.job_id, turnNo: entry.turn_no, eventId: entry.event_id })
          }
          className="ui-focus-ring rounded text-accent hover:underline [@media(pointer:coarse)]:min-h-11"
        >
          {t(entry.event_id === null ? 'context.openTurn' : 'context.openStep')}
        </button>
      </div>
      {open && entry.event_id !== null ? (
        <EntryContent jobId={jobId} eventId={entry.event_id} />
      ) : null}
    </li>
  );
}

/** The stored text of one item, a window at a time. */
function EntryContent({ jobId, eventId }: { jobId: string; eventId: number }) {
  const { t, locale } = useLocale();
  const [pages, setPages] = useState<EventContentApi[]>([]);
  const [loading, setLoading] = useState(false);
  const [failed, setFailed] = useState(false);
  const next = pages.length === 0 ? 0 : pages[pages.length - 1].next_offset;
  const load = (offset: number) => {
    setLoading(true);
    getEventContent(jobId, eventId, offset).then(
      (page) => {
        setLoading(false);
        if (page) setPages((current) => [...current, page]);
        else setFailed(true);
      },
      () => {
        setLoading(false);
        setFailed(true);
      },
    );
  };
  useEffect(() => {
    load(0);
    // One load per opened item; later windows load on request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, eventId]);
  if (failed) return <Note>{t('contextRequest.error')}</Note>;
  const text = pages.map((page) => page.text).join('');
  const last = pages[pages.length - 1];
  return (
    <div className="mt-1">
      <pre className="max-h-72 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-surface-muted p-2 font-mono text-[11px] text-foreground">
        {text}
      </pre>
      {last ? (
        <p className="mt-0.5 text-[11px] text-muted">
          {t('contextRequest.entry.of', {
            shown: text.length.toLocaleString(locale),
            total: last.total_chars.toLocaleString(locale),
          })}
          {last.truncated ? ` · ${t('contextRequest.entry.storedClipped')}` : ''}
        </p>
      ) : null}
      {loading ? <Note>{t('contextRequest.entry.loading')}</Note> : null}
      {!loading && next !== null && pages.length > 0 ? (
        <button
          type="button"
          onClick={() => load(next)}
          className="ui-focus-ring mt-1 rounded text-accent hover:underline"
        >
          {t('contextRequest.entry.more')}
        </button>
      ) : null}
    </div>
  );
}

function Excluded({ rebuilt }: { rebuilt: ReconstructedCompositionApi }) {
  const { t, locale } = useLocale();
  const lines: string[] = [];
  for (const reason of EXCLUDED_REASONS) {
    const count = rebuilt.excluded[reason]?.entries ?? 0;
    if (count > 0) {
      lines.push(
        t(`contextRequest.excluded.${reason}` as TranslationKey, {
          count: count.toLocaleString(locale),
        }),
      );
    }
  }
  if (rebuilt.copied_turns > 0)
    lines.push(t('contextRequest.copied', { count: rebuilt.copied_turns }));
  if (rebuilt.events_capped) lines.push(t('contextRequest.capped'));
  if (lines.length === 0) return null;
  return (
    <div className="mt-2 text-xs text-muted">
      <p className="font-medium text-foreground">{t('contextRequest.excluded')}</p>
      <ul className="mt-0.5 space-y-0.5">
        {lines.map((line) => (
          <li key={line}>{line}</li>
        ))}
      </ul>
    </div>
  );
}
