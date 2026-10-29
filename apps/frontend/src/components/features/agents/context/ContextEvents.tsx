'use client';

// The conversation's context events: what the platform or the CLI put into
// the context, what the CLI compacted or pruned, and where the model or the
// mode changed. Each one comes from a record; a size is shown only when the
// record carried one, and says whose number it is: the CLI's own count for a
// compaction, an estimate from characters for an injection. A token rise or
// drop with no record behind it is not an event, and is not listed here.

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  CONTEXT_EVENT_KINDS,
  type ContextEventApi,
  type ContextEventKind,
  type ThreadContextApi,
} from '@/lib/api/agents';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import { compactCount, relativeTime } from '@/components/features/settings/usagePresent';
import { Section, type OpenPoint } from './ContextSections';
import { eventSourceKey, filterEvents, supportReasonKey } from './contextEventsPresent';

/** Events drawn before "show all". */
const FIRST_EVENTS = 40;

const KIND_LABEL: Record<ContextEventKind, TranslationKey> = {
  injection: 'context.events.kind.injection',
  compaction: 'context.events.kind.compaction',
  prune: 'context.events.kind.prune',
  model_switch: 'context.events.kind.model_switch',
  mode: 'context.events.kind.mode',
};

/** One event's kind, as its row names it. */
const ONE_KIND: Record<ContextEventKind, TranslationKey> = {
  injection: 'context.events.one.injection',
  compaction: 'context.events.one.compaction',
  prune: 'context.events.one.prune',
  model_switch: 'context.events.one.model_switch',
  mode: 'context.events.one.mode',
};

const KIND_MARK: Record<ContextEventKind, string> = {
  injection: 'bg-yellow-500/70',
  compaction: 'bg-rose-400/80',
  prune: 'bg-orange-600/70',
  model_switch: 'bg-indigo-500/70',
  mode: 'bg-teal-500/70',
};

export function ContextEvents({
  view,
  nowMs,
  onOpenPoint,
}: {
  view: ThreadContextApi;
  nowMs: number;
  onOpenPoint: OpenPoint;
}) {
  const { t, locale } = useLocale();
  const [kind, setKind] = useState<ContextEventKind | null>(null);
  const [all, setAll] = useState(false);
  const events = view.events ?? [];
  const counts = view.event_counts ?? {};
  const shown = filterEvents(events, kind);
  const drawn = all ? shown : shown.slice(-FIRST_EVENTS);
  const total = CONTEXT_EVENT_KINDS.reduce((sum, key) => sum + (counts[key] ?? 0), 0);
  return (
    <Section title={t('context.events')}>
      <p className="mt-1 text-xs text-muted">{t('context.events.note')}</p>
      <div
        role="group"
        aria-label={t('context.events.filter')}
        className="mt-2 flex flex-wrap gap-1 text-xs"
      >
        <Chip active={kind === null} onClick={() => setKind(null)}>
          {t('context.events.all')} {total.toLocaleString(locale)}
        </Chip>
        {CONTEXT_EVENT_KINDS.map((key) => (
          <Chip key={key} active={kind === key} onClick={() => setKind(key)}>
            <span
              aria-hidden="true"
              className={`mr-1 inline-block h-2 w-2 rounded-full ${KIND_MARK[key]}`}
            />
            {t(KIND_LABEL[key])} {(counts[key] ?? 0).toLocaleString(locale)}
          </Chip>
        ))}
      </div>
      {shown.length === 0 ? (
        <p className="mt-2 rounded-lg border border-border px-3 py-2 text-xs text-muted">
          {t(kind ? 'context.events.noneOfKind' : 'context.events.none')}
        </p>
      ) : (
        <ol
          aria-label={t('context.events')}
          className="mt-2 divide-y divide-border rounded-lg border border-border text-xs"
        >
          {drawn.length < shown.length ? (
            <li className="px-3 py-1.5 text-muted">
              <button
                type="button"
                onClick={() => setAll(true)}
                className="ui-focus-ring rounded text-accent hover:underline"
              >
                {t('context.events.showEarlier', {
                  count: (shown.length - drawn.length).toLocaleString(locale),
                })}
              </button>
            </li>
          ) : null}
          {drawn.map((event) => (
            <EventRow
              key={`${event.kind}:${event.job_id}:${event.attempt_id ?? '-'}:${event.seq ?? '-'}:${event.source}`}
              event={event}
              nowMs={nowMs}
              onOpenPoint={onOpenPoint}
            />
          ))}
        </ol>
      )}
      {view.events_omitted > 0 ? (
        <p className="mt-1 text-xs text-muted">
          {t('context.events.omitted', { count: view.events_omitted.toLocaleString(locale) })}
        </p>
      ) : null}
      <EventCoverage view={view} />
    </Section>
  );
}

function Chip({
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
      className={`ui-focus-ring inline-flex items-center rounded-md border px-2 py-1 tabular-nums [@media(pointer:coarse)]:min-h-11 ${
        active
          ? 'border-accent bg-surface font-medium text-foreground'
          : 'border-border text-muted hover:bg-surface-hover'
      }`}
    >
      {children}
    </button>
  );
}

function EventRow({
  event,
  nowMs,
  onOpenPoint,
}: {
  event: ContextEventApi;
  nowMs: number;
  onOpenPoint: OpenPoint;
}) {
  const { t, locale } = useLocale();
  const source = eventSourceKey(event.source);
  // What the platform added at claim is no step of the trajectory: its turn opens.
  const step = event.source.startsWith('platform_') ? null : event.event_id;
  return (
    <li
      className={`flex flex-wrap items-start gap-2 px-3 py-2 ${event.superseded ? 'opacity-70' : ''}`}
    >
      <span
        aria-hidden="true"
        className={`mt-1 h-2 w-2 shrink-0 rounded-full ${KIND_MARK[event.kind]}`}
      />
      <div className="min-w-0 flex-1">
        <p className="text-foreground">
          <span className="font-medium">{t(ONE_KIND[event.kind])}</span>
          <span className="text-muted">
            {' · '}
            {source ? t(source) : event.source}
            {' · '}
            {t('context.compactionTurn', { turn: event.turn_no })}
            {event.at ? ` · ${relativeTime(event.at, nowMs, locale)}` : ''}
          </span>
        </p>
        {eventDetail(event, t).map((line) => (
          <p key={line} className="break-words tabular-nums text-muted">
            {line}
          </p>
        ))}
        {event.superseded ? <p className="text-muted">{t('context.events.superseded')}</p> : null}
        {event.copied ? <p className="text-muted">{t('context.events.copied')}</p> : null}
      </div>
      <button
        type="button"
        onClick={() => onOpenPoint({ jobId: event.job_id, turnNo: event.turn_no, eventId: step })}
        className="ui-button ui-button-ghost min-h-9 shrink-0 px-2.5 text-xs [@media(pointer:coarse)]:min-h-11"
      >
        {t(step === null ? 'context.openTurn' : 'context.openStep')}
      </button>
    </li>
  );
}

/** What one event's record says, in words; no line for what it does not carry. */
export function eventDetail(event: ContextEventApi, t: Translator): string[] {
  if (event.kind === 'compaction') return compactionDetail(event, t);
  if (event.kind === 'model_switch' || event.kind === 'mode') return [changeDetail(event, t)];
  return sizedDetail(event, t);
}

function compactionDetail(event: ContextEventApi, t: Translator): string[] {
  const lines = event.label ? [t('context.events.trigger', { trigger: event.label })] : [];
  const { before_tokens: before, after_tokens: after } = event;
  if (before !== null && after !== null) {
    lines.push(
      t('context.events.cliSizes', { before: compactCount(before), after: compactCount(after) }),
    );
  } else if (before !== null) {
    lines.push(t('context.events.cliBefore', { before: compactCount(before) }));
  } else {
    lines.push(t('context.events.noSizes'));
  }
  return lines;
}

function changeDetail(event: ContextEventApi, t: Translator): string {
  // A plan-mode tool call is its own sentence: "Entered plan mode".
  if (!event.from_value && event.to_value && MODE_LABEL[event.to_value]) {
    return modeLabel(event.to_value, t);
  }
  const to = event.to_value ? modeLabel(event.to_value, t) : '—';
  return event.from_value
    ? t('context.events.change', { from: modeLabel(event.from_value, t), to })
    : t('context.events.changeTo', { to });
}

/** An injection's or a prune's names and size, the size only as recorded. */
function sizedDetail(event: ContextEventApi, t: Translator): string[] {
  const lines = [event.label, event.names.join(', ')].filter((line): line is string =>
    Boolean(line),
  );
  if (event.items) lines.push(t('context.events.items', { count: event.items }));
  if (event.tokens !== null && event.tokens_basis === 'estimate') {
    const chars = (event.chars ?? 0).toLocaleString('en-US');
    lines.push(t('context.events.estimate', { tokens: compactCount(event.tokens), chars }));
  } else if (event.chars !== null) {
    lines.push(t('context.events.chars', { chars: event.chars.toLocaleString('en-US') }));
  } else if (event.kind === 'injection') {
    lines.push(t('context.events.unsized'));
  }
  return lines;
}

const MODE_LABEL: Record<string, TranslationKey> = {
  plan_entered: 'context.events.mode.plan_entered',
  plan_exit_requested: 'context.events.mode.plan_exit_requested',
};

function modeLabel(value: string, t: Translator): string {
  const key = MODE_LABEL[value];
  return key ? t(key) : value;
}

/** Each runtime of the conversation, and why a kind of event cannot be shown for it. */
function EventCoverage({ view }: { view: ThreadContextApi }) {
  const { t } = useLocale();
  const rows = (view.event_capabilities ?? []).filter((row) => row.support !== 'reported');
  if (rows.length === 0) return null;
  return (
    <details className="mt-2 text-xs">
      <summary className="ui-focus-ring cursor-pointer rounded text-accent">
        {t('context.events.coverage')}
      </summary>
      <ul className="mt-1 space-y-1 text-muted">
        {rows.map((row) => (
          <li key={`${row.runtime}:${row.kind}`}>
            <span className="text-foreground">
              {row.runtime} · {t(KIND_LABEL[row.kind])}
            </span>
            {': '}
            {t(
              row.support === 'partly_reported'
                ? 'context.events.support.partly'
                : 'context.events.support.not',
            )}
            {row.reason ? ` — ${t(supportReasonKey(row.reason))}` : ''}
          </li>
        ))}
      </ul>
    </details>
  );
}
