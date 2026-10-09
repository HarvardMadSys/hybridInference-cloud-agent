// What an audit entry says, as one sentence an admin can read.
//
// A separate module because `page.tsx` may not export anything else (see
// `hosts/format.ts`). The raw action and its details stay one click away on the
// page; this is the line that has to make sense without them.

import type { AdminAuditLogEntry, HostSettingDefinition } from '@/lib/api/admin';

import { formatSettingValue } from '../hosts/settings';
import { agentName } from '../settings/AgentVersions';

type Details = Record<string, unknown>;

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function record(value: unknown): Details {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Details)
    : {};
}

/**
 * One sentence per changed setting: "Set Turn time limit to 2 h for all
 * servers (was 1 h)". `where` is "for all servers" or "on <host>"; a cleared
 * value falls back to what the scope inherits, so it is named as that.
 */
function settingChanges(
  details: Details,
  definitions: HostSettingDefinition[],
  scope: { where: string; inherited: string },
): string[] {
  const old = record(details.old_values);
  return Object.entries(record(details.changes)).map(([key, value]) => {
    const definition = definitions.find((item) => item.key === key);
    const label = definition?.label ?? key;
    const show = (raw: unknown) => {
      const number = count(raw);
      if (number === null) return scope.inherited;
      return definition ? formatSettingValue(definition, number) : String(number);
    };
    return count(value) === null
      ? `Reset ${label} ${scope.where} to ${scope.inherited} (was ${show(old[key])})`
      : `Set ${label} to ${show(value)} ${scope.where} (was ${show(old[key])})`;
  });
}

/**
 * Sentences that need nothing from the details.
 *
 * Most server actions are recorded when the request is queued (the route
 * answers 202), long before the provider has done anything, and the operation
 * can still fail. Those read as requests, never as outcomes. Connection
 * changes and the lease policy are applied before they are recorded.
 */
const FIXED: Record<string, string> = {
  'agent.compute.connection.create': 'Added a server connection',
  'agent.compute.connection.update': 'Updated a server connection',
  'agent.compute.connection.validate': 'Checked a server connection',
  'agent.compute.allocation.policy': "Changed a server's lease policy",
  'agent.compute.server.add': 'Requested a new server',
  'agent.compute.server.preflight': 'Requested a check of a server before adding it',
  'agent.compute.allocation.import': 'Requested importing an existing server',
  'agent.compute.allocation.extend': 'Requested a lease extension',
  'agent.compute.allocation.retire': 'Requested releasing a server',
  'agent.compute.operation.retry': 'Requested a retry of a server operation',
  'agent.compute.operation.cancel': 'Requested cancelling a server operation',
  'agent.compute.operation.resolve': 'Requested resolving a server operation by hand',
};

/**
 * Whether a recorded action took effect.
 *
 * Measurement Start and Stop record an ``outcome``: ``applied`` changed the
 * control, ``replayed`` answered a repeat without changing anything, and
 * ``refused`` changed nothing either. ``refused`` comes from the details, not
 * from ``success``: refusals were recorded with ``success`` true before that
 * was fixed, and those rows must not read as measurements turned on or off.
 */
export function auditOutcome(
  entry: AdminAuditLogEntry,
): 'done' | 'replayed' | 'refused' | 'failed' {
  const outcome = record(entry.details).outcome;
  if (outcome === 'refused') return 'refused';
  if (!entry.success) return 'failed';
  return outcome === 'replayed' ? 'replayed' : 'done';
}

/**
 * A measurement Start or Stop, by what it did.
 *
 * ``replayed`` is a retried request id, or a Stop with nothing running (a
 * double click): either way the control did not change, so it must not read
 * as measurements turned on or off — an experiment started since may still
 * be running.
 */
function measurementChange(state: 'on' | 'off', details: Details): string {
  if (details.outcome === 'refused') {
    const reason = text(details.reason);
    return reason
      ? `Tried to turn measurements ${state} (refused: ${reason})`
      : `Tried to turn measurements ${state} (refused)`;
  }
  if (details.outcome === 'replayed') {
    return `Asked to turn measurements ${state} again; nothing changed`;
  }
  return `Turned measurements ${state}`;
}

/** An action this page has no sentence for, readable rather than dotted. */
function fallback(action: string): string {
  const words = action.replace(/^agent\./, '').replace(/[._]/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

type Sentence = (details: Details, host: string, definitions: HostSettingDefinition[]) => string;

/** The actions whose sentence depends on what was recorded with them. */
const SENTENCES: Record<string, Sentence> = {
  'agent.host_settings.set': (details, _host, definitions) => {
    const lines = settingChanges(details, definitions, {
      where: 'for all servers',
      inherited: "each server's default",
    });
    return lines.length > 0 ? lines.join('; ') : 'Changed sandbox settings for all servers';
  },
  'agent.runner_host.set_settings': (details, host, definitions) => {
    const lines = settingChanges(details, definitions, {
      where: `on ${host}`,
      inherited: 'the Settings value',
    });
    return lines.length > 0 ? lines.join('; ') : `Changed sandbox settings on ${host}`;
  },
  'agent.runtime_version.set': (details) => {
    const agent = agentName(text(details.runtime) ?? 'agent');
    const next = text(details.new_version);
    const previous = text(details.old_version) ?? 'the server default';
    return next
      ? `Switched new ${agent} sandboxes to ${next} (was ${previous})`
      : `Put new ${agent} sandboxes back on the server default (was ${previous})`;
  },
  'agent.runner_host.set_active': (details) => {
    const next = text(details.new_host);
    return next ? `Sent new sandboxes to ${next}` : 'Let new sandboxes go to any available server';
  },
  'agent.runner_host.set_slots': (details, host) => {
    const next = count(details.new_slots);
    const previous = count(details.old_slots);
    return next === null
      ? `Put ${host} back on its own number of turns at once`
      : `Set ${host} to ${next} turns at once (was ${previous ?? 'its own'})`;
  },
  'agent.runner_host.drain': (_details, host) => `Paused new work on ${host}`,
  'agent.runner_host.undrain': (_details, host) => `Resumed new work on ${host}`,
  'agent.runner_host.remove': (details, host) =>
    details.force === true
      ? `Removed ${host} from the pool while it was running work`
      : `Removed ${host} from the pool`,
  'agent.runner_host.forget': (_details, host) => `Forgot ${host}`,
  'agent.runner_host.enroll_token_issued': (_details, host) =>
    `Issued an enrollment token for ${host}`,
  'agent.profiling.start': (details) => measurementChange('on', details),
  'agent.profiling.stop': (details) => measurementChange('off', details),
  'agent.run.event_content.read': (details) => {
    const turn = text(details.job_id);
    return turn ? `Viewed a step's content in turn ${turn}` : "Viewed a step's content";
  },
};

/** What happened, as one sentence. */
export function describeAuditEntry(
  entry: AdminAuditLogEntry,
  definitions: HostSettingDefinition[] = [],
): string {
  const sentence = SENTENCES[entry.action];
  if (sentence) {
    const details = record(entry.details);
    return sentence(details, text(details.host) ?? 'a server', definitions);
  }
  return FIXED[entry.action] ?? fallback(entry.action);
}

/**
 * Who acted: "You", the operator token, or a short form of another admin's id.
 *
 * Only an id is known: identity tokens deliberately carry no display name, and
 * the Gateway offers no contract to look one up.
 */
export function describeActor(
  actorId: string | null,
  currentUserId: string | null | undefined,
): { label: string; title?: string } {
  if (actorId === null) return { label: 'An admin' };
  if (actorId === 'admin-token')
    return { label: 'Admin token', title: 'The ADMIN_TOKEN credential' };
  const userId = actorId.startsWith('admin:') ? actorId.slice('admin:'.length) : actorId;
  if (currentUserId && userId === currentUserId) return { label: 'You', title: userId };
  return { label: userId.length > 10 ? `${userId.slice(0, 8)}…` : userId, title: userId };
}

/** "2 min ago", "3 h ago", "Yesterday" or a date, with the exact time kept for the title. */
export function describeWhen(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return 'Just now';
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)} h ago`;
  if (seconds < 2 * 86_400) return 'Yesterday';
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
