'use client';

// Parts of the Context Browser (`dashboard/ContextBrowser.tsx`): the provider's
// input, the CLI's own account, and the reconstructed items. None of it is
// called the live window. Each stored item opens its trajectory step, or its
// turn, and expands into the text the platform stored.

import { useEffect, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  getEventContent,
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
  type EntryOrder,
  nativeCategories,
  nativeState,
  type BarSegment,
} from './requestPresent';

export function Note({ children }: { children: React.ReactNode }) {
  return <p className="mt-2 text-xs text-muted">{children}</p>;
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

/** The request's input as the provider reported it, and how it was billed. */
export function ProviderInput({ request }: { request: ContextPointApi }) {
  const { t } = useLocale();
  return (
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
  );
}

export function NativeSection({ composition }: { composition: RequestCompositionApi }) {
  const { t } = useLocale();
  const state = nativeState(composition);
  let body: React.ReactNode;
  if (state.kind === 'not_collected' || state.kind === 'not_settled' || state.kind === 'pending')
    body = <Note>{t(`contextRequest.native.${state.kind}`)}</Note>;
  else if (state.kind === 'not_offered')
    body = (
      <Note>
        {composition.runtime === 'pi'
          ? t('contextRequest.native.not_offered.pi')
          : t('contextRequest.native.not_offered', { runtime: composition.runtime ?? '—' })}
      </Note>
    );
  else if (state.kind === 'failed')
    body = <Note>{t('contextRequest.native.failed', { status: state.status })}</Note>;
  else
    body = (
      <NativeSnapshot
        snapshot={state.snapshot}
        turn={composition.request.turn_no}
        relation={composition.native_relation}
      />
    );
  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextRequest.native')}</h4>
      {body}
    </div>
  );
}

/** When the read was made relative to this request; it is never live occupancy. */
function readNote(
  t: ReturnType<typeof useLocale>['t'],
  turn: number,
  relation: RequestCompositionApi['native_relation'],
): string {
  const before = relation?.requests_before ?? 0;
  const read =
    before > 0
      ? t('contextRequest.native.turnStartLater', { turn, count: before })
      : t('contextRequest.native.turnStart', { turn });
  const compacted = relation?.compacted_since
    ? ` ${t('contextRequest.native.turnStartCompacted')}`
    : '';
  return `${read}${compacted} ${t('contextRequest.native.estimate')}`;
}

function NativeSnapshot({
  snapshot,
  turn,
  relation,
}: {
  snapshot: NativeSnapshotApi;
  turn: number;
  relation: RequestCompositionApi['native_relation'];
}) {
  const { t } = useLocale();
  const { sized, hidden } = nativeCategories(snapshot);
  const namesOnly = snapshot.token_basis === 'names_only';
  const servers = (snapshot.mcp_servers ?? [])
    .map((server) => (server.status ? `${server.name} (${server.status})` : server.name))
    .join(', ');
  return (
    <>
      <Note>
        {namesOnly ? t('contextRequest.native.namesOnly', { turn }) : readNote(t, turn, relation)}
      </Note>
      {namesOnly && servers ? (
        <Note>{t('contextRequest.native.mcpServers', { servers })}</Note>
      ) : null}
      {namesOnly ? null : <Bar segments={barSegments(sized)} label={t('contextRequest.native')} />}
      <ul className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        {sized.map((category) => (
          <CategoryRow
            key={category.key}
            name={category.key}
            value={
              namesOnly
                ? t('contextRequest.native.named', { count: category.entries ?? 0 })
                : compactCount(category.tokens)
            }
          />
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

function CategoryRow({ name, value }: { name: string; value: string }) {
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
      <span className="tabular-nums text-foreground">{value}</span>
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
              {entry.tokens === null ? null : (
                <span className="shrink-0 tabular-nums">{compactCount(entry.tokens)}</span>
              )}
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

/** Search the listed entries' names and previews, and choose their order. */
export function EntryControls({
  query,
  onQuery,
  order,
  onOrder,
}: {
  query: string;
  onQuery: (query: string) => void;
  order: EntryOrder;
  onOrder: (order: EntryOrder) => void;
}) {
  const { t } = useLocale();
  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
      <input
        type="search"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        aria-label={t('contextRequest.search')}
        placeholder={t('contextRequest.searchPlaceholder')}
        className="ui-input min-h-8 w-full text-xs sm:w-56 [@media(pointer:coarse)]:min-h-11"
      />
      <div role="group" aria-label={t('contextRequest.order')} className="flex gap-1">
        {(['largest', 'transcript'] as const).map((value) => (
          <FilterChip key={value} active={order === value} onClick={() => onOrder(value)}>
            {t(
              value === 'largest'
                ? 'contextRequest.order.largest'
                : 'contextRequest.order.transcript',
            )}
          </FilterChip>
        ))}
      </div>
    </div>
  );
}

export function BoundaryNote({ rebuilt }: { rebuilt: ReconstructedCompositionApi }) {
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

export function EntryRow({
  entry,
  jobId,
  onOpenPoint,
  onOpenTurn,
  isNew = false,
}: {
  entry: CompositionEntryApi;
  jobId: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
  /** Not carried by the request it is compared with (by item identity). */
  isNew?: boolean;
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
          {isNew ? (
            <span className="ml-1 rounded bg-accent-subtle px-1 text-[10px] text-accent">
              {t('dashboard.browser.new')}
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

export function Excluded({ rebuilt }: { rebuilt: ReconstructedCompositionApi }) {
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
