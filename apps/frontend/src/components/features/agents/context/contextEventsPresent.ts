// How the Context events read: who produced each one, and why a runtime
// cannot show a kind of event. Pure, so the wording rules are tested apart.

import type { ContextEventApi, ContextEventKind } from '@/lib/api/agents';
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
export function filterEvents(
  events: ContextEventApi[],
  kind: ContextEventKind | null,
): ContextEventApi[] {
  return kind ? events.filter((event) => event.kind === kind) : events;
}
