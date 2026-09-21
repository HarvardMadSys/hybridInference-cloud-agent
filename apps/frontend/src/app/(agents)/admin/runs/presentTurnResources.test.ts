import { describe, expect, it } from 'vitest';

import type {
  ResourceMetricBriefApi,
  RunResourceSummaryApi,
  TurnResourceReasonCode,
  TurnResourceSummaryApi,
} from '@/lib/api/adminRuns';

import {
  attemptCoverageNote,
  collectingSentence,
  completenessSentence,
  cpuCell,
  formatCpuSeconds,
  formatMemoryBytes,
  formatSpan,
  interruptedSentence,
  memoryCell,
  memoryMethod,
  reasonSentence,
  reasonSentences,
  runResourceCells,
  runStateLabel,
  TURN_SCOPE_SENTENCE,
  turnSummaryRows,
  UNKNOWN_REASON_SENTENCE,
} from './presentTurnResources';

const AVAILABLE: ResourceMetricBriefApi = { status: 'available', reason_codes: [] };

/** One attempt's summary, overridden per test. */
function summary(overrides: Partial<TurnResourceSummaryApi> = {}): TurnResourceSummaryApi {
  return {
    measurement_id: '00000000-0000-4000-8000-000000000001',
    state: 'final',
    source: 'kata-guest-member-window-v1',
    received_at: '2026-09-10T08:02:00Z',
    duration_ms: 130_000,
    observed_through_ms: 130_000,
    open_scan_ms: 12,
    close_scan_ms: 15,
    cpu: {
      status: 'available',
      method: 'live_and_exit_task_counters',
      usec: 80_000_000,
      reason_codes: [],
    },
    memory: {
      status: 'available',
      method: 'sampled_member_pss',
      peak_bytes: 1_288_490_189,
      sample_interval_ms: 1000,
      valid_samples: 131,
      expected_samples: 131,
      max_gap_ms: 1000,
      reason_codes: [],
    },
    storage_io: {
      status: 'available',
      method: 'task_io_accounting',
      read_bytes: 2_097_152,
      write_bytes: 524_288,
      reason_codes: [],
    },
    ...overrides,
  };
}

/** The compact run projection the Runs list reads, overridden per test. */
function runSummary(overrides: Partial<RunResourceSummaryApi> = {}): RunResourceSummaryApi {
  const full = summary();
  return {
    cpu_usec: full.cpu.usec,
    memory_peak_bytes: full.memory.peak_bytes,
    state: 'final',
    observed_through_ms: full.observed_through_ms,
    cpu: AVAILABLE,
    memory: AVAILABLE,
    storage_io: AVAILABLE,
    attempts_measured: 1,
    attempts_total: 1,
    ...overrides,
  };
}

function brief(
  status: ResourceMetricBriefApi['status'],
  reason_codes: TurnResourceReasonCode[] = [],
): ResourceMetricBriefApi {
  return { status, reason_codes };
}

describe('turn resource magnitudes', () => {
  it('renders CPU in CPU-seconds and never clamps a value above the turn', () => {
    // 80 CPU-s of a 130-second turn is the member's own scheduler time summed
    // over its threads, not wall-clock utilisation: it is the number, not a
    // figure to be explained away.
    expect(formatCpuSeconds(80_000_000)).toBe('80 CPU-s');
    expect(formatCpuSeconds(130_000_000)).toBe('130 CPU-s');
    expect(formatCpuSeconds(12_345_000)).toBe('12.3 CPU-s');
  });

  it('keeps a sub-second CPU time distinguishable from a true zero', () => {
    expect(formatCpuSeconds(500_000)).toBe('0.500 CPU-s');
    expect(formatCpuSeconds(1_000)).toBe('0.00100 CPU-s');
    expect(formatCpuSeconds(12)).toBe('0.0000120 CPU-s');
    // A measured zero is still a zero, and it is never what null renders as.
    expect(formatCpuSeconds(0)).toBe('0 CPU-s');
  });

  it('renders memory in binary units, at the documented sample', () => {
    expect(formatMemoryBytes(1_288_490_189)).toBe('1.2 GiB');
    expect(formatMemoryBytes(524_288)).toBe('512 KiB');
    expect(formatMemoryBytes(0)).toBe('0 B');
    expect(formatMemoryBytes(1_073_741_824)).toBe('1 GiB');
  });

  it('renders a missing magnitude as unavailable, never as a zero', () => {
    for (const missing of [null, undefined, Number.NaN, -1]) {
      expect(formatCpuSeconds(missing)).toBeNull();
      expect(formatMemoryBytes(missing)).toBeNull();
    }
    // The two states are rendered by different words, and never by the same one.
    expect(cpuCell(null, AVAILABLE, 'final').text).not.toBe(cpuCell(0, AVAILABLE, 'final').text);
    expect(memoryCell(null, AVAILABLE, 'final').text).not.toBe(
      memoryCell(0, AVAILABLE, 'final').text,
    );
  });

  it('names the sampling period beside the peak', () => {
    expect(memoryMethod(1000)).toBe('sampled every 1s');
    expect(memoryMethod(500)).toBe('sampled every 500ms');
    expect(memoryMethod(1500)).toBe('sampled every 1.5s');
  });

  it('formats a window span and reports a missing cut-off as null', () => {
    expect(formatSpan(130_000)).toBe('2m 10s');
    expect(formatSpan(30_000)).toBe('30s');
    expect(formatSpan(200)).toBe('200ms');
    expect(formatSpan(null)).toBeNull();
  });
});

describe('per-metric status is independent', () => {
  it('marks a partial CPU measurement as the lower bound it is', () => {
    const partial = cpuCell(80_000_000, brief('partial', ['ring_loss']), 'final');
    expect(partial.text).toBe('\u2265 80 CPU-s');
    // A complete measurement is not dressed up as a bound.
    expect(cpuCell(80_000_000, AVAILABLE, 'final').text).toBe('80 CPU-s');
  });

  it('never gives a partial memory peak the ">=" form', () => {
    const partial = memoryCell(
      1_288_490_189,
      brief('partial', ['missing_thread_baseline']),
      'final',
    );
    expect(partial.text).toBe('1.2 GiB \u00b7 sampled \u00b7 observed');
    expect(partial.text).not.toContain('\u2265');
  });

  it('keeps a sampled peak marked as sampled even when it is complete', () => {
    expect(memoryCell(1_288_490_189, AVAILABLE, 'final').text).toBe('1.2 GiB \u00b7 sampled');
  });

  it('renders each unavailable metric as unavailable without touching the others', () => {
    expect(cpuCell(80_000_000, AVAILABLE, 'final').text).toBe('80 CPU-s');
    expect(memoryCell(null, brief('unavailable', ['not_a_kata_guest']), 'final').text).toBe(
      'Unavailable',
    );
    expect(
      runStateLabel({ state: 'final', cpu: AVAILABLE, memory: AVAILABLE, storage_io: AVAILABLE }),
    ).toBe('complete');
    expect(
      runStateLabel({
        state: 'final',
        cpu: AVAILABLE,
        memory: AVAILABLE,
        storage_io: brief('unavailable', ['filesystem_attribution_unverified']),
      }),
    ).toBe('partial');
  });
});

describe('turn states that are not a final total', () => {
  it('never reads an interrupted collection as complete', () => {
    const cells = runResourceCells(
      runSummary({ state: 'interrupted', observed_through_ms: 130_000 }),
    );
    expect(cells.state.text).toBe('Interrupted');
    expect(cells.state.text).not.toBe('Complete');
    expect(cells.cpu.text).toBe('80 CPU-s \u00b7 collection was interrupted');
    expect(cells.memory.text).toBe('1.2 GiB \u00b7 sampled \u00b7 collection was interrupted');
  });

  it('names the cut-off an interrupted collection reached', () => {
    expect(interruptedSentence(130_000)).toBe(
      'Collection interrupted: data reached 2m 10s of the turn, and this is not the turn\u2019s total.',
    );
    expect(interruptedSentence(null)).toContain('cut-off is not reported');
    // No cut-off is ever rendered as a zero.
    expect(interruptedSentence(null)).not.toContain('0ms');
  });

  it('never reads a still-collecting summary as a finalised total', () => {
    const cells = runResourceCells(
      runSummary({ state: 'collecting', observed_through_ms: 90_000 }),
    );
    expect(cells.state.text).toBe('Collecting');
    expect(cells.cpu.text).toContain('still collecting');
    expect(collectingSentence(90_000)).toBe(
      'Collection is still running: data has reached 1m 30s, and this is a snapshot, not the turn\u2019s total.',
    );
  });

  it('says a finished window is over without calling it complete on its own', () => {
    const cells = runResourceCells(runSummary({ state: 'final', observed_through_ms: 130_000 }));
    expect(cells.state.text).toBe('Complete');
    expect(cells.state.title).toBe('Collection finished with the turn.');
  });

  it('still reads a finished window with an unsupported metric as partial', () => {
    const cells = runResourceCells(
      runSummary({ storage_io: brief('unavailable', ['filesystem_attribution_unverified']) }),
    );
    expect(cells.state.text).toBe('Complete');
    expect(cells.completeness).toBe('partial');
  });
});

describe('attempt coverage', () => {
  it('says when the numbers cover only some executions of the run', () => {
    const cells = runResourceCells(runSummary({ attempts_measured: 3, attempts_total: 5 }));
    expect(cells.state.title).toContain('3 of 5 executions measured');
    expect(cells.state.title).toContain('not the run\u2019s total');
    expect(attemptCoverageNote(3, 5)).toBe(
      '3 of 5 executions measured; this is not the run\u2019s total.',
    );
  });

  it('stays silent when one execution is the whole run', () => {
    expect(attemptCoverageNote(1, 1)).toBeNull();
    expect(
      runResourceCells(runSummary({ attempts_measured: 1, attempts_total: 1 })).state.title,
    ).toBe('Collection finished with the turn.');
  });
});

describe('reason code sentences', () => {
  // A code added on the wire without a sentence here must fail this test
  // rather than quietly render its raw form on an admin page.
  const CODES: TurnResourceReasonCode[] = [
    'missing_thread_baseline',
    'counter_regressed',
    'thread_identity_conflict',
    'observer_restarted',
    'ring_loss',
    'filesystem_attribution_unverified',
    'pss_unreadable',
    'address_space_share_unverified',
    'sampling_over_budget',
    'window_prefix_retained',
    'observer_unavailable',
    'not_a_kata_guest',
    'source_mismatch',
  ];

  it('maps every bounded reason code to exactly one fixed sentence', () => {
    const sentences = CODES.map(reasonSentence);
    for (const [index, sentence] of sentences.entries()) {
      expect(sentence, CODES[index]).not.toBe(UNKNOWN_REASON_SENTENCE);
      expect(sentence.endsWith('.'), CODES[index]).toBe(true);
    }
    // One sentence per code, and none of them is another's copy.
    expect(new Set(sentences).size).toBe(CODES.length);
  });

  it('never renders the code itself, an identifier, or a kernel field name', () => {
    // Codes are snake_case identifiers, so any underscore in the rendered text
    // is one of them leaking through.
    for (const code of CODES) {
      const sentence = reasonSentence(code);
      expect(sentence).not.toContain(code);
      expect(sentence).not.toContain('_');
      expect(sentence).not.toContain('uid');
      expect(sentence).not.toContain('se.exec');
    }
    expect(reasonSentence('source_mismatch')).toBe(
      'These executions were measured by different collector versions, so their totals are not combined.',
    );
  });

  it('gives an unknown code the generic sentence and leaks neither it nor anything else', () => {
    const hostile = 'uid=1001 lease_token=deadbeef se.exec_runtime';
    const sentence = reasonSentence(hostile as unknown as TurnResourceReasonCode);
    expect(sentence).toBe(UNKNOWN_REASON_SENTENCE);
    expect(sentence).not.toContain(hostile);
    expect(sentence).not.toContain('1001');
    expect(sentence).not.toContain('deadbeef');
    // Inherited object properties are not a reason code either.
    expect(reasonSentence('toString' as unknown as TurnResourceReasonCode)).toBe(
      UNKNOWN_REASON_SENTENCE,
    );
  });

  it('says a repeated code once and states a cap rather than dropping reasons silently', () => {
    expect(reasonSentences(['ring_loss', 'ring_loss'])).toEqual([reasonSentence('ring_loss')]);
    const many = reasonSentences([
      'ring_loss',
      'counter_regressed',
      'observer_restarted',
      'not_a_kata_guest',
    ]);
    expect(many).toHaveLength(4);
    expect(many[3]).toBe('And 1 more reasons.');
  });
});

describe('detail panel rows', () => {
  it('shows each metric, its method and its own completeness', () => {
    const rows = turnSummaryRows({ ...summary(), attempts_measured: 3, attempts_total: 5 });
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row]));
    expect(byLabel['CPU usage'].value).toBe('80 CPU-s');
    expect(byLabel['Memory peak'].value).toBe('1.2 GiB \u00b7 sampled');
    expect(byLabel['Process storage I/O'].value).toBe('2 MiB read / 512 KiB written');
    // The panel must never imply the number is the platform's whole cost.
    expect(TURN_SCOPE_SENTENCE).toBe('Sandbox member execution, including the agent and its tools');
    expect(byLabel.Scope.value).toBe(TURN_SCOPE_SENTENCE);
    expect(byLabel.Completeness.value).toBe(
      'CPU complete. memory sampling complete. I/O complete.',
    );
    expect(byLabel.Completeness.detail).toBe(
      '3 of 5 executions measured; this is not the run\u2019s total.',
    );
  });

  it('states an unmeasured metric independently of the measured ones', () => {
    const rows = turnSummaryRows(
      summary({
        storage_io: {
          status: 'unavailable',
          method: 'task_io_accounting',
          read_bytes: null,
          write_bytes: null,
          reason_codes: ['filesystem_attribution_unverified'],
        },
      }),
    );
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row]));
    expect(byLabel['Process storage I/O'].value).toBe('Unavailable');
    expect(byLabel['Process storage I/O'].detail).toContain(
      'do not prove which filesystem this I/O reached',
    );
    expect(byLabel['CPU usage'].value).toBe('80 CPU-s');
    expect(byLabel.Completeness.value).toContain('I/O unavailable.');
    expect(byLabel.Completeness.value).toContain('CPU complete.');
  });

  it('says the cut-off in the metrics an interrupted collection did reach', () => {
    const rows = turnSummaryRows(summary({ state: 'interrupted', observed_through_ms: 130_000 }));
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row]));
    expect(byLabel['CPU usage'].detail).toContain('data reached 2m 10s');
    expect(byLabel['CPU usage'].value).not.toContain('final');
  });

  it('keeps the lower-bound wording on the partial metrics only', () => {
    const rows = turnSummaryRows(
      summary({ cpu: { ...summary().cpu, status: 'partial', reason_codes: ['ring_loss'] } }),
    );
    const byLabel = Object.fromEntries(rows.map((row) => [row.label, row]));
    expect(byLabel['CPU usage'].value).toBe('\u2265 80 CPU-s');
    expect(byLabel['CPU usage'].detail).toContain('lower bound');
    expect(byLabel['Memory peak'].value).not.toContain('\u2265');
  });
});

describe('not collected', () => {
  it('says a run with no summary has none, rather than showing a zero', () => {
    const cells = runResourceCells(null);
    expect(cells.cpu.text).toBe('Not collected');
    expect(cells.memory.text).toBe('Not collected');
    expect(cells.state.text).toBe('Not collected');
    expect(cells.state.text).not.toBe('0');
    expect(cells.completeness).toBe('unavailable');
    expect(runResourceCells(undefined).state.text).toBe('Not collected');
  });

  it('states each metric completeness on its own line of the sentence', () => {
    const sentence = completenessSentence({
      cpu: AVAILABLE,
      memory: brief('partial', ['pss_unreadable']),
      storage_io: brief('unavailable', ['not_a_kata_guest']),
    });
    expect(sentence).toBe(
      `CPU complete. memory sampling partial. ${reasonSentence('pss_unreadable')} ` +
        `I/O unavailable. ${reasonSentence('not_a_kata_guest')}`,
    );
    // A complete metric is not downgraded by an unmeasured one beside it.
    expect(sentence).toContain('CPU complete.');
  });
});
