// Sandbox settings from the admin pages: which value a server should use, and whether it does.
//
// Precedence is the control plane's (`cloud_agent_shared/host_settings.py`): a
// value set for one server, then the value set for all servers, then the
// deployment default. The page only reads what the server
// reports applying and compares — it never assumes a saved value is in force,
// for the same reason the execution-slots cell says "applying" until the host
// confirms.

import type { AgentRunnerHost, HostSettingDefinition, HostSettingValues } from '@/lib/api/admin';
import { HOST_SETTINGS_CAPABILITY } from '@/lib/api/admin';

/** Where the value a server should use comes from. */
export type SettingSource = 'server' | 'global' | 'own';

/** Whether the server has taken that value up. */
export type SettingState = 'applied' | 'applying' | 'unreported' | 'unsupported';

export interface SettingFact {
  definition: HostSettingDefinition;
  /** The admin value that applies to this server, or null when none does. */
  admin: number | null;
  source: SettingSource;
  /** What the server reports using now, or null when it has not said. */
  inUse: number | null;
  /** Legacy local value reported by the server, retained for diagnostics only. */
  own: number | null;
  state: SettingState;
}

/** Whether this server's runner applies settings from this page at all. */
export function supportsSettings(host: AgentRunnerHost): boolean {
  return (host.capabilities ?? []).includes(HOST_SETTINGS_CAPABILITY);
}

function valueOf(values: HostSettingValues | null | undefined, key: string): number | null {
  const value = values?.[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** One setting on one server: the value it should use, where from, and whether it does. */
export function settingFact(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  host: AgentRunnerHost,
): SettingFact {
  const server = valueOf(host.settings_desired, definition.key);
  const global = valueOf(globalValues, definition.key);
  const admin = server ?? global;
  const source: SettingSource = server !== null ? 'server' : global !== null ? 'global' : 'own';
  const inUse = valueOf(host.settings_applied, definition.key);
  const own = valueOf(host.settings_local, definition.key);
  let state: SettingState;
  if (!supportsSettings(host)) {
    state = 'unsupported';
  } else if (inUse === null) {
    state = 'unreported';
  } else {
    // Reset restores the deployment default; a pre-migration local value is
    // diagnostic information and must never become the desired value again.
    const target = admin ?? definition.default;
    state = inUse === target ? 'applied' : 'applying';
  }
  return { definition, admin, source, inUse, own, state };
}

export interface GlobalRollout {
  /** Servers taking this setting from the global value, and applying it. */
  applied: number;
  /** Servers taking it from the global value that have not applied it yet. */
  waiting: number;
  /** Servers with a value of their own, which wins over the global one. */
  overridden: number;
  /** Servers whose runner predates settings and keeps its `.env`. */
  unsupported: number;
}

/** How far the global value of one setting has reached across the servers. */
export function globalRollout(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  hosts: AgentRunnerHost[],
): GlobalRollout {
  const rollout: GlobalRollout = { applied: 0, waiting: 0, overridden: 0, unsupported: 0 };
  for (const host of hosts) {
    const fact = settingFact(definition, globalValues, host);
    if (fact.state === 'unsupported') rollout.unsupported += 1;
    else if (fact.source === 'server') rollout.overridden += 1;
    else if (fact.state === 'applied') rollout.applied += 1;
    else rollout.waiting += 1;
  }
  return rollout;
}

function plural(count: number, one: string, many: string): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A value in its setting's unit, in the largest whole unit that fits. */
export function formatSettingValue(definition: HostSettingDefinition, value: number): string {
  if (definition.unit === 'seconds') {
    if (Number.isInteger(value) && value > 0) {
      if (value % 86_400 === 0) return `${value / 86_400} d`;
      if (value % 3_600 === 0) return `${value / 3_600} h`;
      if (value % 60 === 0) return `${value / 60} min`;
    }
    return `${value} s`;
  }
  if (definition.unit === 'MiB') {
    return Number.isInteger(value) && value > 0 && value % 1024 === 0
      ? `${value / 1024} GiB`
      : `${value} MiB`;
  }
  if (definition.unit === 'KiB') return `${value} KiB`;
  if (definition.unit === 'millicores') return `${value / 1000} vCPU`;
  return String(value);
}

/** A unit a value can be typed in; the API takes the definition's own unit. */
export type InputUnit = 's' | 'min' | 'h' | 'd' | 'MiB' | 'GiB' | 'KiB' | 'vCPU';

const UNIT_SIZE: Record<InputUnit, number> = {
  s: 1,
  min: 60,
  h: 3_600,
  d: 86_400,
  MiB: 1,
  GiB: 1024,
  KiB: 1,
  vCPU: 1000,
};

/**
 * The units a setting can be typed in, smallest first; none for a count.
 *
 * Typing eight hours as 28800 was the old field's main complaint. Seconds stay
 * on the list so any stored value can still be shown exactly, and days appear
 * only where the bound allows more than one.
 */
export function inputUnits(definition: HostSettingDefinition): InputUnit[] {
  if (definition.unit === 'seconds') {
    return definition.maximum >= 2 * 86_400 ? ['s', 'min', 'h', 'd'] : ['s', 'min', 'h'];
  }
  if (definition.unit === 'MiB') return ['MiB', 'GiB'];
  if (definition.unit === 'KiB') return ['KiB'];
  if (definition.unit === 'millicores') return ['vCPU'];
  return [];
}

/** The largest unit a value is a whole number of, so 7200 s opens as 2 h. */
export function unitFor(definition: HostSettingDefinition, value: number): InputUnit | null {
  const units = inputUnits(definition);
  if (units.length === 0) return null;
  const whole = [...units].reverse().find((unit) => Number.isInteger(value / UNIT_SIZE[unit]));
  return value > 0 && whole !== undefined ? whole : units[0];
}

/** A value as it is typed in a unit: 7200 s in h is "2". */
export function amountIn(value: number, unit: InputUnit | null): string {
  return String(unit === null ? value : value / UNIT_SIZE[unit]);
}

/**
 * Read a typed amount in a unit: a whole number of the setting's own unit
 * inside its bounds, or why not. A decimal amount is fine when it lands on a
 * whole second, MiB or millicore, so 1.5 h is 5400 s and 1.25 vCPU is 1250
 * millicores.
 */
export function parseSettingInput(
  definition: HostSettingDefinition,
  raw: string,
  unit: InputUnit | null = null,
): { value: number } | { error: string } {
  const text = raw.trim();
  const amount = Number(text);
  const value = unit === null ? amount : amount * UNIT_SIZE[unit];
  // Multiplying a decimal amount can leave float noise (1.1 h); a real
  // fraction of the setting's own unit is still refused.
  const whole = Math.round(value);
  if (
    text === '' ||
    !Number.isFinite(value) ||
    Math.abs(value - whole) > 1e-6 ||
    whole < definition.minimum ||
    whole > definition.maximum
  ) {
    return {
      error:
        definition.unit === 'count'
          ? `Enter a whole number from ${definition.minimum} to ${definition.maximum}.`
          : `Enter a value from ${formatSettingValue(definition, definition.minimum)} to ${formatSettingValue(definition, definition.maximum)}.`,
    };
  }
  return { value: whole };
}

/**
 * What saving a shorter value does to terminals already open, or null.
 *
 * The broker judges open terminals by the new limits from its next sweep
 * (`_TerminalManager.configure`), so a shorter one closes those already past
 * it. The other settings take effect for later turns, imports or sandboxes,
 * which their descriptions say.
 */
export function shorteningEffect(definition: HostSettingDefinition, value: number): string | null {
  const shown = formatSettingValue(definition, value);
  if (definition.key === 'terminal_idle_ttl_s') {
    return `Open terminals idle longer than ${shown} will close.`;
  }
  if (definition.key === 'terminal_hard_ttl_s') {
    return `Open terminals older than ${shown} will close.`;
  }
  return null;
}

/** Name up to two servers, or count more, so the line stays one line. */
const NAMED_SERVERS = 2;

/**
 * A one-line account of a global value's reach, for under its control.
 *
 * A server that sets its own value is named with it — "rtx6000b sets its own
 * (128)" — because a count alone sent the admin through every server's details
 * to find which one differs.
 */
export function rolloutText(
  definition: HostSettingDefinition,
  globalValues: HostSettingValues,
  hosts: AgentRunnerHost[],
  nameOf: (host: AgentRunnerHost) => string = (host) => host.host,
): string {
  const rollout = globalRollout(definition, globalValues, hosts);
  const parts: string[] = [];
  if (valueOf(globalValues, definition.key) === null) {
    parts.push(`Deployment default: ${formatSettingValue(definition, definition.default)}`);
  } else {
    const following = rollout.applied + rollout.waiting;
    parts.push(
      rollout.waiting === 0
        ? `In use on ${plural(following, 'server', 'servers')}`
        : `Applied on ${rollout.applied} of ${plural(following, 'server', 'servers')}`,
    );
  }
  const own = hosts.filter(
    (host) => supportsSettings(host) && valueOf(host.settings_desired, definition.key) !== null,
  );
  if (own.length > NAMED_SERVERS) {
    parts.push(plural(own.length, 'server sets its own', 'servers set their own'));
  } else {
    for (const host of own) {
      const value = valueOf(host.settings_desired, definition.key) as number;
      parts.push(`${nameOf(host)} sets its own (${formatSettingValue(definition, value)})`);
    }
  }
  const old = hosts.filter((host) => !supportsSettings(host));
  if (old.length > NAMED_SERVERS) {
    parts.push(`${plural(old.length, 'server needs', 'servers need')} a redeploy`);
  } else {
    for (const host of old) parts.push(`${nameOf(host)} needs a redeploy`);
  }
  return parts.join(' · ');
}

/** What a server-level row says about where its value comes from and whether it is in force. */
export function factNote(fact: SettingFact): string {
  if (fact.state === 'unsupported') return 'Redeploy this server to manage it here';
  const from =
    fact.source === 'server'
      ? 'set for this server'
      : fact.source === 'global'
        ? 'set for all servers'
        : 'deployment default';
  if (fact.state === 'unreported') return `${from} · not reported yet`;
  if (fact.state === 'applying') {
    const target = fact.admin ?? fact.definition.default;
    return `${from} · applying ${formatSettingValue(fact.definition, target)}`;
  }
  return from;
}
