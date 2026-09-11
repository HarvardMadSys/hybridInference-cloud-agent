import { describe, expect, it } from 'vitest';

import type {
  AgentHostCapacity,
  AgentHostCapacityInventory,
  AgentHostCapacityPoint,
  AgentRunnerHost,
} from '@/lib/api/admin';

import {
  CAPACITY_FRESH_SECONDS,
  CHART_VIEW,
  capacitySeries,
  chartGeometry,
  chartSummaryText,
  chartXForTime,
  chartYForValue,
  cpuCell,
  executionSlotsCell,
  formatGib,
  formatGibPair,
  formatPercent,
  formatPercentTenth,
  formatPointTooltip,
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
  summarizeSandboxes,
  svgXFromClientX,
} from './capacity';

const GIB = 1024 ** 3;

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
});

describe('cells', () => {
  it('states the execution quota without inventing a ceiling', () => {
    expect(executionSlotsCell({ slots_used: 2, slots_total: 3 })).toMatchObject({
      text: '2 / 3 in use',
      note: '1 free',
    });
    // Over-subscribed is clamped, never negative.
    expect(executionSlotsCell({ slots_used: 5, slots_total: 3 }).note).toBe('0 free');
    // No declared ceiling: the used count is a fact, the free count is not.
    expect(executionSlotsCell({ slots_used: 2, slots_total: null })).toMatchObject({
      text: 'Limit not set',
      note: '2 in use',
    });
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
