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

function flag(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
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
  'agent.database.pgadmin.open': 'Opened pgAdmin',
  'agent.database.pgadmin.setup': 'Set up pgAdmin',
  'agent.settings.update': 'Changed application settings',
  'agent.compute.connection.create': 'Added a server connection',
  'agent.compute.connection.update': 'Updated a server connection',
  'agent.compute.profile.create': 'Added a server setup',
  'agent.compute.cloudlab_autoscaling.set': 'Changed CloudLab autoscaling',
  'agent.compute.server.remove': 'Removed a server from the inventory',
  'agent.compute.allocation.rotate': 'Requested replacing a server',
  'agent.compute.operation.confirm_absent':
    'Confirmed a server operation left nothing behind at the provider',
  'agent.local_broadcast.cancel': 'Cancelled a scheduled mail to accounts',
  'agent.compute.profile.automatic_create': 'Prepared server setups automatically',
  'agent.compute.cloudlab_defaults.set': 'Changed CloudLab defaults',
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
  const recorded = record(entry.details).outcome;
  if (entry.outcome === 'refused' || recorded === 'refused') return 'refused';
  if (entry.outcome === 'failed') return 'failed';
  return entry.outcome === 'replayed' || recorded === 'replayed' ? 'replayed' : 'done';
}

/**
 * What came of a request recorded before it ran: "Completed 2 min later",
 * "Failed 3 h later", "Cancelled", or "Still in progress". ``null`` for a row
 * that recorded its own result.
 */
export function describeResult(entry: AdminAuditLogEntry): string | null {
  if (entry.outcome !== 'requested') return null;
  if (entry.result === null) return 'Still in progress';
  const later = entry.result_at ? ` ${after(entry.created_at, entry.result_at)} later` : '';
  if (entry.result === 'applied') return `Completed${later}`;
  if (entry.result === 'failed') return `Failed${later}`;
  return 'Cancelled';
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

/** A sign-up domain as an administrator entered it. */
function domain(details: Details): string {
  const name = text(details.domain) ?? 'a domain';
  return details.wildcard === true ? `*.${name}` : name;
}

/** An action this page has no sentence for, readable rather than dotted. */
function fallback(action: string): string {
  const words = action.replace(/^agent\./, '').replace(/[._]/g, ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

type Sentence = (
  details: Details,
  host: string,
  definitions: HostSettingDefinition[],
  entry: AdminAuditLogEntry,
) => string;

/** How long after the row its result came, as "2 min" or "3 h". */
function after(from: string, to: string): string {
  const seconds = Math.max(0, (new Date(to).getTime() - new Date(from).getTime()) / 1000);
  if (seconds < 60) return 'moments';
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)} h`;
  return `${Math.floor(seconds / 86_400)} d`;
}

/**
 * An account as the page names it: its name when the row was written, with
 * a short id, because an erased account's name can belong to a new account.
 */
export function accountName(id: string | null, label: string | null, deleted = false): string {
  if (id === null) return label ?? 'an account';
  const short = id.length > 10 ? `${id.slice(0, 8)}…` : id;
  const name = label ? `${label} (${short})` : short;
  return deleted ? `${name}, since deleted` : name;
}

/** The account a row is about. */
function subject(entry: AdminAuditLogEntry): string {
  return accountName(entry.target_user_id, entry.subject_label, entry.subject_deleted);
}

/** Whose sandbox, and which. */
function sandbox(entry: AdminAuditLogEntry, details: Details): string {
  const id = text(details.sandbox_id) ?? entry.target_id;
  return entry.target_user_id ? `${subject(entry)}'s sandbox ${id}` : `sandbox ${id}`;
}

/** Administrators deciding about an account. */
const ACCOUNT: Record<string, (who: string) => string> = {
  approve: (who) => `Approved ${who}`,
  reject: (who) => `Rejected ${who}'s application`,
  revoke: (who) => `Revoked ${who}'s access to Agent`,
  suspend: (who) => `Suspended ${who}`,
  restore: (who) => `Lifted the suspension of ${who}`,
  grant_admin: (who) => `Made ${who} an administrator`,
  revoke_admin: (who) => `Removed ${who}'s administrator access`,
  delete: (who) => `Deleted ${who}`,
  resume: (who) => `Restored deleted account ${who}`,
  annotate: (who) => `Edited the note or suspension message of ${who}`,
  erase: (who) => `Erased ${who} and all of its data`,
  transfer_team: (who) => `Handed ${who}'s team to another member`,
  setup_admin: (who) => `Created the first administrator, ${who}`,
  signup: (who) => `${who} signed up`,
  password_set: (who) => `${who} set a password`,
  password_change: (who) => `${who} changed its password`,
  request_bind_email: (who) => `${who} asked to add an email address`,
  bind_phone: (who) => `${who} added a phone number`,
  reclaim: (who) => `${who} was reclaimed with a password reset`,
  self_delete: (who) => `${who} deleted its own account`,
};

/** Actions that delete the account themselves; saying so again would be noise. */
const DELETING = new Set(['delete', 'erase', 'self_delete']);

const ACCOUNT_SENTENCES: Record<string, Sentence> = Object.fromEntries(
  Object.entries(ACCOUNT).map(([name, sentence]) => [
    `agent.local_account.${name}`,
    (_details: Details, _host: string, _defs: HostSettingDefinition[], entry: AdminAuditLogEntry) =>
      sentence(
        accountName(
          entry.target_user_id,
          entry.subject_label,
          entry.subject_deleted && !DELETING.has(name),
        ),
      ),
  ]),
);

/** The actions whose sentence depends on what was recorded with them. */
const SENTENCES: Record<string, Sentence> = {
  ...ACCOUNT_SENTENCES,
  'agent.local_login_events.purge': (details, _host, _definitions, entry) => {
    const days = count(details.older_than_days);
    const age = days === null ? '' : ` older than ${days} days`;
    return entry.target_user_id
      ? `Purged ${subject(entry)}'s sign-in history${age}`
      : `Purged everyone's sign-in history${age}`;
  },
  'agent.local_broadcast.create': (details) => {
    const recipients = count(details.recipients);
    return recipients === null
      ? 'Sent mail to accounts'
      : `Sent mail to ${recipients} account${recipients === 1 ? '' : 's'}`;
  },
  'agent.local_signup_domain.add': (details) => `Allowed sign-up from ${domain(details)}`,
  'agent.local_signup_domain.remove': (details) =>
    `Stopped allowing sign-up from ${domain(details)}`,
  'agent.runtime_version.upgrade': (details) => {
    const agent = agentName(text(details.runtime) ?? 'agent');
    const version = text(details.version);
    return version ? `Upgraded ${agent} to ${version}` : `Upgraded ${agent}`;
  },
  'agent.sandbox.archive': (details, _host, _definitions, entry) =>
    `Hid ${sandbox(entry, details)} from the inventory`,
  'agent.sandbox.restore': (details, _host, _definitions, entry) =>
    `Showed ${sandbox(entry, details)} in the inventory again`,
  'agent.sandbox.delete.requested': (details, _host, _definitions, entry) =>
    `Requested deleting ${sandbox(entry, details)}`,
  'agent.sandbox.delete.retry_resumed': (details, _host, _definitions, entry) =>
    `Resumed deleting ${sandbox(entry, details)}`,
  'agent.database.query': (details) => {
    const table = text(details.table);
    return table ? `Queried database table ${table}` : 'Queried the database';
  },
  'agent.sandbox_archive_settings.set': (details) => {
    const previous = flag(record(details.old_settings).enabled) ?? flag(details.old_enabled);
    const next = flag(record(details.new_settings).enabled) ?? flag(details.new_enabled);
    if (previous !== null && next !== null && previous !== next) {
      return next ? 'Enabled sandbox archives' : 'Disabled sandbox archives';
    }
    return 'Changed sandbox archive settings';
  },
  'agent.host_settings.set': (details, _host, definitions) => {
    const lines = settingChanges(details, definitions, {
      where: 'for all servers',
      inherited: 'the deployment default',
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
  'agent.sandbox.stop': (details, host, _definitions, entry) =>
    `Requested stopping ${sandbox(entry, details)} on ${host}`,
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
    return sentence(details, text(details.host) ?? 'a server', definitions, entry);
  }
  return FIXED[entry.action] ?? fallback(entry.action);
}

/** Access that belongs to no account, by what it was. */
const BREAK_GLASS: Record<string, string> = {
  'admin-token': 'Admin token',
  'deployment-bootstrap': 'Bootstrap command',
  'first-run-setup': 'First-run setup',
};

/** The control plane acting on its own, by what it was doing. */
const SYSTEM: Record<string, string> = {
  'sandbox-deletion-worker': 'Sandbox cleanup',
  'user-deletion-sweeper': 'Deleted-chat cleanup',
  'signup-policy': 'Sign-up policy',
  'admin-emails': 'Administrator email list',
};

export interface AuditActor {
  label: string;
  title?: string;
  /** ``alert``: access that belongs to no account; ``quiet``: automation. */
  tone?: 'alert' | 'quiet';
}

/**
 * Who acted: "You", an administrator's name, access that belongs to no
 * account (always marked), an automation, or the account itself.
 */
export function describeActor(
  entry: AdminAuditLogEntry,
  currentUserId: string | null | undefined,
): AuditActor {
  const raw = entry.actor_id ?? '';
  const type = entry.actor_type;
  if (type === 'admin' || type === 'user') {
    const id = entry.actor_account_id;
    if (id && currentUserId && id === currentUserId) return { label: 'You', title: id };
    return { label: accountName(id, entry.actor_label, entry.actor_deleted), title: id ?? raw };
  }
  if (type === 'break_glass') {
    const operator = raw.startsWith('operator:') ? `Operator ${raw.slice(9)}` : raw;
    return {
      label: BREAK_GLASS[raw] ?? operator,
      title: 'Access that belongs to no account',
      tone: 'alert',
    };
  }
  if (type === 'service') {
    return { label: raw.replace(/^(service|admin):/, ''), title: raw, tone: 'quiet' };
  }
  if (type === 'system') return { label: SYSTEM[raw] ?? 'System', title: raw, tone: 'quiet' };
  return { label: 'Not signed in', title: raw, tone: 'quiet' };
}

/** Whether the page has its own sentence for ``action``, rather than its name. */
export function hasSentence(action: string): boolean {
  return action in SENTENCES || action in FIXED;
}

/**
 * "2 min ago", "3 h ago", "Yesterday" or a date, with the exact time kept for
 * the title. Days are UTC days, as on every admin page.
 */
export function describeWhen(iso: string, now: number = Date.now()): string {
  const time = new Date(iso).getTime();
  const seconds = Math.max(0, (now - time) / 1000);
  if (seconds < 60) return 'Just now';
  if (seconds < 3_600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3_600)} h ago`;
  // The day before today's, not anything under two days old.
  if (Math.floor(now / 86_400_000) - Math.floor(time / 86_400_000) === 1) return 'Yesterday';
  return new Date(iso).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}
