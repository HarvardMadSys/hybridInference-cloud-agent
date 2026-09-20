import { describe, expect, it } from 'vitest';

import type {
  AgentHostCapacity,
  AgentHostCapacityInventory,
  AgentHostCapacityPoint,
  AgentRunnerHost,
} from '@/lib/api/admin';

import {
  CAPACITY_FRESH_SECONDS,
  capacitySeries,
  CHART_VIEW,
  chartGeometry,
  chartSummaryText,
  chartXForTime,
  chartYForValue,
  concurrencyFacts,
  cpuCell,
  executionCeiling,
  executionSlotsCell,
  formatGib,
  formatGibPair,
  formatPercent,
  formatPercentCeil,
  formatPercentTenth,
  formatPointTooltip,
  formatStoragePair,
  formatStorageSize,
  formatUtcAxisTick,
  formatUtcTimestamp,
  gapSpans,
  groupCapacityFacts,
  highestObserved,
  historyIsAccumulating,
  liveAgeSeconds,
  memoryCell,
  missingReasonLabel,
  monotonicNow,
  nearestPointIndex,
  observedPointCount,
  reasonLabel,
  storageCell,
  summarizeSandboxes,
  svgXFromClientX,
} from './capacity';

const GIB = 1024 ** 3;
const TIB = 1024 ** 4;

function inventory(over: Partial<AgentHostCapacityInventory> = {}): AgentHostCapacityInventory {
  return {
    namespace: 'example-agent-kata',
    status: 'available',
    running: 3,
    stopped: 7,
    other_states: { created: 0, paused: 0, restarting: 0, removing: 0, dead: 0 },
    reason: null,
    unscoped_legacy_containers: 0,
    duplicate_sandbox_id: false,
    ...over,
  };
}

/**
 * An observation with no storage block, because that is what most of these
 * tests are about.
 *
 * The storage fields are left off rather than set to null on purpose: it is
 * the shape a control plane older than this client sends, and every caller
 * that does not care about storage is then also asserting that its absence
 * does not disturb CPU, memory or the sandbox counts.
 */
function capacity(over: Partial<AgentHostCapacity> = {}): AgentHostCapacity {
  return {
    schema_version: 1,
    source: 'linux_host_proc_and_docker_list',
    sample_id: 'sample-1',
    linux_boot_id: 'boot-a',
    cpu_percent: 37.24,
    cpu_logical_count: 32,
    cpu_window_seconds: 30,
    cpu_missing_reason: null,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 86 * GIB,
    memory_used_bytes: 42 * GIB,
    memory_used_percent: 32.8,
    memory_missing_reason: null,
    inventory: inventory(),
    received_at: '2026-09-11T12:34:31Z',
    sampled_at: '2026-09-11T12:34:01Z',
    sample_age_seconds: 30,
    freshness: 'fresh',
    ...over,
  };
}

/**
 * The same observation with a measured storage block on it.
 *
 * The three byte figures are mutually consistent the way the control plane's
 * derivation leaves them: used = total - free, and the 15 GiB between `free`
 * and `available` is the root reserve that is still free — counted by neither
 * figure, and the reason the pair does not add up to the total.
 */
function storageCapacity(over: Partial<AgentHostCapacity> = {}): AgentHostCapacity {
  return capacity({
    storage_total_bytes: 1000 * GIB,
    storage_free_bytes: 665 * GIB,
    storage_available_bytes: 650 * GIB,
    storage_used_bytes: 335 * GIB,
    storage_used_percent: 100 * (335 / (335 + 650)),
    storage_source: 'linux_overlay_root_statvfs',
    storage_target: '/mnt/agent-docker',
    storage_missing_reason: null,
    ...over,
  });
}

function host(over: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    host: 'runner-a',
    active: false,
    last_worker_id: null,
    first_seen_at: '2026-08-01T00:00:00Z',
    last_seen_at: '2026-09-11T12:34:31Z',
    seconds_since_seen: 12,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: [],
    slots_total: 3,
    slots_used: 2,
    runner_version: null,
    broker_url: null,
    last_heartbeat_at: null,
    sandbox_runtime: 'io.containerd.kata.v2',
    sandbox_image_digest: null,
    runtime_versions: null,
    runtime_versions_available: null,
    capacity_epoch: 'epoch-1',
    capacity: capacity(),
    ...over,
  };
}

const AT = (minute: number, second = 0): string =>
  new Date(Date.UTC(2026, 8, 11, 12, minute, second)).toISOString();

function point(
  minute: number,
  value: number | null,
  over: Partial<AgentHostCapacityPoint> = {},
): AgentHostCapacityPoint {
  return {
    bucket_at: AT(minute),
    sampled_at: value === null ? null : AT(minute, 30),
    linux_boot_id: 'boot-a',
    cpu_percent: value,
    cpu_logical_count: 32,
    cpu_window_seconds: 30,
    cpu_missing_reason: null,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 86 * GIB,
    memory_used_bytes: 42 * GIB,
    memory_used_percent: value,
    memory_missing_reason: null,
    ...over,
  };
}

describe('number formatting', () => {
  it('rounds a percentage for the table and keeps a tenth for a tooltip', () => {
    expect(formatPercent(37.24)).toBe('37%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(null)).toBeNull();
    expect(formatPercent(Number.NaN)).toBeNull();
    expect(formatPercentTenth(37.24)).toBe('37.2%');
    expect(formatPercentTenth(Number.NaN)).toBeNull();
  });

  it('rounds the storage figure up, the way df does', () => {
    // The direction is the point: a disk with a third of a percent free is
    // full to the reader deciding whether to clear space, and df says 100%.
    expect(formatPercentCeil(99.4)).toBe('100%');
    expect(formatPercentCeil(0.1)).toBe('1%');
    expect(formatPercentCeil(0)).toBe('0%');
    expect(formatPercentCeil(null)).toBeNull();
    expect(formatPercentCeil(Number.NaN)).toBeNull();
    // Where it disagrees with the nearest-rounding helper beside it.
    expect(formatPercent(74.2)).toBe('74%');
    expect(formatPercentCeil(74.2)).toBe('75%');
  });

  it('reports GiB at a precision a reader can act on', () => {
    expect(formatGib(128 * GIB)).toBe('128');
    expect(formatGib(42 * GIB)).toBe('42');
    expect(formatGib(1.5 * GIB)).toBe('1.5');
    expect(formatGib(null)).toBeNull();
    expect(formatGibPair(42 * GIB, 128 * GIB)).toBe('42 / 128 GiB');
    // Half an answer is not an answer.
    expect(formatGibPair(42 * GIB, null)).toBeNull();
  });

  it('translates a bounded missing reason, and refuses to invent one', () => {
    expect(missingReasonLabel('warming_up')).toBe('Warming up');
    expect(missingReasonLabel('counter_reset')).toBe('Counter reset');
    expect(missingReasonLabel(null)).toBe('Not measured');
    // An unknown identifier is a gap, not a licence to guess.
    expect(missingReasonLabel('something_new')).toBe('Not measured');
    expect(reasonLabel('docker_timeout')).toBe('The Docker container listing timed out');
    expect(reasonLabel('brand_new_reason')).toBe('brand_new_reason');
  });

  it('says why storage is absent, and which of the reasons need an operator', () => {
    // The two the page already had: a sample that could not be taken and one
    // that failed validation are both gaps, not faults to be explained.
    expect(missingReasonLabel('source_unavailable')).toBe('Not measured');
    expect(missingReasonLabel('invalid_sample')).toBe('Invalid sample');
    // The four storage adds. `disabled` and `unsupported` are decisions, and
    // `unverified` is work the deployment has not finished.
    expect(missingReasonLabel('not_reported')).toBe('Not measured');
    expect(missingReasonLabel('disabled')).toBe('Storage measurement disabled');
    expect(missingReasonLabel('unsupported')).toBe('Storage not supported on this host');
    expect(missingReasonLabel('unverified')).toBe('Storage not verified');
  });
});

describe('cells', () => {
  it('states the concurrency quota without inventing a ceiling', () => {
    const plain = { capabilities: ['host-concurrency-v1'], status: 'online' as const };
    expect(executionSlotsCell({ ...plain, slots_used: 2, slots_total: 3 })).toMatchObject({
      text: '2 / 3 in use',
      note: '1 free',
      title: null,
    });
    // Over-subscribed is clamped, never negative — and named as finishing,
    // because those turns were admitted before the limit came down.
    expect(executionSlotsCell({ ...plain, slots_used: 5, slots_total: 3 }).note).toBe(
      '0 free · 2 finishing',
    );
    // No report and no saved number: the running count is a fact, the free
    // count is not.
    expect(executionSlotsCell({ ...plain, slots_used: 2, slots_total: null })).toMatchObject({
      text: 'Limit not set',
      note: '2 in use',
    });
  });

  it('counts against what is enforced, never against what was merely saved', () => {
    const host = { capabilities: ['kata', 'host-concurrency-v1'], status: 'online' as const };
    // Saved below what the host reports: the lower number is the denominator
    // at once, before the host has even heard about it.
    expect(
      executionSlotsCell({ ...host, slots_used: 1, slots_total: 4, slots_desired: 2 }),
    ).toMatchObject({ text: '1 / 2 in use', note: '1 free · applying 2' });
    // Saved above: the host's own number still bounds it until it confirms.
    expect(
      executionSlotsCell({ ...host, slots_used: 1, slots_total: 2, slots_desired: 5 }),
    ).toMatchObject({ text: '1 / 2 in use', note: '1 free · applying 5' });
    // Confirmed: nothing to say beyond the numbers.
    expect(
      executionSlotsCell({ ...host, slots_used: 1, slots_total: 4, slots_desired: 4 }),
    ).toMatchObject({ text: '1 / 4 in use', note: '3 free', title: null });
    // Lowered while full: the surplus turns finish, they are not cut short.
    expect(
      executionSlotsCell({ ...host, slots_used: 5, slots_total: 5, slots_desired: 2 }),
    ).toMatchObject({ text: '5 / 2 in use', note: '0 free · applying 2 · 3 finishing' });
    // Nothing reported but a number saved: that number is the only bound.
    expect(
      executionSlotsCell({ ...host, slots_used: 1, slots_total: null, slots_desired: 3 }),
    ).toMatchObject({ text: '1 / 3 in use', note: '2 free · applying 3' });
  });

  it('separates a number a host will apply from one it never will', () => {
    // An offline host cannot confirm anything until it reports in.
    const offline = executionSlotsCell({
      capabilities: ['kata', 'host-concurrency-v1'],
      status: 'offline',
      slots_used: 0,
      slots_total: 3,
      slots_desired: 6,
    });
    expect(offline.note).toBe('3 free · 6 not applied');
    expect(offline.title).toContain('offline');

    // A runner that predates remote limits never will, and says so instead of
    // looking like a host that is merely slow to confirm.
    const legacy = executionSlotsCell({
      capabilities: ['kata'],
      status: 'online',
      slots_used: 0,
      slots_total: 3,
      slots_desired: 6,
    });
    expect(legacy.note).toBe('3 free · redeploy to change');
    expect(legacy.title).toContain('redeployed');
  });

  it('says when a host with room is claiming nothing, and why', () => {
    const stuck = executionSlotsCell({
      capabilities: ['kata', 'host-concurrency-v1'],
      status: 'online',
      slots_used: 0,
      slots_total: 3,
      slots_desired: null,
      launch_error: '3 turns in a row could not be started (image is broken)',
    });

    // Free capacity and no work is the state an operator most needs a reason
    // for; the reason is the host's own words, not an inference from silence.
    expect(stuck.note).toBe('3 free · not claiming');
    expect(stuck.title).toContain('image is broken');
  });

  it('reports the facts behind the cell separately', () => {
    const facts = concurrencyFacts({
      capabilities: ['kata', 'host-concurrency-v1'],
      status: 'online',
      slots_used: 5,
      slots_total: 5,
      slots_desired: 2,
      launch_error: '',
    });

    expect(facts).toMatchObject({
      state: 'pending',
      requested: 2,
      reported: 5,
      enforced: 2,
      running: 5,
      free: 0,
      finishing: 3,
      launchError: null,
    });
  });

  it('derives the enforced ceiling from the lower of the two counts', () => {
    expect(executionCeiling(4, 2)).toBe(2);
    expect(executionCeiling(2, 4)).toBe(2);
    expect(executionCeiling(null, 3)).toBe(3);
    expect(executionCeiling(3, null)).toBe(3);
    expect(executionCeiling(null, null)).toBeNull();
  });

  it('never renders an unmeasured CPU as zero', () => {
    expect(cpuCell(null).text).toBe('Not measured');
    expect(cpuCell(capacity({ cpu_percent: null, cpu_missing_reason: 'warming_up' })).text).toBe(
      'Warming up',
    );
    expect(cpuCell(capacity({ cpu_percent: null, cpu_missing_reason: 'counter_reset' })).text).toBe(
      'Counter reset',
    );
    expect(cpuCell(capacity({ cpu_percent: null, cpu_missing_reason: null })).text).toBe(
      'Not measured',
    );
    const cell = cpuCell(capacity());
    expect(cell.text).toBe('37%');
    expect(cell.note).toBe('32 logical CPUs');
    expect(cell.title).toContain('averaged over 30s');
  });

  it('states memory as a share and an absolute pair, with what is free', () => {
    const cell = memoryCell(capacity());
    expect(cell.text).toBe('33% · 42 / 128 GiB');
    expect(cell.note).toBe('86 GiB available');
    expect(memoryCell(null).text).toBe('Not measured');
    // A percentage without its absolute pair is still worth showing.
    const bare = memoryCell(capacity({ memory_used_bytes: null, memory_total_bytes: null }));
    expect(bare.text).toBe('33%');
    expect(bare.note).toBe('86 GiB available');
    expect(
      memoryCell(capacity({ memory_used_percent: null, memory_missing_reason: 'invalid_sample' }))
        .text,
    ).toBe('Invalid sample');
  });
});

describe('storage cell', () => {
  it('states the share, the pair and what is still writable', () => {
    const cell = storageCell(storageCapacity());
    // 100 × 335 / (335 + 650) = 34.01%, and the table rounds up where CPU and
    // memory round to nearest.
    expect(cell.text).toBe('35% · 335 / 1000 GiB');
    // 650 GiB, not 665: `available` excludes the root reserve that is still
    // free, and it is the number a writer can actually use.
    expect(cell.note).toBe('650 GiB available');
  });

  it('draws no cell at all when no observation has ever arrived', () => {
    const cell = storageCell(null);
    expect(cell).toEqual({
      text: 'Not measured',
      note: null,
      title: 'No capacity observation has arrived from this host.',
    });
  });

  it('tells a genuinely empty disk from a disk nobody measured', () => {
    // 0% is a reading: every byte of the denominator is free, and the cell
    // says so rather than falling back to a reason.
    const empty = storageCell(
      storageCapacity({
        storage_total_bytes: 500 * GIB,
        storage_free_bytes: 500 * GIB,
        storage_available_bytes: 500 * GIB,
        storage_used_bytes: 0,
        storage_used_percent: 0,
        storage_missing_reason: null,
      }),
    );
    // The used figure is a real zero and not a gap, which is the whole point.
    // It prints as `0` and not `0.0`: storage reads at formatGib's precision,
    // so a whole number of units carries no trailing tenth here any more than
    // memory's `42 / 128 GiB` does.
    expect(empty.text).toBe('0% · 0 / 500 GiB');
    expect(empty.note).toBe('500 GiB available');
    // The absence of a measurement is not that reading. This is the case an
    // older control plane produces, and `0%` here would be an empty disk
    // reported for a machine nobody asked about.
    const missing = storageCell(capacity({ storage_missing_reason: 'not_reported' }));
    expect(missing.text).toBe('Not measured');
    expect(missing.note).toBeNull();
    expect(missing.text).not.toBe('0%');
  });

  it('reads a control plane that predates the storage block as not measured', () => {
    // The UI ships independently of the collector: an older control plane
    // omits the keys entirely rather than sending nulls, so `undefined` has to
    // land on the same branch as null. A `0%` here would invent a full answer
    // from a field that was never sent.
    const legacy = capacity();
    expect(legacy.storage_used_percent).toBeUndefined();
    const cell = storageCell(legacy);
    expect(cell.text).toBe('Not measured');
    expect(cell.note).toBeNull();
    expect(cell.title).toBe('Storage is not measured: reason not reported.');
    // Half a payload is still not a reading: a percentage whose bytes did not
    // arrive renders as the percentage alone, as memory does.
    expect(storageCell(storageCapacity({ storage_used_bytes: undefined })).text).toBe('35%');
    expect(storageCell(storageCapacity({ storage_total_bytes: undefined })).note).toBe(
      '650 GiB available',
    );
  });

  it('names every missing reason rather than printing a zero', () => {
    const reasons: [AgentHostCapacity['storage_missing_reason'], string][] = [
      ['not_reported', 'Not measured'],
      ['disabled', 'Storage measurement disabled'],
      ['unsupported', 'Storage not supported on this host'],
      ['unverified', 'Storage not verified'],
      ['source_unavailable', 'Not measured'],
      ['invalid_sample', 'Invalid sample'],
    ];
    for (const [reason, label] of reasons) {
      const cell = storageCell(
        storageCapacity({ storage_used_percent: null, storage_missing_reason: reason }),
      );
      expect(cell.text).toBe(label);
      expect(cell.note).toBeNull();
      expect(cell.title).toBe(
        `Storage is not measured: ${reason}.${reason === 'unverified' ? ' The filesystem has not been verified as the one the retained sandboxes write to; a successful deployment verification would confirm it.' : ''}`,
      );
      expect(cell.text).not.toBe('0%');
    }
    // The reason is not a fact the wire requires; a null one still says the
    // right thing rather than showing an empty cell.
    expect(storageCell(storageCapacity({ storage_used_percent: null })).title).toBe(
      'Storage is not measured: reason not reported.',
    );
  });

  it('reads disks in GiB below a tebibyte and in TiB at it', () => {
    // 1023 GiB is still GiB — and reads as a fraction of a TiB, which is the
    // unit the next disk up will be read in.
    const below = storageCell(
      storageCapacity({
        storage_total_bytes: 1023 * GIB,
        storage_free_bytes: 512 * GIB,
        storage_available_bytes: 512 * GIB,
        storage_used_bytes: 511 * GIB,
        storage_used_percent: 100 * (511 / 1023),
      }),
    );
    expect(below.text).toBe('50% · 511 / 1023 GiB');
    expect(below.note).toBe('512 GiB available');
    // At 1 TiB the unit changes, and the boundary itself is the first TiB
    // reading rather than the last GiB one.
    const at = storageCell(
      storageCapacity({
        storage_total_bytes: TIB,
        storage_free_bytes: 512 * GIB,
        storage_available_bytes: 500 * GIB,
        storage_used_bytes: 512 * GIB,
        storage_used_percent: 100 * (512 / 1012),
      }),
    );
    expect(at.text).toBe('51% · 0.5 / 1 TiB');
    expect(at.note).toBe('500 GiB available');
    // Above ten units the tenth is noise, as it is for memory.
    const large = storageCell(
      storageCapacity({
        storage_total_bytes: 24 * TIB,
        storage_free_bytes: 6 * TIB,
        storage_available_bytes: 6 * TIB,
        storage_used_bytes: 18 * TIB,
        storage_used_percent: 75,
      }),
    );
    expect(large.text).toBe('75% · 18 / 24 TiB');
    expect(large.note).toBe('6 TiB available');
  });

  it('chooses the available figure’s unit independently of the total’s', () => {
    // A terabyte disk with 40 GiB left. In the total's unit that is `0 TiB`,
    // which reads as none — the reading an operator most needs to act on.
    const cell = storageCell(
      storageCapacity({
        storage_total_bytes: 2 * TIB,
        storage_free_bytes: 104 * GIB,
        storage_available_bytes: 40 * GIB,
        storage_used_bytes: 2 * TIB - 104 * GIB,
        storage_used_percent: 100 * ((2 * TIB - 104 * GIB) / (2 * TIB - 64 * GIB)),
      }),
    );
    expect(cell.text).toBe('98% · 1.9 / 2 TiB');
    expect(cell.note).toBe('40 GiB available');
    // The available figure is fitted to its own magnitude, not to 1 TiB: the
    // two formatters are not the same call with a different argument.
    expect(formatStorageSize(40 * GIB)).toBe('40 GiB');
    expect(formatStorageSize(1.5 * TIB)).toBe('1.5 TiB');
    expect(formatStorageSize(2 * TIB)).toBe('2 TiB');
    // A hair over a tenth still reads as that tenth: the figure is rounded to
    // the tenth once, so 1.2499 TiB is `1.2` and not `1.3`.
    expect(formatStorageSize(1.2499 * TIB)).toBe('1.2 TiB');
    expect(formatStorageSize(null)).toBeNull();
    expect(formatStorageSize(undefined)).toBeNull();
    expect(formatStorageSize(Number.NaN)).toBeNull();
  });

  it('keeps the pair in one unit so the two halves compare', () => {
    expect(formatStoragePair(335 * GIB, 1000 * GIB)).toBe('335 / 1000 GiB');
    // Below ten units a fractional figure keeps its tenth; at or above ten the
    // tenth is rounded away — the same rule, in the same function, that
    // formatGib applies to memory, so a whole number of units prints without a
    // trailing `.0`. Both halves are read in the total's unit, so `used` is
    // never printed on a scale its total is not on.
    expect(formatStoragePair(0.5 * TIB, TIB)).toBe('0.5 / 1 TiB');
    expect(formatStoragePair(512 * GIB, 2 * TIB)).toBe('0.5 / 2 TiB');
    // Half an answer is not an answer, and neither is half a payload.
    expect(formatStoragePair(null, TIB)).toBeNull();
    expect(formatStoragePair(335 * GIB, undefined)).toBeNull();
  });

  it('explains the metric, the denominator and the filesystem it came from', () => {
    const title = storageCell(storageCapacity()).title ?? '';
    // What was measured, and where — the mount root, spelled as one.
    expect(title).toContain('writable layers');
    expect(title).toContain('the filesystem mounted at /mnt/agent-docker');
    // The definition, in the same words the figure is computed with, plus the
    // reserved-block caveat that keeps the two lines from being read as a
    // partition of the total.
    expect(title).toContain('used / (used + available)');
    // The caveat has to say what `used` *is* (total minus free), not that it
    // counts the reserve: it does not, and a reader who acts on the wrong
    // version concludes the disk is fuller than it is.
    expect(title).toContain('used is total minus free');
    expect(title).toContain('reserved blocks that are still free');
    expect(title).not.toContain('used counts the blocks');
    expect(title).toContain('Source: linux_overlay_root_statvfs.');
    // No target: the sentence is still grammatical rather than leaving a
    // dangling preposition.
    const targetless = storageCell(storageCapacity({ storage_target: null })).title ?? '';
    expect(targetless).not.toContain('mounted at');
    expect(targetless).toContain('writable layers.');
    // The probe's own identifier, not the payload's collector source.
    expect(storageCell(storageCapacity({ storage_source: null })).title).not.toContain('Source:');
  });

  it('says an unverified measurement needs a deployment to confirm it', () => {
    const unverified = storageCell(
      storageCapacity({ storage_used_percent: null, storage_missing_reason: 'unverified' }),
    );
    expect(unverified.text).toBe('Storage not verified');
    expect(unverified.title).toContain('a successful deployment verification');
    // The same sentence rides along on a measured cell: a verified figure can
    // go back to unverified when the filesystem is resized under it, and the
    // reader of the number needs to know that as much as the reader of the gap.
    const since = storageCell(storageCapacity({ storage_missing_reason: 'unverified' }));
    expect(since.text).toBe('35% · 335 / 1000 GiB');
    expect(since.title).toContain('a successful deployment verification');
    // No other reason gets the sentence: `disabled` is a decision, not a
    // verification still owed.
    expect(storageCell(storageCapacity()).title).not.toContain('confir');
  });

  it('derives the same percentage the tooltip explains', () => {
    // Section 9 of the design asks for the current value and the tooltip to
    // come from one formula. The cell prints the ceil of the DTO's own figure,
    // and the tenth is that figure unchanged.
    const observation = storageCapacity();
    const percent = observation.storage_used_percent as number;
    expect(storageCell(observation).text).toContain(`${Math.ceil(percent)}%`);
    expect(formatPercentTenth(percent)).toBe('34.0%');
  });
});

describe('retained sandboxes', () => {
  it('names running and stopped and appends only non-zero other states', () => {
    expect(summarizeSandboxes([host()]).text).toBe('3 running · 7 stopped');
    const paused = summarizeSandboxes([
      host({
        capacity: capacity({
          inventory: inventory({
            other_states: { created: 0, paused: 1, restarting: 0, removing: 0, dead: 2 },
          }),
        }),
      }),
    ]);
    expect(paused.text).toBe('3 running · 7 stopped · 1 paused · 2 dead');
    expect(paused.partial).toBe(false);
  });

  it('counts one namespace once when several replicas report it', () => {
    const older = host({
      host: 'runner-a',
      capacity: capacity({
        sampled_at: '2026-09-11T12:34:01Z',
        inventory: inventory({ running: 3, stopped: 7 }),
      }),
    });
    const newer = host({
      host: 'runner-b',
      capacity: capacity({
        sampled_at: '2026-09-11T12:35:01Z',
        inventory: inventory({ running: 4, stopped: 9 }),
      }),
    });
    const summary = summarizeSandboxes([older, newer]);
    // Three replicas of one deployment are one Docker host seen three times.
    expect(summary.text).toBe('4 running · 9 stopped');
    expect(summary.partial).toBe(false);
    // Both reports are still listed, so the panel can show what each said.
    expect(summary.perSource.map((line) => line.host)).toEqual(['runner-b', 'runner-a']);
  });

  it('adds distinct explicitly named namespaces on the same boot', () => {
    const summary = summarizeSandboxes([
      host({ host: 'runner-a', capacity: capacity({ inventory: inventory() }) }),
      host({
        host: 'runner-b',
        capacity: capacity({
          inventory: inventory({ namespace: 'example-agent-runc', running: 1, stopped: 2 }),
        }),
      }),
    ]);
    expect(summary.text).toBe('4 running · 9 stopped');
    expect(summary.partial).toBe(false);
  });

  it('lists per-environment counts rather than a sum it cannot prove', () => {
    const summary = summarizeSandboxes([
      host({ host: 'runner-a', capacity: capacity({ inventory: inventory() }) }),
      host({
        host: 'runner-b',
        capacity: capacity({
          // No boot id: nothing proves this is the same Docker host as runner-a.
          linux_boot_id: null,
          inventory: inventory({ namespace: 'example-agent-runc', running: 1, stopped: 2 }),
        }),
      }),
    ]);
    expect(summary.text).toBe(
      'example-agent-kata 3 running · 7 stopped | example-agent-runc 1 running · 2 stopped',
    );
    expect(summary.partial).toBe(true);
    expect(summary.reasons).toContain('sources_not_summed');
    expect(summary.explanation).toContain('cannot be proven');
  });

  it('reports a partial count as a lower bound and carries its reason', () => {
    const summary = summarizeSandboxes([
      host({
        capacity: capacity({
          inventory: inventory({
            status: 'partial',
            running: 3,
            stopped: null,
            reason: 'unknown_state',
          }),
        }),
      }),
    ]);
    expect(summary.text).toBe('≥3 observed · Partial');
    expect(summary.partial).toBe(true);
    expect(summary.reasons).toEqual(['unknown_state']);
  });

  it('shows Unavailable, never zero, and Not measured when nothing was ever reported', () => {
    const unavailable = summarizeSandboxes([
      host({
        capacity: capacity({
          inventory: inventory({
            status: 'unavailable',
            running: null,
            stopped: null,
            other_states: null,
            reason: 'docker_timeout',
          }),
        }),
      }),
    ]);
    expect(unavailable.text).toBe('Unavailable');
    expect(unavailable.reasons).toEqual(['docker_timeout']);
    expect(summarizeSandboxes([host({ capacity: null })]).text).toBe('Not measured');
    expect(summarizeSandboxes([]).text).toBe('Not measured');
  });

  it('treats an unattributable report as partial rather than as a total', () => {
    const summary = summarizeSandboxes([
      host({ capacity: capacity({ inventory: inventory({ namespace: null }) }) }),
    ]);
    expect(summary.text).toBe('≥10 observed · Partial');
    expect(summary.reasons).toContain('namespace_unset');
  });

  it('marks the total partial when one source could not count everything', () => {
    const summary = summarizeSandboxes([
      host({ host: 'runner-a', capacity: capacity({ inventory: inventory() }) }),
      host({
        host: 'runner-b',
        capacity: capacity({
          inventory: inventory({
            namespace: 'example-agent-runc',
            status: 'unavailable',
            running: null,
            stopped: null,
            other_states: null,
            reason: 'docker_timeout',
          }),
        }),
      }),
    ]);
    expect(summary.text).toBe('≥10 observed · Partial');
    expect(summary.reasons).toEqual(['docker_timeout']);
  });

  it('bounds the total from below when an enrollment reported nothing', () => {
    // An older runner on the same machine holds containers this server cannot
    // see, so the machines that did report are not the whole machine.
    const summary = summarizeSandboxes([
      host({ host: 'runner-a', capacity: capacity({ inventory: inventory() }) }),
      host({ host: 'runner-b', capacity: null }),
    ]);
    expect(summary.text).toBe('≥10 observed · Partial');
    expect(summary.partial).toBe(true);
    expect(summary.reasons).toEqual(['inventory_not_reported']);
  });

  it('does not count a record that is neither reporting nor reporting anything', () => {
    // The row a rename leaves behind: the machine's previous identity, offline
    // since it stopped answering under that name, and never having sent an
    // observation because it ran code older than the collector. It is not a
    // source this page can be short of — its containers, if it has any, are on
    // this same Docker host under the name that is still reporting.
    const summary = summarizeSandboxes([
      host({
        host: 'rtx6000b-kata',
        status: 'online',
        capacity: capacity({ inventory: inventory({ running: 0, stopped: 18 }) }),
      }),
      host({ host: 'rtx6000b', status: 'offline', capacity: null }),
    ]);

    expect(summary.text).toBe('0 running · 18 stopped');
    expect(summary.partial).toBe(false);
    expect(summary.reasons).toEqual([]);
  });

  it('still bounds the total when a *reporting* enrollment sent no inventory', () => {
    // The case the lower bound exists for: a runner alive on this machine,
    // running code older than the collector, holding containers the page
    // cannot see. Silence from a machine that is answering is a gap; silence
    // from a record that is not answering is not.
    const summary = summarizeSandboxes([
      host({ host: 'runner-a', status: 'online', capacity: capacity({ inventory: inventory() }) }),
      host({ host: 'runner-b', status: 'online', capacity: null }),
      host({ host: 'runner-old', status: 'offline', capacity: null }),
    ]);

    expect(summary.text).toBe('≥10 observed · Partial');
    expect(summary.reasons).toEqual(['inventory_not_reported']);
  });

  it('carries the legacy-unscoped warning into the total', () => {
    const summary = summarizeSandboxes([
      host({
        capacity: capacity({
          inventory: inventory({
            unscoped_legacy_containers: 2,
            reason: 'unscoped_legacy_containers',
          }),
        }),
      }),
    ]);
    expect(summary.partial).toBe(true);
    expect(summary.text).toBe('≥10 observed · Partial');
    expect(summary.reasons).toContain('unscoped_legacy_containers');
  });
});

describe('one physical server', () => {
  it('shows the freshest fresh observation, and ages only what it shows', () => {
    const fresh = host({
      host: 'runner-a',
      capacity: capacity({
        sample_age_seconds: 12,
        freshness: 'fresh',
        cpu_percent: 11,
        inventory: inventory({ namespace: 'example-agent-a' }),
      }),
    });
    const stale = host({
      host: 'runner-b',
      capacity: capacity({
        sample_age_seconds: 400,
        freshness: 'stale',
        cpu_percent: 90,
        inventory: inventory({ namespace: 'example-agent-b' }),
      }),
      labels: { physical_host: 'runner-a' },
    });
    const facts = groupCapacityFacts([stale, fresh]);
    expect(facts.source?.host).toBe('runner-a');
    expect(facts.capacity?.cpu_percent).toBe(11);
    // The expired source contributes nothing to this row — not its CPU, not
    // its sandbox count — so it must not set the row's age either. Its own
    // time is listed in the panel, next to the number it belongs to.
    expect(facts.oldestSampleAgeSeconds).toBe(12);
    // The row still says something on it has expired.
    expect(facts.stale).toBe(true);
  });

  it('expires an observation locally once the page stops hearing from the pool', () => {
    // The server's `freshness` describes the moment it answered. A page that
    // has been open, or failing to poll, for another two minutes is showing a
    // reading that has since expired — and the stale marker has to say so, or
    // the age ticking up beside a "fresh" label is the one reading that looks
    // current and is not.
    const members = [host({ capacity: capacity({ sample_age_seconds: 5, freshness: 'fresh' }) })];
    const expiresAfterMs = (CAPACITY_FRESH_SECONDS - 5) * 1000 + 1;
    expect(groupCapacityFacts(members, 0).stale).toBe(false);
    expect(groupCapacityFacts(members, 30_000).stale).toBe(false);
    // The threshold is on the age by now, not on the wait: exactly at it the
    // reading is still current, and one millisecond later it is not.
    expect(groupCapacityFacts(members, expiresAfterMs - 1).stale).toBe(false);
    expect(groupCapacityFacts(members, expiresAfterMs).stale).toBe(true);
  });

  it('excludes an expired source from the current sandbox count', () => {
    // Two environments behind one physical name, one of them measured an hour
    // ago. Adding them states a total for a machine nobody measured: the old
    // one still holds containers that have since been stopped and cannot know
    // about any started since. It is excluded, named, and the total becomes a
    // lower bound.
    const now = host({
      host: 'runner-now',
      labels: { physical_host: 'box' },
      capacity: capacity({
        sample_age_seconds: 5,
        inventory: inventory({ namespace: 'example-a', running: 2, stopped: 0 }),
      }),
    });
    const old = host({
      host: 'runner-old',
      labels: { physical_host: 'box' },
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({ namespace: 'example-b', running: 9, stopped: 0 }),
      }),
    });

    const summary = summarizeSandboxes([now, old], (member) => member.host === 'runner-old');

    expect(summary.text).toBe('≥2 observed · Partial');
    expect(summary.partial).toBe(true);
    expect(summary.reasons).toContain('source_stale');
    expect(summary.contributingHosts).toEqual(['runner-now']);
    expect(summary.explanation).toContain('runner-old');

    // Both sources are still listed, each with the time it belongs to.
    expect(summary.perSource.map((line) => line.host)).toEqual(['runner-now', 'runner-old']);
    expect(summary.perSource[1].counts).toBe('9 running · 0 stopped');
  });

  it('shows a last observation as stale rather than as the current count', () => {
    // Nothing current was measured, so there is no total to give. The last
    // reading is still worth showing — with the host and the fact that it has
    // expired attached to it, and with nothing counted as current.
    const only = host({
      host: 'runner-old',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({ running: 9, stopped: 0 }),
      }),
    });

    const summary = summarizeSandboxes([only], () => true);

    expect(summary.text).toBe('9 running · 0 stopped');
    expect(summary.staleOnly).toBe(true);
    expect(summary.partial).toBe(true);
    // The counts in `text` are this host's, so it is the row's ingredient —
    // `staleOnly` is what says they are not current.
    expect(summary.contributingHosts).toEqual(['runner-old']);
    expect(summary.stale).toBe(true);
    expect(summary.explanation).toContain('runner-old');
  });

  it('never turns an unavailable listing into zero, however old it is', () => {
    // A listing that timed out has no counts at all. Showing it as the last
    // observation is right; showing the nulls it carries as zeroes would state
    // a number that was never measured, and this is the one branch where that
    // could happen — the fresh path refuses unavailable reports already.
    const failed = host({
      host: 'runner-a',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({
          status: 'unavailable',
          running: null,
          stopped: null,
          other_states: null,
          reason: 'docker_timeout',
        }),
      }),
    });

    const summary = summarizeSandboxes([failed], () => true);

    expect(summary.text).toBe('Unavailable');
    expect(summary.staleOnly).toBe(false);
    expect(summary.contributingHosts).toEqual([]);
    expect(summary.reasons).toContain('docker_timeout');
  });

  it('shows the newest expired observation, with its own age', () => {
    // Two expired environments. The older one happens to come first in the
    // array; pairing its counts with the newer one's age is how a row ends up
    // describing two machines at once.
    const older = host({
      host: 'older',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        sampled_at: '2026-09-11T11:34:00Z',
        inventory: inventory({ namespace: 'ns-old', running: 9, stopped: 0 }),
      }),
    });
    const newer = host({
      host: 'newer',
      capacity: capacity({
        sample_age_seconds: 120,
        freshness: 'stale',
        sampled_at: '2026-09-11T12:32:00Z',
        inventory: inventory({ namespace: 'ns-new', running: 2, stopped: 0 }),
      }),
    });

    const facts = groupCapacityFacts([older, newer]);
    const summary = facts.sandboxes;

    expect(summary.text).toBe('2 running · 0 stopped');
    expect(summary.explanation).toContain('newer');
    expect(summary.contributingHosts).toEqual(['newer']);
    // The number and the age describe the same machine.
    expect(facts.source?.host).toBe('newer');
    expect(facts.oldestSampleAgeSeconds).toBe(120);
    expect(facts.stale).toBe(true);
  });

  it('does not call a superseded duplicate missing coverage', () => {
    // Three replicas share one namespace; one of them is a beat behind. That
    // is not an uncovered environment — the namespace is fully counted by a
    // report that is right here — and marking it Partial would put the badge
    // on every healthy server whose replicas report at slightly different
    // moments.
    const fresh = host({
      host: 'replica-a',
      capacity: capacity({
        sample_age_seconds: 5,
        freshness: 'fresh',
        inventory: inventory({ namespace: 'example-agent', running: 2, stopped: 0 }),
      }),
    });
    const duplicate = host({
      host: 'replica-b',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({ namespace: 'example-agent', running: 2, stopped: 0 }),
      }),
    });

    const facts = groupCapacityFacts([fresh, duplicate]);

    expect(facts.sandboxes.text).toBe('2 running · 0 stopped');
    expect(facts.sandboxes.partial).toBe(false);
    expect(facts.sandboxes.reasons).not.toContain('source_stale');
    expect(facts.stale).toBe(false);
    expect(facts.oldestSampleAgeSeconds).toBe(5);
    // Both are still listed, so the panel can show which replica is behind.
    expect(facts.sandboxes.perSource.map((line) => line.host)).toEqual(['replica-a', 'replica-b']);
  });

  it('does not treat an unidentified expired source as covered', () => {
    // An expired report with no namespace cannot be shown to be the same
    // environment as anything, so it is a source this server cannot see — not
    // a duplicate to be quietly deduped away. Hiding it is the failure that
    // cannot be recovered from; saying "Partial" when the two turn out to be
    // one machine is the failure that can.
    const fresh = host({
      host: 'fresh',
      capacity: capacity({
        sample_age_seconds: 5,
        inventory: inventory({ namespace: 'ns-a', running: 2, stopped: 0 }),
      }),
    });
    const anonymous = host({
      host: 'anonymous',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({ namespace: null, running: 9, stopped: 0 }),
      }),
    });

    const facts = groupCapacityFacts([fresh, anonymous]);

    expect(facts.sandboxes.partial).toBe(true);
    expect(facts.stale).toBe(true);
    expect(facts.sandboxes.text).toBe('≥2 observed · Partial');
    // The unidentified source is named in the panel, where an operator can
    // see that a whole environment is not being counted.
    expect(facts.sandboxes.perSource.map((line) => line.host)).toContain('anonymous');
  });

  it('does not treat the same namespace on an unproven host as covered', () => {
    // A rebuilt machine keeps its name, so a namespace alone does not survive
    // it: the same namespace on another boot id is another Docker host, and an
    // unknown boot id proves nothing either way.
    const fresh = host({
      host: 'fresh',
      capacity: capacity({
        sample_age_seconds: 5,
        linux_boot_id: 'boot-a',
        inventory: inventory({ namespace: 'ns-a', running: 2, stopped: 0 }),
      }),
    });
    const otherBoot = host({
      host: 'other-boot',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        linux_boot_id: 'boot-b',
        inventory: inventory({ namespace: 'ns-a', running: 9, stopped: 0 }),
      }),
    });
    const unknownBoot = host({
      host: 'unknown-boot',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        linux_boot_id: null,
        inventory: inventory({ namespace: 'ns-a', running: 9, stopped: 0 }),
      }),
    });

    expect(groupCapacityFacts([fresh, otherBoot]).sandboxes.partial).toBe(true);
    expect(groupCapacityFacts([fresh, unknownBoot]).sandboxes.partial).toBe(true);

    // The proof works both ways: same namespace, same known boot id, is a
    // superseded duplicate and nothing is missing.
    const sameBoot = host({
      host: 'same-boot',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        linux_boot_id: 'boot-a',
        inventory: inventory({ namespace: 'ns-a', running: 2, stopped: 0 }),
      }),
    });
    const covered = groupCapacityFacts([fresh, sameBoot]);
    expect(covered.sandboxes.partial).toBe(false);
    expect(covered.stale).toBe(false);
  });

  it('never renders the unknown counts of a partial listing as zero', () => {
    // The heartbeat model accepts `partial` with every count null and a
    // reason, which is the honest shape a listing gets when it can say what
    // happened but not how many. Formatting those nulls as zeroes states a
    // very specific nothing.
    const nothingCounted = host({
      host: 'nothing',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({
          status: 'partial',
          running: null,
          stopped: null,
          other_states: null,
          reason: 'unknown_state',
        }),
      }),
    });
    const someCounted = host({
      host: 'some',
      capacity: capacity({
        sample_age_seconds: 3600,
        freshness: 'stale',
        inventory: inventory({
          status: 'partial',
          running: 2,
          stopped: null,
          reason: 'unknown_state',
        }),
      }),
    });

    const none = summarizeSandboxes([nothingCounted], () => true);
    expect(none.text).toBe('Unavailable');
    expect(none.staleOnly).toBe(false);
    expect(none.contributingHosts).toEqual([]);
    expect(none.perSource[0].counts).toBe('Not counted');

    const some = summarizeSandboxes([someCounted], () => true);
    expect(some.text).toBe('≥2 observed');
    expect(some.text).not.toContain('stopped');
    expect(some.staleOnly).toBe(true);
    expect(some.perSource[0].counts).toBe('≥2 observed');
  });

  it('falls back to a stale observation when that is all there is', () => {
    const facts = groupCapacityFacts([
      host({ capacity: capacity({ sample_age_seconds: 400, freshness: 'stale' }) }),
    ]);
    expect(facts.capacity).not.toBeNull();
    expect(facts.stale).toBe(true);
  });

  it('has no source at all for a host that never reported', () => {
    const facts = groupCapacityFacts([host({ capacity: null })]);
    expect(facts.source).toBeNull();
    expect(facts.capacity).toBeNull();
    expect(facts.oldestSampleAgeSeconds).toBeNull();
    expect(facts.stale).toBe(false);
  });
});

describe('ages advance monotonically', () => {
  it('adds locally observed elapsed time to the server figure', () => {
    expect(liveAgeSeconds(12, 5_000)).toBe(17);
    expect(liveAgeSeconds(12, 0)).toBe(12);
    // A backwards monotonic reading is ignored rather than reported.
    expect(liveAgeSeconds(12, -1_000)).toBe(12);
    expect(liveAgeSeconds(Number.NaN, 1_000)).toBe(1);
  });

  it('reads a monotonic instant', () => {
    const first = monotonicNow();
    expect(Number.isFinite(first)).toBe(true);
    expect(monotonicNow()).toBeGreaterThanOrEqual(first);
  });
});

describe('UTC time', () => {
  it('formats an instant the same way whichever offset spelling arrived', () => {
    expect(formatUtcTimestamp('2026-09-11T12:34:56Z')).toBe('2026-09-11 12:34 UTC');
    expect(formatUtcTimestamp('2026-09-11T12:34:56+00:00')).toBe('2026-09-11 12:34 UTC');
    expect(formatUtcTimestamp('2026-09-11T12:34:56.123456Z')).toBe('2026-09-11 12:34 UTC');
    expect(formatUtcTimestamp(null)).toBe('');
    expect(formatUtcTimestamp('not a time')).toBe('');
  });

  it('keeps the date off an hour-long axis and on the longer ones', () => {
    expect(formatUtcAxisTick(AT(4), '1h')).toBe('12:04');
    expect(formatUtcAxisTick(AT(4), '6h')).toBe('09-11 12:04');
    expect(formatUtcAxisTick(AT(4), '24h')).toBe('09-11 12:04');
  });
});

describe('series', () => {
  it('maps the metric and keeps the pair a memory tooltip needs', () => {
    const [cpu] = capacitySeries([point(0, 37.24)], 'cpu');
    expect(cpu.value).toBe(37.24);
    expect(cpu.detail).toBeNull();
    const [memory] = capacitySeries([point(0, 32.8)], 'memory');
    expect(memory.value).toBe(32.8);
    expect(memory.detail).toBe('42 / 128 GiB');
  });

  it('renders a tooltip with the observation time, the value and the unit', () => {
    const [cpu] = capacitySeries([point(4, 37.24)], 'cpu');
    expect(formatPointTooltip('cpu', cpu)).toBe('2026-09-11 12:04 UTC · CPU 37.2%');
    const [memory] = capacitySeries([point(4, 32.8)], 'memory');
    expect(formatPointTooltip('memory', memory)).toBe(
      '2026-09-11 12:04 UTC · Memory 32.8% · 42 / 128 GiB',
    );
  });

  it('breaks the line on an unobserved minute and on a value that is null', () => {
    const series = capacitySeries(
      [
        point(0, 10),
        point(1, 20),
        // A minute nobody observed.
        point(2, null, { sampled_at: null, cpu_missing_reason: 'source_unavailable' }),
        point(3, 30),
        // Observed, but the CPU half is missing.
        point(4, null, { cpu_missing_reason: 'warming_up' }),
        point(5, 40),
      ],
      'cpu',
    );
    const geometry = chartGeometry(series, AT(0), AT(5));
    // Three runs, four observed points: no line crosses the gaps.
    expect(geometry.segments.map((segment) => segment.length)).toEqual([2, 1, 1]);
    expect(geometry.points).toHaveLength(4);
  });

  it('breaks the line where the machine behind the name changed', () => {
    const series = capacitySeries(
      [
        point(0, 10),
        point(1, 10),
        point(2, 10, { linux_boot_id: 'boot-b' }),
        point(3, 10, { linux_boot_id: 'boot-b' }),
      ],
      'cpu',
    );
    expect(chartGeometry(series, AT(0), AT(3)).segments.map((s) => s.length)).toEqual([2, 2]);
  });

  it('breaks the line on a gap in the bucket grid itself', () => {
    const series = capacitySeries([point(0, 10), point(1, 10), point(30, 10)], 'cpu');
    expect(chartGeometry(series, AT(0), AT(30)).segments.map((s) => s.length)).toEqual([2, 1]);
  });

  it('draws a single observed minute as its own segment', () => {
    const series = capacitySeries([point(0, null), point(1, 42), point(2, null)], 'cpu');
    const geometry = chartGeometry(series, AT(0), AT(2));
    expect(geometry.segments).toHaveLength(1);
    expect(geometry.segments[0]).toHaveLength(1);
  });
});

describe('chart geometry', () => {
  it('spans the whole plot from since to until, so gaps keep their width', () => {
    const series = capacitySeries([point(0, 0), point(30, 100)], 'cpu');
    const geometry = chartGeometry(series, AT(0), AT(30));
    expect(geometry.plotLeft).toBe(CHART_VIEW.left);
    expect(geometry.points[0].x).toBe(CHART_VIEW.left);
    expect(geometry.points[1].x).toBe(CHART_VIEW.width - CHART_VIEW.right);
    // Fixed 0-100 scale: 0 is the floor and 100 the ceiling.
    expect(geometry.points[0].y).toBe(geometry.plotBottom);
    expect(geometry.points[1].y).toBe(geometry.plotTop);
  });

  it('puts a value where it is, not where it ranks', () => {
    expect(chartYForValue(0)).toBe(
      CHART_VIEW.top + (CHART_VIEW.height - CHART_VIEW.top - CHART_VIEW.bottom),
    );
    expect(chartYForValue(50)).toBeLessThan(chartYForValue(10));
    expect(chartYForValue(500)).toBe(CHART_VIEW.top);
    expect(chartXForTime(0, 0, 0)).toBe(CHART_VIEW.left);
  });

  it('picks the nearest observed point to a position', () => {
    const series = capacitySeries([point(0, 10), point(10, 20), point(20, 30)], 'cpu');
    const { points } = chartGeometry(series, AT(0), AT(20));
    expect(nearestPointIndex(points, points[0].x + 1)).toBe(0);
    expect(nearestPointIndex(points, points[1].x)).toBe(1);
    expect(nearestPointIndex(points, points[2].x + 500)).toBe(2);
    expect(nearestPointIndex([], 10)).toBe(-1);
  });

  it('maps a pointer through the letterboxing the aspect ratio creates', () => {
    // An exact match: one user unit per pixel.
    expect(
      svgXFromClientX({ left: 0, width: CHART_VIEW.width, height: CHART_VIEW.height }, 100),
    ).toBe(100);
    // Drawn at twice the size: two pixels per user unit.
    const doubled = {
      left: 20,
      width: CHART_VIEW.width * 2,
      height: CHART_VIEW.height * 2,
    };
    expect(svgXFromClientX(doubled, 20 + 100)).toBe(50);
    // A box wider than the drawing: the scale is the height's, and the drawing
    // is centred, so the left margin is not the axis.
    const wide = { left: 20, width: CHART_VIEW.width * 2, height: CHART_VIEW.height };
    const margin = (CHART_VIEW.width * 2 - CHART_VIEW.width) / 2;
    expect(svgXFromClientX(wide, 20 + margin + 100)).toBe(100);
    // A collapsed box cannot be mapped and must not divide by zero.
    expect(svgXFromClientX({ left: 0, width: 0, height: 0 }, 10)).toBe(CHART_VIEW.left);
    // Outside the drawing, the reading is clamped to the axis.
    expect(svgXFromClientX(wide, 0)).toBe(0);
    expect(svgXFromClientX({ left: 0, width: 520, height: 220 }, 900)).toBe(CHART_VIEW.width);
  });
});

describe('gaps', () => {
  it('annotates interior holes only, never the edges of the window', () => {
    const series = capacitySeries(
      [point(0, null), point(1, 10), point(2, 10), point(3, null), point(4, null), point(5, 10)],
      'cpu',
    );
    const gaps = gapSpans(series, AT(0), AT(5));
    expect(gaps).toHaveLength(1);
    expect(gaps[0].minutes).toBe(2);
    expect(gaps[0].key).toBe(AT(3));
    expect(gaps[0].startX).toBeLessThan(gaps[0].endX);
  });

  it('has nothing to annotate when the window holds no observation', () => {
    const series = capacitySeries([point(0, null), point(1, null)], 'cpu');
    expect(gapSpans(series, AT(0), AT(1))).toEqual([]);
  });
});

describe('range facts', () => {
  it('counts observations and the highest one, without calling it a peak', () => {
    const series = capacitySeries([point(0, 10), point(1, null), point(2, 82.44)], 'cpu');
    expect(observedPointCount(series)).toBe(2);
    expect(highestObserved(series)).toBe(82.44);
    expect(chartSummaryText('cpu', series, AT(0), AT(2))).toContain('highest observed 82.4%');
    expect(chartSummaryText('cpu', series, AT(0), AT(2))).toContain('1 gap');
    expect(chartSummaryText('cpu', series, AT(0), AT(2))).not.toContain('peak');
  });

  it('says a window is still accumulating when the record starts inside it', () => {
    const late = capacitySeries([point(20, 10), point(21, 10)], 'cpu');
    expect(historyIsAccumulating(late, AT(0), 60)).toBe(true);
    const complete = capacitySeries([point(0, 10), point(1, 10)], 'cpu');
    expect(historyIsAccumulating(complete, AT(0), 60)).toBe(false);
    const empty = capacitySeries([point(0, null)], 'cpu');
    expect(historyIsAccumulating(empty, AT(0), 60)).toBe(true);
  });
});
