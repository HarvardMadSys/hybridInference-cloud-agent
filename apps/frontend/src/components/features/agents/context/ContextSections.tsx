'use client';

import { useLocale } from '@/components/providers';
import type {
  ContextChangeApi,
  ContextCompactionApi,
  ContextCompositionApi,
  ContextPointApi,
  ContextSupport,
  ContextTurnApi,
  RuntimeContextCapabilityApi,
  ThreadContextApi,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import {
  compactCount,
  formatShare,
  relativeTime,
} from '@/components/features/settings/usagePresent';
import { compositionSegments, signedTokens } from './contextPresent';

export type OpenPoint = (target: { jobId: string; turnNo: number; eventId: number | null }) => void;
export type OpenTurn = (jobId: string, turnNo: number) => void;

const SEGMENT_COLOR: Record<string, string> = {
  user: 'bg-sky-500/70',
  assistant: 'bg-emerald-500/70',
  reasoning: 'bg-violet-500/70',
  tool_calls: 'bg-amber-500/70',
  tool_results: 'bg-orange-600/70',
  unattributed: 'bg-muted/40',
};

const TRIGGER: Record<string, TranslationKey> = {
  auto: 'context.trigger.auto',
  manual: 'context.trigger.manual',
  threshold: 'context.trigger.threshold',
  overflow: 'context.trigger.overflow',
};

const SUPPORT: Record<ContextSupport | 'not_collected', TranslationKey> = {
  reported: 'context.support.reported',
  partly_reported: 'context.support.partly_reported',
  not_reported: 'context.support.not_reported',
  not_collected: 'context.support.not_collected',
};

export function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-6">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  );
}

function Fact({ label, value, detail }: { label: string; value: string; detail?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-border px-3 py-2">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-0.5 text-lg font-semibold tabular-nums text-foreground">{value}</dd>
      {detail ? <dd className="mt-0.5 text-xs text-muted">{detail}</dd> : null}
    </div>
  );
}

/** The headline: latest observed request, its window when reported, and what is not collected. */
export function ContextSummary({ view, nowMs }: { view: ThreadContextApi; nowMs: number }) {
  const { t, locale } = useLocale();
  const latest = view.latest;
  const point = latest?.point;
  const window = point?.context_window ?? null;
  const subagent = view.coverage.subagent_requests;
  // With nothing observed, a count of requests or compactions is unknown, not 0.
  const observed = view.coverage.attempts_observed > 0;
  const count = (value: number) => (observed ? value.toLocaleString(locale) : '—');
  let latestDetail: string | undefined;
  if (point) {
    const when = point.at ? relativeTime(point.at, nowMs, locale) : '—';
    latestDetail = t('context.latestDetail', { turn: point.turn_no, when });
  }
  let windowDetail = t('context.windowNotReported');
  if (window && latest?.window_share != null) {
    const share = formatShare(latest.window_share);
    windowDetail = t('context.windowShare', { share, window: compactCount(window) });
  }
  return (
    <>
      <dl className="mt-3 grid grid-cols-2 gap-2 lg:grid-cols-3">
        <Fact
          label={t('context.latest')}
          value={compactCount(point?.prompt_tokens)}
          detail={latestDetail}
        />
        <Fact
          label={t('context.window')}
          value={window ? compactCount(window) : '—'}
          detail={windowDetail}
        />
        <Fact
          label={t('context.live')}
          value={t('context.liveNotCollected')}
          detail={t('context.liveDetail')}
        />
        <Fact
          label={t('context.peak')}
          value={compactCount(view.peak?.prompt_tokens)}
          detail={view.peak ? t('context.peakDetail', { turn: view.peak.turn_no }) : undefined}
        />
        <Fact
          label={t('context.requests')}
          value={count(view.coverage.main_requests)}
          detail={
            subagent > 0
              ? t('context.requestsDetail', { count: subagent.toLocaleString(locale) })
              : undefined
          }
        />
        <Fact
          label={t('context.compactions')}
          value={count(view.coverage.compactions)}
          detail={t('context.compactionsDetail')}
        />
      </dl>
      <p className="mt-2 text-xs text-muted">{t('context.notLive')}</p>
      {latest?.compacted_after ? (
        <p
          role="note"
          className="mt-2 rounded-lg border border-border px-3 py-2 text-xs text-foreground"
        >
          {t('context.compactedAfter')}
        </p>
      ) : null}
      {latest?.turn_active ? (
        <p role="status" className="mt-2 text-xs text-muted">
          {t('context.turnActive')}
        </p>
      ) : null}
    </>
  );
}

/** The request picked on the chart, with what the CLI reported for it. */
export function PointDetail({
  point,
  onOpenPoint,
  onOpenTurn,
}: {
  point: ContextPointApi;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  const parts = [
    t('context.cacheSplit', {
      read: compactCount(point.cache_read_tokens),
      write: compactCount(point.cache_write_tokens),
      uncached: compactCount(point.uncached_input_tokens),
    }),
    t('context.output', { tokens: compactCount(point.output_tokens) }),
  ];
  if (point.model) parts.push(point.model);
  return (
    <div className="mt-3 flex flex-wrap items-start justify-between gap-2 rounded-lg border border-border px-3 py-2 text-xs">
      <div className="min-w-0">
        <p className="font-medium tabular-nums text-foreground">
          {t('context.point', {
            index: point.index + 1,
            turn: point.turn_no,
            tokens: compactCount(point.prompt_tokens),
          })}
        </p>
        <p className="tabular-nums text-muted">
          {point.delta_tokens === null
            ? null
            : t('context.delta', { delta: signedTokens(point.delta_tokens, compactCount) })}
          {point.after_compaction ? ` · ${t('context.change.afterCompaction')}` : ''}
        </p>
        <p className="tabular-nums text-muted">{parts.join(' · ')}</p>
      </div>
      <div className="flex shrink-0 gap-2">
        <button
          type="button"
          onClick={() =>
            onOpenPoint({ jobId: point.job_id, turnNo: point.turn_no, eventId: point.event_id })
          }
          className="ui-button ui-button-secondary min-h-9 px-2.5 text-xs [@media(pointer:coarse)]:min-h-11"
        >
          {t('context.openStep')}
        </button>
        <button
          type="button"
          onClick={() => onOpenTurn(point.job_id, point.turn_no)}
          className="ui-button ui-button-ghost min-h-9 px-2.5 text-xs [@media(pointer:coarse)]:min-h-11"
        >
          {t('context.openTurn')}
        </button>
      </div>
    </div>
  );
}

export function Composition({ composition }: { composition: ContextCompositionApi }) {
  const { t } = useLocale();
  const segments = compositionSegments(composition);
  return (
    <Section title={t('context.composition')}>
      <p className="mt-1 text-xs text-muted">
        {t(
          composition.basis === 'since_last_compaction'
            ? 'context.composition.since'
            : 'context.composition.whole',
        )}
        {composition.truncated ? ` ${t('context.composition.truncated')}` : ''}
      </p>
      <div
        aria-hidden="true"
        className="mt-2 flex h-3 overflow-hidden rounded-full bg-surface-muted"
      >
        {segments.map((segment) => (
          <span
            key={segment.key}
            className={SEGMENT_COLOR[segment.key]}
            style={{ width: `${segment.share * 100}%` }}
          />
        ))}
      </div>
      <ul className="mt-2 grid grid-cols-1 gap-x-4 gap-y-1 text-xs sm:grid-cols-2">
        {segments.map((segment) => (
          <li key={segment.key} className="flex items-center justify-between gap-2">
            <span className="flex min-w-0 items-center gap-1.5 text-muted">
              <span
                aria-hidden="true"
                className={`h-2 w-2 shrink-0 rounded-full ${SEGMENT_COLOR[segment.key]}`}
              />
              {t(`context.category.${segment.key}` as TranslationKey)}
            </span>
            <span className="tabular-nums text-foreground">
              ≈{compactCount(segment.tokens)} · {formatShare(segment.share)}
            </span>
          </li>
        ))}
      </ul>
      {composition.unattributed_tokens !== null ? (
        <p className="mt-1 text-xs text-muted">{t('context.unattributedDetail')}</p>
      ) : null}
    </Section>
  );
}

function compactionText(
  compaction: ContextCompactionApi,
  t: (key: TranslationKey, vars?: Record<string, string | number>) => string,
): string[] {
  const lines = [
    compaction.trigger && TRIGGER[compaction.trigger]
      ? t(TRIGGER[compaction.trigger])
      : t('context.trigger.unknown'),
  ];
  if (compaction.pre_tokens !== null && compaction.post_tokens !== null) {
    lines.push(
      t('context.compactionReported', {
        pre: compactCount(compaction.pre_tokens),
        post: compactCount(compaction.post_tokens),
      }),
    );
  } else if (compaction.pre_tokens !== null) {
    lines.push(t('context.compactionReportedPre', { pre: compactCount(compaction.pre_tokens) }));
  }
  if (compaction.observed_before !== null || compaction.observed_after !== null) {
    lines.push(
      t('context.compactionObserved', {
        before: compactCount(compaction.observed_before),
        after: compactCount(compaction.observed_after),
      }),
    );
  }
  if (compaction.completed === null) lines.push(t('context.compactionUnknownOutcome'));
  return lines;
}

export function Compactions({
  compactions,
  nowMs,
  onOpenPoint,
}: {
  compactions: ContextCompactionApi[];
  nowMs: number;
  onOpenPoint: OpenPoint;
}) {
  const { t, locale } = useLocale();
  if (compactions.length === 0) return null;
  return (
    <Section title={t('context.compactionList')}>
      <ul className="mt-2 divide-y divide-border rounded-lg border border-border text-xs">
        {compactions.map((compaction) => (
          <li
            key={`${compaction.attempt_id}:${compaction.seq}`}
            className="flex flex-wrap items-start justify-between gap-2 px-3 py-2"
          >
            <div className="min-w-0">
              <p className="font-medium text-foreground">
                {t('context.compactionTurn', { turn: compaction.turn_no })}
                {compaction.at ? ` · ${relativeTime(compaction.at, nowMs, locale)}` : ''}
              </p>
              {compactionText(compaction, t).map((line) => (
                <p key={line} className="tabular-nums text-muted">
                  {line}
                </p>
              ))}
            </div>
            <button
              type="button"
              onClick={() =>
                onOpenPoint({
                  jobId: compaction.job_id,
                  turnNo: compaction.turn_no,
                  eventId: compaction.event_id,
                })
              }
              className="ui-button ui-button-ghost min-h-9 shrink-0 px-2.5 text-xs [@media(pointer:coarse)]:min-h-11"
            >
              {t('context.openStep')}
            </button>
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function LargestChanges({
  changes,
  points,
  onSelect,
}: {
  changes: ContextChangeApi[];
  points: ContextPointApi[];
  onSelect: (index: number) => void;
}) {
  const { t } = useLocale();
  const byIndex = new Map(points.map((point) => [point.index, point]));
  const shown = changes.filter((change) => byIndex.has(change.to_index));
  if (shown.length === 0) return null;
  return (
    <Section title={t('context.changes')}>
      <ul className="mt-2 grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
        {shown.map((change) => {
          const point = byIndex.get(change.to_index) as ContextPointApi;
          const delta = compactCount(Math.abs(change.delta_tokens));
          return (
            <li key={`${change.direction}:${change.to_index}`}>
              <button
                type="button"
                onClick={() => onSelect(change.to_index)}
                className="ui-focus-ring flex w-full items-center justify-between gap-2 rounded-md border border-border px-2.5 py-1.5 text-left hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11"
              >
                <span className="tabular-nums font-medium text-foreground">
                  {t(
                    change.direction === 'growth' ? 'context.change.growth' : 'context.change.drop',
                    { delta },
                  )}
                </span>
                <span className="min-w-0 truncate text-muted">
                  {t('context.change.at', { index: change.to_index + 1, turn: point.turn_no })}
                  {change.direction === 'drop'
                    ? ` · ${t(change.compaction_between ? 'context.change.afterCompaction' : 'context.change.noCompaction')}`
                    : ''}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </Section>
  );
}

/** A turn's count, or a dash for a turn whose events recorded neither requests nor compactions. */
function turnCount(turn: ContextTurnApi, value: number): string {
  return turn.requests > 0 || turn.compactions > 0 ? String(value) : '—';
}

export function Turns({ turns, onOpenTurn }: { turns: ContextTurnApi[]; onOpenTurn: OpenTurn }) {
  const { t } = useLocale();
  if (turns.length === 0) return null;
  return (
    <Section title={t('context.turns')}>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[34rem] text-left text-xs">
          <thead className="text-muted">
            <tr className="border-b border-border">
              {(
                ['turn', 'model', 'requests', 'range', 'peak', 'change', 'compactions'] as const
              ).map((column) => (
                <th key={column} scope="col" className="py-1.5 pr-3 font-medium">
                  {t(`context.col.${column}` as TranslationKey)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="tabular-nums">
            {turns.map((turn) => (
              <tr key={turn.job_id} className="border-b border-border last:border-0">
                <th scope="row" className="py-1.5 pr-3 font-normal">
                  <button
                    type="button"
                    onClick={() => onOpenTurn(turn.job_id, turn.turn_no)}
                    className="ui-focus-ring rounded font-medium text-accent hover:underline"
                  >
                    #{turn.turn_no}
                  </button>
                  {turn.copied ? (
                    <span className="ml-1 text-muted">{t('context.turnCopied')}</span>
                  ) : null}
                </th>
                <td className="max-w-[10rem] truncate py-1.5 pr-3 text-muted">
                  {turn.model ?? '—'}
                </td>
                <td className="py-1.5 pr-3">{turnCount(turn, turn.requests)}</td>
                <td className="py-1.5 pr-3">
                  {turn.requests > 0
                    ? `${compactCount(turn.first_prompt_tokens)} → ${compactCount(turn.last_prompt_tokens)}`
                    : '—'}
                </td>
                <td className="py-1.5 pr-3">{compactCount(turn.peak_prompt_tokens)}</td>
                <td className="py-1.5 pr-3">{signedTokens(turn.change_tokens, compactCount)}</td>
                <td className="py-1.5">{turnCount(turn, turn.compactions)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

const CAPABILITY_ROWS: Array<
  [TranslationKey, (cap: RuntimeContextCapabilityApi) => ContextSupport | 'not_collected']
> = [
  ['context.cap.requestUsage', (cap) => cap.request_usage],
  ['context.cap.compactionEvents', (cap) => cap.compaction_events],
  ['context.cap.compactionSizes', (cap) => cap.compaction_sizes],
  ['context.cap.window', (cap) => cap.context_window],
  ['context.cap.live', (cap) => cap.live_occupancy],
];

/** What each runtime of this conversation can report, and how much of it is here. */
export function Capabilities({ view }: { view: ThreadContextApi }) {
  const { t, locale } = useLocale();
  const coverage = view.coverage;
  const notes: string[] = [
    t('context.coverage.observed', {
      observed: coverage.attempts_observed.toLocaleString(locale),
      attempts: coverage.attempts.toLocaleString(locale),
    }),
  ];
  if (coverage.points_omitted > 0)
    notes.push(
      t('context.coverage.omitted', { count: coverage.points_omitted.toLocaleString(locale) }),
    );
  if (coverage.unparsed_raw > 0)
    notes.push(
      t('context.coverage.unparsed', { count: coverage.unparsed_raw.toLocaleString(locale) }),
    );
  if (coverage.compactions_unknown_outcome > 0)
    notes.push(
      t('context.coverage.unknownOutcome', {
        count: coverage.compactions_unknown_outcome.toLocaleString(locale),
      }),
    );
  return (
    <Section title={t('context.capabilities')}>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[28rem] text-left text-xs">
          <thead className="text-muted">
            <tr className="border-b border-border">
              <th scope="col" className="py-1.5 pr-3 font-medium" />
              {view.capabilities.map((cap) => (
                <th
                  key={cap.runtime}
                  scope="col"
                  className="py-1.5 pr-3 font-medium text-foreground"
                >
                  {cap.runtime}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {CAPABILITY_ROWS.map(([label, read]) => (
              <tr key={label} className="border-b border-border last:border-0">
                <th scope="row" className="py-1.5 pr-3 font-normal text-muted">
                  {t(label)}
                </th>
                {view.capabilities.map((cap) => (
                  <td key={cap.runtime} className="py-1.5 pr-3 text-foreground">
                    {t(SUPPORT[read(cap)])}
                    {label === 'context.cap.window' && cap.context_window !== 'not_reported' ? (
                      <span className="block text-muted">
                        {t('context.windowCoverage', {
                          count: cap.window_reported_turns,
                          turns: cap.turns,
                        })}
                      </span>
                    ) : null}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="mt-2 space-y-0.5 text-xs text-muted">
        {notes.map((note) => (
          <li key={note}>{note}</li>
        ))}
      </ul>
    </Section>
  );
}
