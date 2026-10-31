// How the Context events read: who produced each one, and why a runtime
// cannot show a kind of event. Pure, so the wording rules are tested apart.

import type {
  ContextCompactionApi,
  ContextEventApi,
  ContextEventKind,
  ContextPointApi,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';

/** Producers the server names, worded; anything else is shown as sent. */
const SOURCES = [
  'platform_attachments',
  'platform_memory',
  'platform_skills',
  'platform_ssh_servers',
  'platform_goal',
  'platform_plugins',
  'claude_user_line',
  'pi_custom_message',
  'server_synthetic_part',
  'kilo_prune',
  'opencode_prune',
  'cli_compact_boundary',
  'codex_context_compaction',
  'codex_thread_compacted',
  'pi_compaction_raw',
  'opencode_session_compacted',
  'turn_settings',
  'request_usage',
  'claude_init',
  'claude_plan_tool',
] as const;

export function eventSourceKey(source: string): TranslationKey | null {
  return (SOURCES as readonly string[]).includes(source)
    ? (`context.events.source.${source}` as TranslationKey)
    : null;
}

/** Reasons the server gives for a kind a runtime cannot (fully) show. */
const REASONS = [
  'since_this_release',
  'claude_plan_tools',
  'platform_only',
  'platform_fixed_mode',
  'no_native_record',
  'claude_microcompact_unrecorded',
  'opencode_prune_off',
  'kilo_payload_limit_only',
  'turn_settings',
  'per_request',
  'unknown_runtime',
] as const;

export function supportReasonKey(reason: string): TranslationKey {
  const known = (REASONS as readonly string[]).includes(reason) ? reason : 'unknown_runtime';
  return `context.events.reason.${known}` as TranslationKey;
}

/** One kind's events, or all, in conversation order. */
export function filterEvents<T extends { event: ContextEventApi }>(
  items: T[],
  kind: ContextEventKind | null,
): T[] {
  return kind ? items.filter((item) => item.event.kind === kind) : items;
}

/** An event row, with the reported compaction it shows when it is one. */
export interface EventItem {
  event: ContextEventApi;
  /** The view's record of the same compaction, with the requests observed around it. */
  compaction: ContextCompactionApi | null;
}

function sameCompaction(event: ContextEventApi, compaction: ContextCompactionApi): boolean {
  if (event.job_id !== compaction.job_id) return false;
  if (event.attempt_id !== null && event.seq !== null) {
    return event.attempt_id === compaction.attempt_id && event.seq === compaction.seq;
  }
  return event.event_id !== null && event.event_id === compaction.event_id;
}

/** A compaction the events list does not carry, as an event row of its own. */
function compactionEvent(compaction: ContextCompactionApi): ContextEventApi {
  const { pre_tokens: before, post_tokens: after } = compaction;
  const sized = before !== null && after !== null;
  return {
    kind: 'compaction',
    source: compaction.source,
    label: compaction.trigger,
    names: [],
    items: null,
    job_id: compaction.job_id,
    turn_no: compaction.turn_no,
    attempt_id: compaction.attempt_id,
    seq: compaction.seq,
    event_id: compaction.event_id,
    at: compaction.at,
    chars: null,
    tokens: sized ? after - before : null,
    tokens_basis: sized ? 'cli' : null,
    before_tokens: before,
    after_tokens: after,
    from_value: null,
    to_value: null,
    superseded: false,
    copied: false,
  };
}

/** Whether `a` comes after `b` in the conversation: by turn, then attempt, then stream order. */
function after(a: ContextEventApi, b: ContextEventApi): boolean {
  if (a.turn_no !== b.turn_no) return a.turn_no > b.turn_no;
  if (a.job_id !== b.job_id) return false;
  const attempt = (a.attempt_id ?? -1) - (b.attempt_id ?? -1);
  if (attempt !== 0) return attempt > 0;
  return (a.seq ?? -1) > (b.seq ?? -1);
}

/**
 * The events, each compaction joined to the view's record of it by evidence
 * identity (attempt and sequence, else stored event), and every reported
 * compaction the events do not carry (an older recording, or one past the
 * listed events) added once, in its place. Nothing is inferred from a drop.
 */
export function mergeCompactions(
  events: ContextEventApi[],
  compactions: ContextCompactionApi[],
): { items: EventItem[]; compactions: number } {
  const identity = (c: ContextCompactionApi) => `${c.job_id}:${c.attempt_id}:${c.seq}`;
  const used = new Set<string>();
  const items: EventItem[] = events.map((event) => {
    if (event.kind !== 'compaction') return { event, compaction: null };
    const match = compactions.find((c) => sameCompaction(event, c)) ?? null;
    if (match) used.add(identity(match));
    return { event, compaction: match };
  });
  let listed = items.filter(({ event }) => event.kind === 'compaction' && !event.superseded).length;
  for (const compaction of compactions) {
    if (used.has(identity(compaction))) continue;
    used.add(identity(compaction));
    const event = compactionEvent(compaction);
    const at = items.findIndex((item) => after(item.event, event));
    items.splice(at < 0 ? items.length : at, 0, { event, compaction });
    listed += 1;
  }
  return { items, compactions: listed };
}

/**
 * The request a compaction is shown at on the trend: the first observed after
 * it (the one the trend marks), else the last one before it.
 */
export function compactionRequest(
  points: ContextPointApi[],
  event: Pick<ContextEventApi, 'job_id' | 'turn_no' | 'attempt_id' | 'seq'>,
): ContextPointApi | null {
  let last = -1;
  for (let i = 0; i < points.length; i += 1) {
    const point = points[i];
    if (point.job_id !== event.job_id || point.attempt_id !== event.attempt_id) continue;
    if (event.seq !== null && point.seq > event.seq) return point;
    last = i;
  }
  if (last >= 0) return points[last + 1] ?? points[last];
  return (
    points.find((point) => point.turn_no > event.turn_no) ??
    points.findLast((point) => point.turn_no <= event.turn_no) ??
    null
  );
}
