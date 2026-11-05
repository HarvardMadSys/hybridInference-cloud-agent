// What a job's own fields say about it: its display state, an owner-facing
// reading of its failure detail, and how the turn relates to the goal. Shared
// by the Job and Thread adapters, which both read job rows.

import type { AgentJobApi } from '@/lib/api/agents';
import type { AgentGoal, AgentJob, AgentJobState, GoalTurn } from '../types';

/**
 * Preserve a server result as a per-turn display state.
 *
 * Historical turns and their actions keep the distinction between success,
 * failure and cancellation. Conversation badges use `toConversationState`
 * to present successful and stopped turns as idle regardless of publication.
 */
export function toDisplayState(job: AgentJobApi): AgentJobState {
  if (job.state === 'queued') return 'queued';
  if (job.state === 'running' || job.state === 'publishing') return 'running';
  // Retain the legacy result values for historical turn consumers.
  if (job.state === 'succeeded') return job.published_pr_url ? 'done' : 'needs_review';
  if (job.state === 'failed') return 'failed';
  if (job.state === 'cancelled') return 'cancelled';
  return 'queued';
}

/** Infrastructure detail shapes the control plane emits for recoverable failures.
 * Listed individually so adding a new backend message is an explicit edit here
 * rather than a silent leak of infrastructure jargon into the owner-facing UI.
 */
const INFRASTRUCTURE_FAILURE_PATTERNS = [
  /lease[ _](?:expired|lost)/i,
  /lease[^.\n]*expir/i,
  /activation lease/i,
  /indeterminate.*(?:turn|admission)/i,
  /turn.*indeterminate/i,
  /context[ _](?:lost|marked lost)/i,
  /native(?: Agent CLI)?[- _](?:context|session|state)/i,
  /finalization expired/i,
  /Session Supervisor/i,
  /resident transport/i,
  /attempt dispatch failed/i,
  /retained (?:sandbox|Agent CLI|chat|session)/i,
  /shared (?:live|chat|supervisor|Agent CLI|member)/i,
];

/** The runner measured the checkout against its server's import limit and
 * refused it before the agent started. Every attempt measures the same
 * repository, so a follow-up fails the same way. Hosts older than the size
 * report name neither the size nor the limit.
 */
const REPOSITORY_TOO_LARGE =
  /source (?:checkout|archive)(?: \((\S+ \S+) including its Git objects\))? exceeds (?:this host's (\S+ \S+)|the) retained import size limit/i;

export function isRepositoryTooLarge(detail: string | null | undefined): boolean {
  return Boolean(detail && REPOSITORY_TOO_LARGE.test(detail));
}

/** The runner refused a turn its site's own setup made impossible, such as
 * connected tools on a server with no MCP relay. Its message always starts
 * this way and ends by asking for an admin, the only person who can clear
 * the cause, so a follow-up fails the same way until one does.
 */
const SITE_MISCONFIGURED = /^Site misconfigured:\s*/;

export function failureCauseOf(detail: string | null | undefined): AgentJob['failureCause'] {
  if (isRepositoryTooLarge(detail)) return 'repository_too_large';
  if (detail && SITE_MISCONFIGURED.test(detail)) return 'site_misconfigured';
  return undefined;
}

/** A site-misconfigured failure's reason, for a surface whose title already
 * says the site is misconfigured. */
export function siteMisconfiguredReason(detail: string): string {
  const reason = detail.replace(SITE_MISCONFIGURED, '');
  return reason.charAt(0).toUpperCase() + reason.slice(1);
}

/** Translate infrastructure failures only; keep task errors and transcripts intact.
 * The original detail remains in the API and operator logs for diagnosis.
 */
export function ownerFailureMessage(detail: string): string {
  // The agent's own words for why it stopped, e.g. the provider refusing the
  // request. The host records them with this prefix; the rest is the reason.
  const reported = /^agent reported an error: ([\s\S]+)$/.exec(detail);
  if (reported) return reported[1].trim();
  if (/^sandbox failed: /.test(detail)) {
    return 'The sandbox failed while running this response.';
  }
  // Written for the owner already, and naming what only an admin can fix; it
  // must never fold into the generic connection message below.
  if (SITE_MISCONFIGURED.test(detail)) return detail;
  const tooLarge = REPOSITORY_TOO_LARGE.exec(detail);
  if (tooLarge) {
    const [, size, limit] = tooLarge;
    const sizes = [size, limit && `the limit is ${limit}`].filter(Boolean).join('; ');
    return `This repository is larger than this server allows${sizes ? ` (${sizes})` : ''}.`;
  }
  if (/^shared Agent CLI turn exceeded its execution timeout/i.test(detail)) {
    return 'This response took too long and was stopped.';
  }
  if (/^cancelled (?:after|by owner)/i.test(detail)) {
    return 'This response was stopped at your request.';
  }
  if (/finalization expired/i.test(detail)) {
    return 'The response finished, but its completion could not be confirmed. Review the response before continuing.';
  }
  if (INFRASTRUCTURE_FAILURE_PATTERNS.some((pattern) => pattern.test(detail))) {
    return 'The connection was interrupted before this response could be confirmed.';
  }
  return detail;
}

/** How one stored turn relates to the conversation's goal, if it does at all.
 *
 * Only an automatic round has one: the platform reads a report out of a turn it
 * queued itself, and a person's turn is not part of the completion protocol.
 * Every other turn returns nothing, which is what keeps a goal from appearing
 * on every turn of every chat the moment the feature ships.
 */
export function storedGoalTurn(job: AgentJobApi): GoalTurn | undefined {
  if (job.turn_source !== 'goal') return undefined;
  return {
    source: 'goal',
    ...(typeof job.goal_round === 'number' ? { round: job.goal_round } : {}),
    ...(job.goal_round_kind ? { kind: job.goal_round_kind } : {}),
    ...(job.goal_report_parse ? { reportParse: job.goal_report_parse } : {}),
    ...(job.goal_report_outcome ? { reportOutcome: job.goal_report_outcome } : {}),
    ...(job.goal_report_detail ? { reportDetail: job.goal_report_detail } : {}),
  };
}

/** The conversation's goal as the band reads it. */
export function toDisplayGoal(goal: NonNullable<AgentJobApi['goal']>): AgentGoal {
  return {
    objective: goal.objective,
    state: goal.state,
    revision: goal.revision,
    roundsUsed: goal.rounds_used,
    maxRounds: goal.max_rounds,
    ...(goal.pause_reason ? { pauseReason: goal.pause_reason } : {}),
    ...(goal.pause_detail ? { pauseDetail: goal.pause_detail } : {}),
    ...(goal.completion_basis ? { completionBasis: goal.completion_basis } : {}),
  };
}
