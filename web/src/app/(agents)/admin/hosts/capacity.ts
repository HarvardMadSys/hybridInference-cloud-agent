// Capacity cells, aggregation and chart geometry for the Servers table.
//
// **A sibling of `format.ts`, for the reason that module gives.** `page.tsx`
// may not export anything but its default component, so everything the table
// and the expanded panel need lives here, where it is tested directly instead
// of through a rendered table.
//
// Three rules run through the whole module, because each of them is a way of
// stating something the server never said:
//
//   * a missing number is never rendered as zero — "never measured" and "idle"
//     are different facts, and only one of them is good news;
//   * counts from two sources are added only when they are provably disjoint —
//     the same namespace reported twice must not double, and two Docker hosts
//     behind one physical name must not merge;
//   * an age advances with local monotonic time, never with the browser's wall
//     clock, so a clock adjustment cannot make an hour-old reading look new;
//   * a field this client has no schema for is missing, not zero — the storage
//     block arrived after the page did, so a control plane older than it sends
//     neither the numbers nor a reason, and both must read as "not measured".
//
// The server's contract is in `cloud_agent/capacity_protocol.py`; where a name
// here differs from that module it is deliberate (the chart's `value` is the
// percentage the curve plots, not the raw field).

import type {
  AgentHostCapacity,
  AgentHostCapacityInventory,
  AgentHostCapacityPoint,
  AgentRunnerHost,
  CapacityInventoryStatus,
  CapacityRange,
} from '@/lib/api/admin';
import { HOST_CONCURRENCY_CAPABILITY } from '@/lib/api/admin';

import { isReporting } from './format';

// ── Numbers ────────────────────────────────────────────────────────────

const BYTES_PER_GIB = 1024 ** 3;
const BYTES_PER_TIB = 1024 ** 4;

/** Whole percent, for the table. A tenth here is false precision. */
export function formatPercent(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  return `${Math.round(value)}%`;
}

/**
 * Whole percent, rounded **up** — the one figure on this page that does not
 * round to nearest.
 *
 * GNU `df` rounds its capacity percentage up, and this cell is the page's
 * rendering of the same number, so an operator comparing the two must not find
 * them a point apart. The direction is also the safer one for the only
 * question the column answers: 99.4% full is 100% full to anyone deciding
 * whether to clear space, and rounding it down would say there is room for
 * another round of builds on a disk that has none.
 */
export function formatPercentCeil(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  return `${Math.ceil(value)}%`;
}

/** One decimal, for tooltips, where the reader is comparing two nearby points. */
export function formatPercentTenth(value: number | null): string | null {
  if (value === null || !Number.isFinite(value)) return null;
  return `${value.toFixed(1)}%`;
}

/**
 * A figure in some size unit, at the precision a reader can act on.
 *
 * Above ten units the tenth is noise and the integer is what an operator
 * compares against a machine's nameplate; below it, the tenth is the
 * difference between a snug machine and a comfortable one.
 *
 * One function rather than one rule per unit, because the Memory and the
 * Storage cells sit side by side on a single table row: two spellings of this
 * rule would print the same quantity two ways on that row, and a reader would
 * be left working out which of the two was the real reading.
 */
function unitNumber(value: number): string {
  return value >= 10 ? `${Math.round(value)}` : `${Math.round(value * 10) / 10}`;
}

/** Bytes as GiB, at `unitNumber`'s precision. */
export function formatGib(bytes: number | null): string | null {
  if (bytes === null || !Number.isFinite(bytes)) return null;
  return unitNumber(bytes / BYTES_PER_GIB);
}

/** `42 / 128 GiB`, or null when either half is unknown. */
export function formatGibPair(bytes: number | null, total: number | null): string | null {
  const used = formatGib(bytes);
  const whole = formatGib(total);
  return used === null || whole === null ? null : `${used} / ${whole} GiB`;
}

const MISSING_REASONS: Record<string, string> = {
  warming_up: 'Warming up',
  counter_reset: 'Counter reset',
  source_unavailable: 'Not measured',
  invalid_sample: 'Invalid sample',
  not_reported: 'Not measured',
  disabled: 'Storage measurement disabled',
  unsupported: 'Storage not supported on this host',
  unverified: 'Storage not verified',
};

/**
 * A bounded missing-measurement reason as words.
 *
 * One table serves CPU, memory and storage: the three vocabularies overlap
 * (`source_unavailable` and `invalid_sample` mean the same gap whichever
 * metric hit them) and the keys that do not overlap cannot collide, so a
 * single lookup cannot pick the wrong sentence. Only the storage entries are
 * about a deployment rather than a failed sample — `disabled` is a setting,
 * `unsupported` is a topology the probe refused, and `unverified` is an
 * attestation that has not happened yet.
 *
 * An unrecognized reason reads as "Not measured" rather than as itself: a raw
 * identifier is not an explanation, and inventing one for a value this client
 * does not know would be worse than admitting the gap.
 */
export function missingReasonLabel(reason: string | null): string {
  return reason === null ? 'Not measured' : (MISSING_REASONS[reason] ?? 'Not measured');
}

// ── Cells ──────────────────────────────────────────────────────────────

/**
 * One table cell: a first line, an optional smaller second line, and the full
 * sentence a `title` carries when the two lines cannot hold it.
 */
export interface CapacityCell {
  text: string;
  note: string | null;
  title: string | null;
}

/**
 * The ceiling the scheduler enforces on one host: the lower of the limit the
 * host reports and the one an operator asked for, or null when neither is set.
 *
 * Taking the lower one is what makes a change safe in both directions. A
 * number the host has not confirmed admits only what the host has agreed to
 * run, and a number that was lowered stops admitting before the host has even
 * heard about it.
 */
export function executionCeiling(reported: number | null, requested: number | null): number | null {
  if (reported === null) return requested;
  if (requested === null) return reported;
  return Math.min(reported, requested);
}

/** Whether the host has taken up the number an operator saved, and if not, why. */
export type ConcurrencyState =
  | 'default' // nothing saved; the host runs its own
  | 'applied' // saved, and the host reports the same limit
  | 'pending' // saved; the host has not confirmed it yet
  | 'offline' // saved; nothing will confirm it until the host comes back
  | 'unsupported'; // saved; this runner changes only by redeployment

/**
 * What an operator needs to read off one host's concurrency, as facts.
 *
 * Saving a number and a host serving it are two different events, and a page
 * that showed only the saved number would report a change that had not
 * happened. So every field here is separately true: `requested` is what was
 * saved, `reported` is what the host last said it serves, `enforced` is what
 * the scheduler will actually admit against, `running` is what is running, and
 * `finishing` is how many of those are above a limit that has since come down.
 */
export interface ConcurrencyFacts {
  state: ConcurrencyState;
  requested: number | null;
  reported: number | null;
  enforced: number | null;
  running: number;
  free: number | null;
  /** Turns admitted before the limit came down. They are never interrupted. */
  finishing: number;
  /** Why a host with room is claiming nothing, straight from the host. */
  launchError: string | null;
  explanation: string | null;
}

export function concurrencyFacts(
  host: Pick<
    AgentRunnerHost,
    'slots_used' | 'slots_total' | 'slots_desired' | 'capabilities' | 'status' | 'launch_error'
  >,
): ConcurrencyFacts {
  const requested = host.slots_desired ?? null;
  const reported = host.slots_total;
  const enforced = executionCeiling(reported, requested);
  const running = host.slots_used;
  const launchError = (host.launch_error ?? '').trim() || null;
  const resizable = host.capabilities.includes(HOST_CONCURRENCY_CAPABILITY);
  const offline = host.status === 'offline' || host.status === 'enrolling';

  let state: ConcurrencyState = 'default';
  let explanation: string | null = null;
  if (requested !== null) {
    if (!resizable) {
      state = 'unsupported';
      explanation =
        "This host's runner predates remote limits. It keeps the number it was deployed with until it is redeployed.";
    } else if (reported === requested) {
      state = 'applied';
    } else if (offline) {
      state = 'offline';
      explanation = `Saved, but this host is ${host.status} and cannot take it up until it reports in.`;
    } else {
      state = 'pending';
      explanation = `Saved. This host still reports ${reported === null ? 'no limit' : reported} and takes the new one up on its next heartbeat.`;
    }
  }

  return {
    state,
    requested,
    reported,
    enforced,
    running,
    free: enforced === null ? null : Math.max(enforced - running, 0),
    finishing: enforced === null ? 0 : Math.max(running - enforced, 0),
    launchError,
    explanation,
  };
}

/**
 * Concurrency as one table cell: `2 / 3 in use · 1 free`.
 *
 * The denominator is what the scheduler enforces, never the number that was
 * merely saved — a page that showed the saved number would claim a change the
 * host has not made. Everything the two can disagree about is said in the
 * note instead: a saved number still being taken up, one a host cannot take
 * up, and turns that were admitted before a limit came down and are finishing.
 *
 * A host with nothing reported and nothing saved has an unknown number of free
 * slots, not zero and not every number: it says `Limit not set` and the
 * running count, and no free count is invented from it.
 */
export function executionSlotsCell(
  host: Pick<
    AgentRunnerHost,
    'slots_used' | 'slots_total' | 'slots_desired' | 'capabilities' | 'status' | 'launch_error'
  >,
): CapacityCell {
  const facts = concurrencyFacts(host);
  if (facts.enforced === null) {
    return { text: 'Limit not set', note: `${facts.running} in use`, title: null };
  }
  const notes = [`${facts.free} free`];
  if (facts.state === 'pending') notes.push(`applying ${facts.requested}`);
  if (facts.state === 'offline') notes.push(`${facts.requested} not applied`);
  if (facts.state === 'unsupported') notes.push('redeploy to change');
  if (facts.finishing > 0) notes.push(`${facts.finishing} finishing`);
  if (facts.launchError !== null) notes.push('not claiming');
  return {
    text: `${facts.running} / ${facts.enforced} in use`,
    note: notes.join(' · '),
    title: [facts.explanation, facts.launchError].filter(Boolean).join(' ') || null,
  };
}

/** CPU load, or why there is none. Never `0%` for a value nobody measured. */
export function cpuCell(capacity: AgentHostCapacity | null): CapacityCell {
  if (capacity === null) {
    return {
      text: 'Not measured',
      note: null,
      title: 'No capacity observation has arrived from this host.',
    };
  }
  const percent = formatPercent(capacity.cpu_percent);
  if (percent === null) {
    return {
      text: missingReasonLabel(capacity.cpu_missing_reason),
      note: null,
      title: `CPU is not measured: ${capacity.cpu_missing_reason ?? 'reason not reported'}.`,
    };
  }
  const logical = capacity.cpu_logical_count;
  const window = capacity.cpu_window_seconds;
  const windowText = window === null ? null : `${Math.round(window)}s`;
  return {
    text: percent,
    note: logical === null ? null : `${logical} logical CPUs`,
    title: `Host-wide CPU across every logical CPU, other processes included${
      windowText === null ? '.' : `, averaged over ${windowText}.`
    } Source: ${capacity.source}.`,
  };
}

/**
 * Memory load: `33% · 42 / 128 GiB` and how much is still available.
 *
 * The available figure is reported rather than computed from a sandbox's
 * declared limit: a container limit is not a reservation, and dividing the
 * free memory by it would answer a question about scheduling with a number
 * about capacity.
 */
export function memoryCell(capacity: AgentHostCapacity | null): CapacityCell {
  if (capacity === null) {
    return {
      text: 'Not measured',
      note: null,
      title: 'No capacity observation has arrived from this host.',
    };
  }
  const percent = formatPercent(capacity.memory_used_percent);
  if (percent === null) {
    return {
      text: missingReasonLabel(capacity.memory_missing_reason),
      note: null,
      title: `Memory is not measured: ${capacity.memory_missing_reason ?? 'reason not reported'}.`,
    };
  }
  const pair = formatGibPair(capacity.memory_used_bytes, capacity.memory_total_bytes);
  const available = formatGib(capacity.memory_available_bytes);
  return {
    text: pair === null ? percent : `${percent} · ${pair}`,
    note: available === null ? null : `${available} GiB available`,
    title: `Used is total minus available, as reported by the Docker host. Source: ${capacity.source}.`,
  };
}

// ── Storage ────────────────────────────────────────────────────────────

/**
 * The size unit a figure is read in.
 *
 * Tie-break at 1 TiB, so a thousand-gigabyte disk reads `0.98 TiB` rather than
 * `1000 GiB`: four digits are read a digit at a time, and the point of the
 * column is comparing disks to each other at a glance. GiB below it is the
 * unit an operator quotes build sizes and image layers in.
 */
const STORAGE_TIB_THRESHOLD = BYTES_PER_TIB;

/**
 * The unit a total is read in, and the divisor that expresses any other byte
 * figure in the same unit.
 */
function storageUnit(total: number): readonly [string, number] {
  return total >= STORAGE_TIB_THRESHOLD
    ? (['TiB', BYTES_PER_TIB] as const)
    : (['GiB', BYTES_PER_GIB] as const);
}

/**
 * A byte figure as a bare number in `unit`.
 *
 * The precision rule is literally `formatGib`'s, applied one exponent up, and
 * it is decided on the figure itself: below ten units the tenth distinguishes
 * a snug disk from a comfortable one, at or above ten it is noise — which is
 * what keeps a four-digit disk readable. It is the same `unitNumber` memory
 * goes through rather than a second statement of the rule, so that a Memory
 * and a Storage cell on one row cannot round the same quantity differently.
 */
function storageNumber(bytes: number, unitDivisor: number): string {
  return unitNumber(bytes / unitDivisor);
}

/**
 * A byte figure in the largest unit it fits, or null when there is none.
 *
 * Used for the *available* figure, which is small when the disk is nearly full
 * and would otherwise be printed in the total's unit as `0 TiB`.
 */
export function formatStorageSize(bytes: number | null | undefined): string | null {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return null;
  const [unit, divisor] = storageUnit(bytes);
  return `${storageNumber(bytes, divisor)} ${unit}`;
}

/**
 * `used / total`, in the unit the **total** is read in.
 *
 * Both halves are forced into the total's unit so the pair stays comparable:
 * `0.4 / 2 TiB` is a ratio a reader can take in, where `410 / 2` is two
 * scales wearing one label. Each half keeps its own precision, because the
 * precision rule is about how many digits a reader has to take in and 500 GiB
 * is not a coarser reading than 0.5 TiB — it is the same one in the unit the
 * total chose.
 */
export function formatStoragePair(
  used: number | null | undefined,
  total: number | null | undefined,
): string | null {
  if (used === null || used === undefined || total === null || total === undefined) return null;
  if (!Number.isFinite(used) || !Number.isFinite(total)) return null;
  const [unit, divisor] = storageUnit(total);
  return `${storageNumber(used, divisor)} / ${storageNumber(total, divisor)} ${unit}`;
}

/**
 * One optional storage field, with absence and null collapsed into "missing".
 *
 * The DTO's storage fields are optional because the page ships on its own
 * schedule; a control plane older than this client omits them rather than
 * sending nulls. Every read below goes through here, so "not measured" has one
 * spelling instead of two, and the null checks stay honest without a `?? null`
 * on each of them.
 */
function optionalBytes(value: number | null | undefined): number | null {
  return value === undefined ? null : value;
}

/**
 * Storage load: `74% · 742 / 1000 GiB` and how much room is still writable.
 *
 * Deliberately not the memory cell with different field names. Both figures
 * are block space in `df` terms, and the differences are the ones a reader has
 * to be told about rather than left to infer:
 *
 *   * `used` is `total - free`, and the root reserve that is still free sits
 *     outside both it and `available` — free, but not to an unprivileged
 *     writer. The two lines are therefore not a partition, and the tooltip
 *     says so: computing used as `total - available` would count that unused
 *     reserve as written space;
 *   * the percentage is GNU `df`'s, and rounds up, unlike CPU and memory;
 *   * available space picks its own unit, so a nearly full terabyte disk does
 *     not report `0 TiB` still writable when the answer is 40 GiB.
 */
export function storageCell(capacity: AgentHostCapacity | null): CapacityCell {
  if (capacity === null) {
    return {
      text: 'Not measured',
      note: null,
      title: 'No capacity observation has arrived from this host.',
    };
  }
  const percent = formatPercentCeil(optionalBytes(capacity.storage_used_percent));
  if (percent === null) {
    return {
      text: missingReasonLabel(capacity.storage_missing_reason ?? null),
      note: null,
      title: `Storage is not measured: ${
        capacity.storage_missing_reason ?? 'reason not reported'
      }.${unverifiedNote(capacity)}`,
    };
  }
  const total = optionalBytes(capacity.storage_total_bytes);
  const used = optionalBytes(capacity.storage_used_bytes);
  const pair = formatStoragePair(used, total);
  // The available figure is fitted with its own unit rather than the pair's:
  // on a 2 TiB disk with 40 GiB left, "0 TiB available" is a number nobody can
  // act on and reading it as zero would be worse than the number itself.
  const available = formatStorageSize(optionalBytes(capacity.storage_available_bytes));
  return {
    text: pair === null ? percent : `${percent} · ${pair}`,
    note: available === null ? null : `${available} available`,
    title: storageTitle(capacity),
  };
}

/**
 * The extra sentence a missing cell owes a reader when the reason is
 * `unverified`.
 *
 * Only that reason: `disabled` is a decision somebody made and `unsupported` a
 * topology the probe refused, while `unverified` is work the deployment still
 * owes and will finish on its own — a `check` run, or the next `up`. The
 * sentence names the verification rather than promising a first one, because
 * the reason covers both "never attested" and "attested, but the filesystem is
 * no longer the size it was" — the control plane reports no figures either way,
 * so a reader must not be told the numbers on screen are merely unconfirmed.
 */
function unverifiedNote(capacity: AgentHostCapacity): string {
  if (capacity.storage_missing_reason !== 'unverified') return '';
  return (
    ' The filesystem has not been verified as the one the retained sandboxes write to; ' +
    'a successful deployment verification would confirm it.'
  );
}

/**
 * The storage tooltip: what was measured, in which terms, and where.
 *
 * A cell can only carry a percentage and a pair, and storage needs three facts
 * the two lines cannot hold — the reserved-block caveat, which filesystem the
 * numbers describe, and who measured them. `target` is the host mount root and
 * is spelled as such, because the value on its own (`/mnt/agent-docker`) reads
 * as a path the reader is expected to recognize.
 */
function storageTitle(capacity: AgentHostCapacity): string {
  const target = capacity.storage_target;
  const sentences = [
    `Block space of the filesystem holding the sandboxes' writable layers${
      target ? `, the filesystem mounted at ${target}` : ''
    }.`,
    // One sentence on purpose: the reader's question is why the two lines do
    // not add up to the total, and the answer is a third quantity — the
    // reserve — that neither line counts.
    'The percentage is used / (used + available), as GNU df prints it: used is total minus free, and ' +
      'the reserved blocks that are still free are in neither figure, so used plus available need ' +
      'not equal the total.',
    // The same fact the CPU and memory charts state in the panel, said here
    // because this metric has no chart of its own: the column is a reading of
    // now, not a trend, and a disk filling over days is not something a
    // minutes-long window would show.
    'Like memory, it is an instantaneous observation rather than an average.',
  ];
  // The probe's own name, not the payload's: `source` describes the collector,
  // and only `storage_source` says which filesystem was actually measured.
  const source = capacity.storage_source;
  if (source) sentences.push(`Source: ${source}.`);
  return `${sentences.join(' ')}${unverifiedNote(capacity)}`;
}

// ── Retained sandboxes ─────────────────────────────────────────────────

/** Fixed vocabulary of Docker states outside running and exited. */
export const OTHER_CONTAINER_STATES = [
  'created',
  'paused',
  'restarting',
  'removing',
  'dead',
] as const;

/**
 * Operator-facing text for the bounded reason identifiers a runner may send.
 *
 * The identifiers are a fixed vocabulary on the wire; these are the sentences
 * the page may show. An unknown one is shown as itself, because a reason the
 * client cannot translate is still better than no reason at all.
 */
const REASON_LABELS: Record<string, string> = {
  unscoped_legacy_containers: 'Legacy containers carry no namespace label',
  namespace_unset: 'This runner has no sandbox namespace configured',
  unknown_state: 'A container reported a state outside the fixed vocabulary',
  docker_timeout: 'The Docker container listing timed out',
  duplicate_sandbox_id: 'One sandbox id was seen on two containers',
  sources_not_summed: 'Two sources cannot be proven to describe the same Docker host',
  source_stale: 'An expired observation is excluded from the current count',
  inventory_not_reported: 'At least one enrollment on this server reported no inventory',
};

export function reasonLabel(reason: string): string {
  return REASON_LABELS[reason] ?? reason;
}

/** One contributing enrollment, as the expanded panel lists it. */
export interface SandboxSourceLine {
  host: string;
  namespace: string | null;
  status: CapacityInventoryStatus;
  /** `3 running · 7 stopped · 1 paused`, or `Not counted`. */
  counts: string;
  /** ISO-8601 UTC of the observation behind this line, or null. */
  sampledAt: string | null;
  reason: string | null;
  unscopedLegacyContainers: number;
  duplicateSandboxId: boolean;
}

export interface SandboxSummary {
  /** The cell's text: a total, a lower bound, per-environment counts, or a state. */
  text: string;
  /** True when `text` is not a complete total for this server. */
  partial: boolean;
  /** Bounded identifiers behind a partial or unavailable result. */
  reasons: string[];
  /** A sentence explaining a client-side decision the reasons cannot state. */
  explanation: string | null;
  /** Every contributing enrollment, newest first, for the expanded panel. */
  perSource: SandboxSourceLine[];
  /**
   * The enrollments whose counts are inside `text`. Empty when nothing was
   * counted, which is what tells the caller that a number on screen is a last
   * observation rather than a current total.
   */
  contributingHosts: string[];
  /**
   * True when every report behind `text` is stale: the number is the last
   * thing measured, and the caller must not draw it as the current count.
   */
  staleOnly: boolean;
  /**
   * True when this server's coverage is incomplete because an observation has
   * expired — either the counts shown are themselves the last observation, or
   * an environment this server holds is no longer being reported on. A stale
   * report that a current one already covers is neither: it is a superseded
   * duplicate of the same Docker host, not something missing.
   */
  stale: boolean;
}

interface Tally {
  running: number;
  stopped: number;
  other: Record<string, number>;
}

interface InventoryReport {
  host: string;
  capacity: AgentHostCapacity;
  inventory: AgentHostCapacityInventory;
}

function emptyTally(): Tally {
  return { running: 0, stopped: 0, other: {} };
}

/**
 * Counts a report is willing to stand behind.
 *
 * A null count contributes nothing, which makes the sum a lower bound rather
 * than a wrong total — the caller marks it partial for exactly that reason.
 */
function tallyOf(inventory: AgentHostCapacityInventory): Tally {
  const tally = emptyTally();
  tally.running += inventory.running ?? 0;
  tally.stopped += inventory.stopped ?? 0;
  for (const state of OTHER_CONTAINER_STATES) {
    tally.other[state] = inventory.other_states?.[state] ?? 0;
  }
  return tally;
}

function addTally(into: Tally, from: Tally): void {
  into.running += from.running;
  into.stopped += from.stopped;
  for (const state of OTHER_CONTAINER_STATES) {
    into.other[state] = (into.other[state] ?? 0) + (from.other[state] ?? 0);
  }
}

/** `3 running · 7 stopped · 1 paused`; zero states are left out, running and
 * stopped are always named. */
function countPhrase(tally: Tally): string {
  const parts = [`${tally.running} running`, `${tally.stopped} stopped`];
  for (const state of OTHER_CONTAINER_STATES) {
    const count = tally.other[state] ?? 0;
    if (count > 0) parts.push(`${count} ${state}`);
  }
  return parts.join(' · ');
}

function tallyTotal(tally: Tally): number {
  let total = tally.running + tally.stopped;
  for (const state of OTHER_CONTAINER_STATES) total += tally.other[state] ?? 0;
  return total;
}

function observedAtMs(capacity: AgentHostCapacity): number {
  const sampled = Date.parse(capacity.sampled_at);
  return Number.isFinite(sampled) ? sampled : 0;
}

/**
 * Whether a current report proves an expired one measured the same host.
 *
 * Both halves of the identity are required, and the failure direction is
 * deliberate: when they cannot be established the expired report is *not*
 * covered, so the row keeps saying its coverage is incomplete. Assuming
 * sameness would let a machine that has stopped reporting disappear into a
 * neighbour's namespace.
 */
function provesSameHost(expired: InventoryReport, current: InventoryReport): boolean {
  const namespace = expired.inventory.namespace?.trim() ?? '';
  if (namespace === '') return false;
  if ((current.inventory.namespace?.trim() ?? '') !== namespace) return false;
  const bootId = expired.capacity.linux_boot_id;
  return bootId !== null && bootId !== undefined && bootId === current.capacity.linux_boot_id;
}

/** The later report of two, by sampled instant, then by receipt. */
function isNewer(candidate: InventoryReport, existing: InventoryReport): boolean {
  const bySample = observedAtMs(candidate.capacity) - observedAtMs(existing.capacity);
  if (bySample !== 0) return bySample > 0;
  return Date.parse(candidate.capacity.received_at) > Date.parse(existing.capacity.received_at);
}

/** Every count slot of a report, whether or not the report filled it in. */
function countValues(inventory: AgentHostCapacityInventory): (number | null)[] {
  return [
    inventory.running,
    inventory.stopped,
    ...OTHER_CONTAINER_STATES.map((state) => inventory.other_states?.[state] ?? null),
  ];
}

/**
 * One report's counts, in the words that report's own completeness allows.
 *
 * `countPhrase` formats a sum, where an unknown count contributes nothing and
 * the total is explicitly a lower bound. Using it for a *single* report prints
 * that same unknown as a zero — "3 running · 0 stopped" for a listing that
 * never got as far as counting the stopped ones — which is a specific claim
 * where there was none. Here the unknowns decide the shape of the sentence:
 * everything counted gets the full breakdown, something counted gets a lower
 * bound, nothing counted says so.
 */
function countsPhrase(inventory: AgentHostCapacityInventory): string {
  if (inventory.status === 'unavailable') return NOT_COUNTED;
  const values = countValues(inventory);
  if (values.every((value) => value === null)) return NOT_COUNTED;
  if (values.every((value) => value !== null)) return countPhrase(tallyOf(inventory));
  return `≥${values.reduce<number>((sum, value) => sum + (value ?? 0), 0)} observed`;
}

/** What a source says when it established no count at all. */
const NOT_COUNTED = 'Not counted';

function sourceLine(report: InventoryReport): SandboxSourceLine {
  return {
    host: report.host,
    namespace: report.inventory.namespace,
    status: report.inventory.status,
    counts: countsPhrase(report.inventory),
    sampledAt: report.capacity.sampled_at,
    reason: report.inventory.reason,
    unscopedLegacyContainers: report.inventory.unscoped_legacy_containers ?? 0,
    duplicateSandboxId: report.inventory.duplicate_sandbox_id,
  };
}

/**
 * One physical server's retained-sandbox count, merged across the enrollment
 * rows that share its `labels.physical_host`.
 *
 * The merging rules are the whole of this function, and each exists because
 * the obvious alternative states a number nobody measured:
 *
 *   * the same namespace reported by several enrollment rows is one Docker
 *     host observed several times, so only the newest report is counted — the
 *     default host deployment runs three runner replicas against one
 *     enrollment, and adding them would triple every machine;
 *   * only explicitly named, distinct namespaces may be added;
 *   * a report with no boot id cannot be proven to describe the same Docker
 *     host as another, so the per-environment counts are listed instead of a
 *     sum that might be double;
 *   * a report with no measurement at all — an older runner, a host that has
 *     only just enrolled — leaves the total a lower bound rather than letting
 *     the machines that did report look like the whole server;
 *   * any partial, unavailable or unscoped contributor makes the whole a
 *     lower bound, and the reasons travel with it;
 *   * a stale report is not part of the current count at all. It describes a
 *     machine as it was an hour ago — a container that has since been stopped
 *     is still in it, and one started since is not — so counting it would
 *     state a total nobody measured. It is listed per source, with its time,
 *     and it makes the coverage incomplete.
 *
 * Args:
 *   members: The enrollment rows grouped under one physical name.
 *   isStale: Whether a member's observation has expired, by the server's own
 *     age plus however long ago that answer arrived. Passed in rather than
 *     read off `freshness` so that one definition of "stale" serves the row,
 *     the panel and this count.
 */
export function summarizeSandboxes(
  members: AgentRunnerHost[],
  isStale: (member: AgentRunnerHost) => boolean = () => false,
): SandboxSummary {
  const reports: InventoryReport[] = [];
  for (const member of members) {
    const capacity = member.capacity;
    const inventory = capacity?.inventory;
    if (capacity && inventory) reports.push({ host: member.host, capacity, inventory });
  }
  // Enrollments that hold containers this server cannot see, so their silence
  // bounds the total from below.
  //
  // A *reporting* enrollment that sends no inventory is one of those: a runner
  // running code older than the collector, alive on this machine and unable to
  // describe it. A row that is neither reporting nor ever sent an observation
  // is not: it is a record of a machine that used to answer under this name —
  // what a rename leaves behind — and it holds nothing this page can be short
  // of. Counting it made every server that had ever been renamed report a
  // lower bound forever, which is a claim about coverage that nobody made.
  const observed = members.filter((member) => member.capacity !== null || isReporting(member));
  const unreported = observed.length - reports.length;
  const perSource = [...reports]
    .sort((a, b) => observedAtMs(b.capacity) - observedAtMs(a.capacity))
    .map(sourceLine);
  const staleHosts = new Set(
    members.filter((member) => isStale(member)).map((member) => member.host),
  );
  const fresh = reports.filter((report) => !staleHosts.has(report.host));
  const stale = reports.filter((report) => staleHosts.has(report.host));

  if (reports.length === 0) {
    const measured = members.some((member) => member.capacity !== null);
    return {
      text: measured ? 'Unavailable' : 'Not measured',
      partial: measured,
      reasons: [],
      explanation: measured
        ? 'A capacity observation arrived without a container inventory.'
        : 'No capacity observation has arrived from this server.',
      perSource,
      contributingHosts: [],
      staleOnly: false,
      stale: false,
    };
  }

  const byNamespace = new Map<string, InventoryReport>();
  const unscoped: InventoryReport[] = [];
  for (const report of fresh) {
    const namespace = report.inventory.namespace?.trim() ?? '';
    if (!namespace) {
      unscoped.push(report);
      continue;
    }
    const existing = byNamespace.get(namespace);
    if (!existing || isNewer(report, existing)) byNamespace.set(namespace, report);
  }
  // Newest first, so the panel lists the freshest report at the top.
  const contributing = [...byNamespace.values()].sort(
    (a, b) => observedAtMs(b.capacity) - observedAtMs(a.capacity),
  );

  const reasons = new Set<string>();
  for (const report of reports) {
    if (report.inventory.reason) reasons.add(report.inventory.reason);
    if (report.inventory.duplicate_sandbox_id) reasons.add('duplicate_sandbox_id');
    if ((report.inventory.unscoped_legacy_containers ?? 0) > 0) {
      reasons.add('unscoped_legacy_containers');
    }
  }
  if (unscoped.length > 0) {
    reasons.add('namespace_unset');
    reasons.add('unscoped_legacy_containers');
  }
  if (unreported > 0) reasons.add('inventory_not_reported');
  if (stale.length > 0) reasons.add('source_stale');

  // An expired report means something is missing unless a current report
  // *proves* it describes the same environment. The default deployment runs
  // three replicas against one namespace, so the usual "stale" report is a
  // superseded duplicate of one that is right here, and treating that as
  // incomplete coverage would put "Partial" on every server whose replicas
  // beat at slightly different moments.
  //
  // Proving it takes both halves of the identity. A missing namespace cannot
  // name an environment at all, and a namespace on its own does not survive a
  // rebuilt machine: the same name on a different boot id is a different
  // Docker host, and an unknown boot id is not evidence of anything. Anything
  // short of that stays visible as incomplete coverage rather than being
  // quietly deduped away — over-reporting a gap is recoverable, hiding one is
  // not.
  const missing = stale.filter(
    (report) => !contributing.some((current) => provesSameHost(report, current)),
  );
  if (missing.length > 0) reasons.add('source_stale');

  const counted = contributing.length > 0 ? contributing : unscoped;
  const countable = counted.filter((report) => report.inventory.status !== 'unavailable');
  // A report that came back without a single count is not a zero and not a
  // total: nothing here can be rendered as the current count of anything.
  const established = countable.filter((report) => countsPhrase(report.inventory) !== NOT_COUNTED);
  if (established.length === 0) {
    // Nothing current was counted. The newest expired report that actually
    // established a count is still worth showing — an operator wants to know
    // what the server last looked like — but never as the current total, and
    // never with counts that were never taken.
    const last = newestWithCounts(stale);
    if (last !== null) {
      return {
        text: countsPhrase(last.inventory),
        partial: true,
        reasons: [...reasons],
        explanation:
          `The newest observation for this server is stale. This is what ${last.host} ` +
          'last reported, not the current count.',
        perSource,
        // The counts in `text` do belong to this host, so the row's age is
        // this host's age. `staleOnly` is what says they are not current.
        contributingHosts: [last.host],
        staleOnly: true,
        stale: true,
      };
    }
    return {
      text: 'Unavailable',
      partial: true,
      reasons: [...reasons],
      explanation: 'No source established a count for this server.',
      perSource,
      contributingHosts: [],
      staleOnly: false,
      stale: false,
    };
  }

  const countedHosts = countable.map((report) => report.host);
  const bootIds = new Set(
    countable
      .map((report) => report.capacity.linux_boot_id)
      .filter((bootId): bootId is string => Boolean(bootId)),
  );
  const unknownBootId = countable.some((report) => !report.capacity.linux_boot_id);
  const summable = countable.length === 1 || (!unknownBootId && bootIds.size === 1);

  if (!summable) {
    reasons.add('sources_not_summed');
    return {
      text: countable
        .map(
          (report) =>
            `${report.inventory.namespace ?? report.host} ${countsPhrase(report.inventory)}`,
        )
        .join(' | '),
      partial: true,
      reasons: [...reasons],
      explanation:
        'These sources cannot be proven to describe the same Docker host, so their counts are' +
        ' listed per environment rather than added together.',
      perSource,
      contributingHosts: countedHosts,
      staleOnly: false,
      stale: missing.length > 0,
    };
  }

  const total = emptyTally();
  for (const report of countable) addTally(total, tallyOf(report.inventory));
  // A total is complete only when every report that could be attributed was
  // counted in full and nothing was left outside the sum — including anything
  // that has since expired, which is outside it by definition.
  const complete =
    unscoped.length === 0 &&
    unreported === 0 &&
    missing.length === 0 &&
    countable.length === contributing.length &&
    countable.every((report) => report.inventory.status === 'available') &&
    reports.every((report) => (report.inventory.unscoped_legacy_containers ?? 0) === 0);
  if (complete) {
    return {
      text: countPhrase(total),
      partial: false,
      reasons: [],
      explanation: null,
      perSource,
      contributingHosts: countedHosts,
      staleOnly: false,
      stale: false,
    };
  }
  return {
    text: `≥${tallyTotal(total)} observed · Partial`,
    partial: true,
    reasons: [...reasons],
    explanation: staleExplanations(missing, unreported, countable, contributing, unscoped),
    perSource,
    contributingHosts: countedHosts,
    staleOnly: false,
    // The row is missing coverage from an environment whose last reading has
    // expired: what is on screen is current, but it is not the whole server.
    stale: missing.length > 0,
  };
}

/**
 * The expired report with a real count that was sampled most recently.
 *
 * Ordered by observation time, not by array position: the two are different
 * facts, and pairing the first element's counts with the newest element's age
 * is how a row ends up describing two machines at once. Reports whose listing
 * failed are skipped — their counts are null, and a null is not a zero.
 */
function newestWithCounts(reports: InventoryReport[]): InventoryReport | null {
  const usable = reports.filter((report) => countsPhrase(report.inventory) !== NOT_COUNTED);
  if (usable.length === 0) return null;
  return [...usable].sort((a, b) => observedAtMs(b.capacity) - observedAtMs(a.capacity))[0];
}

/** Why a total came out a lower bound, in the words the panel shows. */
function staleExplanations(
  stale: InventoryReport[],
  unreported: number,
  counted: InventoryReport[],
  contributing: InventoryReport[],
  unscoped: InventoryReport[],
): string | null {
  if (stale.length > 0) {
    const names = stale.map((report) => report.host).join(', ');
    return (
      `Expired observations are excluded from the total: ${names}. ` +
      'Their last counts are listed per source below.'
    );
  }
  if (unreported > 0 || counted.length !== contributing.length || unscoped.length > 0) {
    return 'The count is a lower bound: at least one source could not count everything.';
  }
  return null;
}

// ── One physical server's capacity facts ───────────────────────────────

export interface HostGroupCapacity {
  /** The enrollment whose observation the row's CPU and memory cells show. */
  source: AgentRunnerHost | null;
  capacity: AgentHostCapacity | null;
  /** Age of the oldest contributing observation, at the pool read instant. */
  oldestSampleAgeSeconds: number | null;
  /** True when any contributing observation is stale. */
  stale: boolean;
  sandboxes: SandboxSummary;
}

/** A host that has an observation, so its capacity is never null below. */
type MeasuredHost = AgentRunnerHost & { capacity: AgentHostCapacity };

/**
 * The freshest fresh observation in the group.
 *
 * CPU and memory describe the Docker host, not an executor, so one member's
 * numbers stand for the whole physical machine — picked, never averaged or
 * summed. A stale reading is used only when it is all there is; the age column
 * says so either way.
 */
function capacitySource(reports: MeasuredHost[]): MeasuredHost | null {
  return (
    [...reports].sort(
      (a, b) =>
        Number(b.capacity.freshness === 'fresh') - Number(a.capacity.freshness === 'fresh') ||
        observedAtMs(b.capacity) - observedAtMs(a.capacity) ||
        a.host.localeCompare(b.host),
    )[0] ?? null
  );
}

/**
 * Every fact the row and its expanded panel need, derived in one place.
 *
 * `elapsedMs` is how long ago the pool answer arrived, by the browser's
 * monotonic clock. It is what makes staleness a live property: the server's
 * `freshness` describes the moment it answered, and a page that has been open
 * — or that has been failing to poll — goes on showing a reading that expired
 * some time ago. Without it the age ticks up in the Observed column while the
 * stale marker never appears, which is the one combination that reads as
 * current.
 */
export function groupCapacityFacts(members: AgentRunnerHost[], elapsedMs = 0): HostGroupCapacity {
  const measured = members.filter((member): member is MeasuredHost => member.capacity !== null);
  const isStale = (member: AgentRunnerHost): boolean => capacityIsStale(member.capacity, elapsedMs);
  const fresh = measured.filter((member) => !isStale(member));
  const source = capacitySource(fresh) ?? capacitySource(measured);
  const sandboxes = summarizeSandboxes(members, isStale);
  // The age is the oldest of what the row actually shows: the CPU and memory
  // source, plus the sources whose counts are in the sandbox cell. A source
  // that was deduped away or has expired is not one of them — letting it set
  // the age would make the row look older than anything on it.
  const ingredients = new Set(sandboxes.contributingHosts);
  if (source !== null) ingredients.add(source.host);
  const ages = measured
    .filter((member) => ingredients.has(member.host))
    .map((member) => member.capacity.sample_age_seconds)
    .filter((age) => Number.isFinite(age))
    .map((age) => Math.max(0, age));
  return {
    source,
    capacity: source?.capacity ?? null,
    // The oldest ingredient decides: a fresh CPU sample must not pass an
    // inventory from ten minutes ago off as having just been collected.
    oldestSampleAgeSeconds: ages.length === 0 ? null : Math.max(...ages),
    // Expired data on this row, in either of the two shapes it takes: the
    // counts shown are a last observation, or an environment this server
    // holds has stopped reporting. A stale report that a current one already
    // covers is a superseded duplicate of the same Docker host and is neither
    // — the sandbox summary is where that distinction is made, because it is
    // the only place that knows which reports were deduped away.
    stale: sandboxes.stale || (source !== null && isStale(source)),
    sandboxes,
  };
}

// ── Age ────────────────────────────────────────────────────────────────

/**
 * How old an observation may be before the page stops calling it current.
 *
 * The control plane's own threshold (`FRESH_AFTER_SECONDS` in
 * `cloud_agent/capacity_protocol.py`). Mirrored rather than re-derived: the
 * two answer the same question and must not drift, or a host would be stale on
 * one side of the wire and fresh on the other.
 */
export const CAPACITY_FRESH_SECONDS = 90;

/**
 * Whether an observation has expired by now, rather than when it was read.
 *
 * The server's `freshness` is true as of its answer and stays true on screen
 * however long ago that was. Adding locally observed monotonic elapsed time to
 * the age it reported is what keeps a page that stopped polling from going on
 * presenting an old reading as a current one.
 */
export function capacityIsStale(capacity: AgentHostCapacity | null, elapsedMs: number): boolean {
  if (capacity === null) return false;
  if (capacity.freshness === 'stale') return true;
  const age = capacity.sample_age_seconds + Math.max(0, elapsedMs) / 1000;
  return Number.isFinite(age) && age > CAPACITY_FRESH_SECONDS;
}

/**
 * A monotonic instant, in milliseconds.
 *
 * `performance.now()` is the source; the `Date.now()` fallback exists only for
 * an environment without it and is never mixed with it inside one comparison.
 */
export function monotonicNow(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now();
}

/**
 * An age as it stands now: the server's figure plus locally observed time.
 *
 * `elapsedMs` is a difference of two `monotonicNow()` readings. Recomputing
 * the age from `sampled_at` and the browser's wall clock would let a backwards
 * clock jump make an hour-old reading look new again, which is the one failure
 * an operator watching a sick machine must not have to notice.
 */
export function liveAgeSeconds(sampleAgeSeconds: number, elapsedMs: number): number {
  const base = Number.isFinite(sampleAgeSeconds) ? Math.max(0, sampleAgeSeconds) : 0;
  const elapsed = Number.isFinite(elapsedMs) ? Math.max(0, elapsedMs) : 0;
  return base + elapsed / 1000;
}

// ── UTC time ───────────────────────────────────────────────────────────

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

interface UtcParts {
  date: string;
  time: string;
}

/** UTC calendar parts, or null for an absent or unparsable instant. */
export function utcParts(iso: string | null): UtcParts | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  const at = new Date(ms);
  return {
    date: `${at.getUTCFullYear()}-${pad(at.getUTCMonth() + 1)}-${pad(at.getUTCDate())}`,
    time: `${pad(at.getUTCHours())}:${pad(at.getUTCMinutes())}`,
  };
}

/**
 * `2026-09-11 12:34 UTC`.
 *
 * Formatted from the instant's UTC parts rather than sliced out of the string:
 * the server may spell the same moment with a `Z` or a `+00:00` offset, and
 * only one of those survives a `slice`. The zone is always spelled out — an
 * admin comparing this page with a machine's own logs must not have to guess.
 */
export function formatUtcTimestamp(iso: string | null): string {
  const parts = utcParts(iso);
  return parts === null ? '' : `${parts.date} ${parts.time} UTC`;
}

/** Axis tick: `12:34` for an hour, `09-11 12:34` for the longer windows. */
export function formatUtcAxisTick(iso: string, range: CapacityRange): string {
  const parts = utcParts(iso);
  if (parts === null) return '';
  return range === '1h' ? parts.time : `${parts.date.slice(5)} ${parts.time}`;
}

/** The selector's label for a range. */
export function rangeLabel(range: CapacityRange): string {
  return range === '1h' ? 'Last 1h' : range === '6h' ? 'Last 6h' : 'Last 24h';
}

/** The windows the panel offers, shortest first. */
export const CAPACITY_RANGES: CapacityRange[] = ['1h', '6h', '24h'];

// ── Series ─────────────────────────────────────────────────────────────

export type CapacityMetric = 'cpu' | 'memory';

export interface CapacitySeriesPoint {
  /** ISO-8601 UTC; the start of the minute this point belongs to. */
  bucketAt: string;
  /** ISO-8601 UTC of the observation, or null for a minute nobody observed. */
  sampledAt: string | null;
  /** The percentage the curve plots, or null where there is nothing to plot. */
  value: number | null;
  /**
   * Points are joined only while this is unchanged: a reboot, a CPU hotplug
   * or a memory resize makes the series a different machine's, and a line
   * across it would be a claim nobody made.
   */
  groupKey: string | null;
  /** An extra fact for the tooltip, e.g. `42 / 128 GiB`. */
  detail: string | null;
}

/** The points one chart plots, from the history the server returned. */
export function capacitySeries(
  points: AgentHostCapacityPoint[],
  metric: CapacityMetric,
): CapacitySeriesPoint[] {
  return points.map((point) => {
    if (metric === 'cpu') {
      return {
        bucketAt: point.bucket_at,
        sampledAt: point.sampled_at,
        value: point.cpu_percent,
        groupKey: `${point.linux_boot_id ?? ''}|${point.cpu_logical_count ?? ''}`,
        detail: null,
      };
    }
    return {
      bucketAt: point.bucket_at,
      sampledAt: point.sampled_at,
      value: point.memory_used_percent,
      groupKey: `${point.linux_boot_id ?? ''}|${point.memory_total_bytes ?? ''}`,
      detail: formatGibPair(point.memory_used_bytes, point.memory_total_bytes),
    };
  });
}

export function isObservedPoint(point: CapacitySeriesPoint): boolean {
  return (
    point.sampledAt !== null &&
    point.value !== null &&
    Number.isFinite(point.value) &&
    Number.isFinite(Date.parse(point.bucketAt))
  );
}

/**
 * The tooltip for one observed point: `2026-09-11 12:34 UTC · CPU 37.2%`.
 *
 * The instant shown is the observation's own, not the bucket's: the server
 * keeps the real sampled time so that a minute's number is never presented as
 * having been taken exactly on the minute.
 */
export function formatPointTooltip(metric: CapacityMetric, point: CapacitySeriesPoint): string {
  const stamp = formatUtcTimestamp(point.sampledAt ?? point.bucketAt);
  const label = metric === 'cpu' ? 'CPU' : 'Memory';
  const value = formatPercentTenth(point.value) ?? '';
  const detail = metric === 'memory' && point.detail ? ` · ${point.detail}` : '';
  return `${stamp} · ${label} ${value}${detail}`;
}

// ── Chart geometry ─────────────────────────────────────────────────────

/** SVG user units. The chart scales to its container; these set the aspect. */
export interface ChartView {
  width: number;
  height: number;
  left: number;
  right: number;
  top: number;
  bottom: number;
}

// Flat on purpose: the y axis is fixed at 0-100, so a machine at 8% draws near
// the baseline and everything above it is the empty headroom that says so. A
// tall box turns that headroom into most of the panel and reads as a chart that
// failed to load. `bottom` clears the tick labels and the axis title under
// them; `left` clears "100%", the widest thing in the y-axis label column.
export const CHART_VIEW: ChartView = {
  width: 520,
  height: 148,
  left: 44,
  right: 12,
  top: 8,
  bottom: 30,
};

export const CHART_PLOT_WIDTH = CHART_VIEW.width - CHART_VIEW.left - CHART_VIEW.right;
export const CHART_PLOT_HEIGHT = CHART_VIEW.height - CHART_VIEW.top - CHART_VIEW.bottom;
/** Y grid lines; 0-100 is fixed so two charts can be read against each other. */
export const CHART_Y_TICKS = [0, 25, 50, 75, 100];

/** Where an instant falls on the axis. The axis spans `since`..`until`, so a
 * gap occupies its true width instead of being closed up. */
export function chartXForTime(atMs: number, sinceMs: number, untilMs: number): number {
  if (!Number.isFinite(atMs) || !Number.isFinite(sinceMs) || !Number.isFinite(untilMs)) {
    return CHART_VIEW.left;
  }
  if (untilMs <= sinceMs) return CHART_VIEW.left;
  const ratio = Math.min(1, Math.max(0, (atMs - sinceMs) / (untilMs - sinceMs)));
  return CHART_VIEW.left + ratio * CHART_PLOT_WIDTH;
}

/** Where a percentage falls vertically. Fixed 0-100: a value is drawn where it
 * is, not where it ranks among the window's values. */
export function chartYForValue(value: number): number {
  const ratio = Math.min(1, Math.max(0, value / 100));
  return CHART_VIEW.top + (1 - ratio) * CHART_PLOT_HEIGHT;
}

export interface PlottedPoint {
  point: CapacitySeriesPoint;
  value: number;
  x: number;
  y: number;
}

export interface ChartGeometry {
  /** Every observed point, in time order; the keyboard and pointer walk this. */
  points: PlottedPoint[];
  /** Contiguous runs of observed points. Each run is one polyline. */
  segments: PlottedPoint[][];
  plotLeft: number;
  plotRight: number;
  plotTop: number;
  plotBottom: number;
}

/**
 * Map a series onto the chart, splitting it wherever the record breaks.
 *
 * A break is any of: a minute nobody observed, a value the source could not
 * measure, a jump in the bucket grid larger than the resolution (the server
 * fills every bucket, so this only happens on a truncated response), or a
 * change of `groupKey` — a reboot or a resized machine. Nothing here
 * interpolates: a break is a break, and the chart draws two lines rather than
 * one that crosses a period it has no data for.
 */
export function chartGeometry(
  series: CapacitySeriesPoint[],
  sinceIso: string,
  untilIso: string,
  resolutionSeconds = 60,
): ChartGeometry {
  const sinceMs = Date.parse(sinceIso);
  const untilMs = Date.parse(untilIso);
  const points: PlottedPoint[] = [];
  const segments: PlottedPoint[][] = [];
  let current: PlottedPoint[] = [];
  let previousAtMs: number | null = null;
  let previousGroup: string | null = null;

  const flush = (): void => {
    if (current.length > 0) segments.push(current);
    current = [];
  };

  for (const point of series) {
    const atMs = Date.parse(point.bucketAt);
    if (!isObservedPoint(point)) {
      flush();
      previousAtMs = null;
      previousGroup = null;
      continue;
    }
    const value = point.value as number;
    const jumped = previousAtMs !== null && atMs - previousAtMs > resolutionSeconds * 1500;
    const changedHost =
      previousGroup !== null && point.groupKey !== null && point.groupKey !== previousGroup;
    if (jumped || changedHost) flush();
    const plotted: PlottedPoint = {
      point,
      value,
      x: chartXForTime(atMs, sinceMs, untilMs),
      y: chartYForValue(value),
    };
    current.push(plotted);
    points.push(plotted);
    previousAtMs = atMs;
    previousGroup = point.groupKey;
  }
  flush();

  return {
    points,
    segments,
    plotLeft: CHART_VIEW.left,
    plotRight: CHART_VIEW.left + CHART_PLOT_WIDTH,
    plotTop: CHART_VIEW.top,
    plotBottom: CHART_VIEW.top + CHART_PLOT_HEIGHT,
  };
}

/** The observed point nearest a position on the axis, or -1 for none. */
export function nearestPointIndex(points: PlottedPoint[], x: number): number {
  let nearest = -1;
  let best = Number.POSITIVE_INFINITY;
  for (let index = 0; index < points.length; index += 1) {
    const distance = Math.abs(points[index].x - x);
    if (distance < best) {
      best = distance;
      nearest = index;
    }
  }
  return nearest;
}

/**
 * A pointer's x in SVG user units.
 *
 * The chart keeps its aspect ratio, so a container whose shape differs from
 * the viewBox's letterboxes the drawing: the scale is the smaller of the two
 * ratios and the drawing is centred on the other axis. Reading the scale off
 * the width alone would put every tooltip in the wrong place as soon as the
 * panel is wide, which is the only time it is read.
 */
export function svgXFromClientX(
  rect: { left: number; width: number; height: number },
  clientX: number,
): number {
  if (!(rect.width > 0) || !(rect.height > 0)) return CHART_VIEW.left;
  const scale = Math.min(rect.width / CHART_VIEW.width, rect.height / CHART_VIEW.height);
  if (!(scale > 0)) return CHART_VIEW.left;
  const offset = (rect.width - CHART_VIEW.width * scale) / 2;
  const x = (clientX - rect.left - offset) / scale;
  return Math.min(CHART_VIEW.width, Math.max(0, x));
}

export interface ChartGap {
  key: string;
  startX: number;
  endX: number;
  /** Buckets in the run. */
  minutes: number;
}

/**
 * Unobserved runs *inside* the observed range, for the `No data` annotation.
 *
 * A span runs from the last observation before the hole to the first one after
 * it — the stretch of axis with no line on it, which is what the label names.
 *
 * Leading and trailing empty buckets are deliberately not gaps: they are where
 * collection had not started, or has stopped, and the panel calls those
 * `History is accumulating` and `stale`. Calling them "no data" would name the
 * chart's own edge the same as an outage in the middle of it.
 */
export function gapSpans(
  series: CapacitySeriesPoint[],
  sinceIso: string,
  untilIso: string,
): ChartGap[] {
  const first = series.findIndex(isObservedPoint);
  if (first < 0) return [];
  let last = -1;
  for (let index = series.length - 1; index >= 0; index -= 1) {
    if (isObservedPoint(series[index])) {
      last = index;
      break;
    }
  }
  const sinceMs = Date.parse(sinceIso);
  const untilMs = Date.parse(untilIso);
  const xAt = (index: number): number =>
    chartXForTime(Date.parse(series[index].bucketAt), sinceMs, untilMs);
  const gaps: ChartGap[] = [];
  let runStart = -1;
  for (let index = first; index <= last; index += 1) {
    if (isObservedPoint(series[index])) {
      if (runStart >= 0) {
        gaps.push({
          key: series[runStart].bucketAt,
          startX: xAt(runStart - 1),
          endX: xAt(index),
          minutes: index - runStart,
        });
        runStart = -1;
      }
      continue;
    }
    if (runStart < 0) runStart = index;
  }
  if (runStart >= 0) {
    gaps.push({
      key: series[runStart].bucketAt,
      startX: xAt(runStart - 1),
      endX: xAt(last),
      minutes: last - runStart + 1,
    });
  }
  return gaps;
}

export function observedPointCount(series: CapacitySeriesPoint[]): number {
  return series.reduce((count, point) => count + (isObservedPoint(point) ? 1 : 0), 0);
}

/** The highest value observed in the window — never called a peak. */
export function highestObserved(series: CapacitySeriesPoint[]): number | null {
  let highest: number | null = null;
  for (const point of series) {
    if (!isObservedPoint(point)) continue;
    const value = point.value as number;
    if (highest === null || value > highest) highest = value;
  }
  return highest;
}

/**
 * True when the window reaches back further than the record does.
 *
 * Either nothing has been observed yet, or the earliest observation is more
 * than one bucket after the window opened — which is what a deployment that
 * started ten minutes ago answers to a request for a day. The panel says
 * `History is accumulating` and draws the part that exists.
 */
export function historyIsAccumulating(
  series: CapacitySeriesPoint[],
  sinceIso: string,
  resolutionSeconds: number,
): boolean {
  const first = series.findIndex(isObservedPoint);
  if (first < 0) return true;
  const sinceMs = Date.parse(sinceIso);
  const firstMs = Date.parse(series[first].bucketAt);
  if (!Number.isFinite(sinceMs) || !Number.isFinite(firstMs)) return false;
  return firstMs - sinceMs > resolutionSeconds * 1000;
}

/** A sentence describing the curve, for a reader who cannot see it. */
export function chartSummaryText(
  metric: CapacityMetric,
  series: CapacitySeriesPoint[],
  sinceIso: string,
  untilIso: string,
): string {
  const label = metric === 'cpu' ? 'CPU usage' : 'Memory usage';
  const window = `${formatUtcTimestamp(sinceIso)} to ${formatUtcTimestamp(untilIso)}`;
  const observed = observedPointCount(series);
  if (observed === 0) return `${label} from ${window}: no observations in this range.`;
  const parts = [`${observed} of ${series.length} minutes observed`];
  const highest = highestObserved(series);
  if (highest !== null) parts.push(`highest observed ${formatPercentTenth(highest)}`);
  const gaps = gapSpans(series, sinceIso, untilIso).length;
  parts.push(gaps === 0 ? 'no gaps' : gaps === 1 ? '1 gap' : `${gaps} gaps`);
  return `${label} from ${window}: ${parts.join(', ')}.`;
}
