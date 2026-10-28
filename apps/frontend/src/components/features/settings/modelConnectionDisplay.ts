import type { TranslationKey, Translator } from '@/lib/i18n/translate';

const AGENT_NAMES: Readonly<Record<string, string>> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  kilo: 'Kilo Code',
  opencode: 'OpenCode',
  pi: 'pi',
};
export function connectionAgentName(runtime: string): string {
  return Object.hasOwn(AGENT_NAMES, runtime) ? AGENT_NAMES[runtime] : runtime;
}
export function connectionAgentNames(runtimes: readonly string[]): string {
  return runtimes.map(connectionAgentName).join(', ');
}
const REASONS: Readonly<Record<string, TranslationKey>> = {
  supported: 'connections.reason.supported',
  blocked_address: 'connections.reason.blocked_address',
  unreachable: 'connections.reason.unreachable',
  tls: 'connections.reason.tls',
  redirected: 'connections.reason.redirected',
  auth: 'connections.reason.auth',
  rate_limited: 'connections.reason.rate_limited',
  upstream_error: 'connections.reason.upstream_error',
  too_large: 'connections.reason.too_large',
  endpoint_missing: 'connections.reason.endpoint_missing',
  request_rejected: 'connections.reason.request_rejected',
  not_streaming: 'connections.reason.not_streaming',
  no_tool_call: 'connections.reason.no_tool_call',
  missing_call_id: 'connections.reason.missing_call_id',
  wrong_tool_arguments: 'connections.reason.wrong_tool_arguments',
  no_final_answer: 'connections.reason.no_final_answer',
  stream_error: 'connections.reason.stream_error',
  incomplete_stream: 'connections.reason.incomplete_stream',
  unexpected_status: 'connections.reason.unexpected_status',
  invalid_response: 'connections.reason.invalid_response',
};
/** Localize only known categories; never display arbitrary upstream text. */
export function connectionReason(category: string | null | undefined, t: Translator): string {
  return t(category && Object.hasOwn(REASONS, category) ? REASONS[category] : 'connections.error');
}
