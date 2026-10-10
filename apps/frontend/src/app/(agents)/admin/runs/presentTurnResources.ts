// Pure presentation for the whole-turn member execution window.
//
// The numbers come from `cloud_agent_host/member_resources.py` through the
// admin run endpoints, and the rules they must be read under are fixed by
// `docs/agents/design/turn-resource-accounting.md`. Three of them live here
// rather than in the components:
//
// - Null is not zero. A metric that was not measured renders as unavailable,
//   never as a zero, and the two are never interchangeable.
// - A partial additive quantity is a lower bound and reads ">="; a partial
//   sampled peak is not additive at all and reads "observed" instead.
// - A reason code never reaches the page. Each maps to exactly one sentence
//   below, and an unrecognized one falls back to a single generic sentence, so
//   a UID, a lease token, a kernel field name or an exception string cannot be
//   rendered through this field.
//
// Separate from the components because Next type-checks a page's exports, and
// tested directly rather than only through the rendered table.

import type {
  ResourceMetricBriefApi,
  ResourceMetricStatus,
  RunResourceSummaryApi,
  TurnMemoryResourcesApi,
  TurnResourceReasonCode,
  TurnResourceState,
  TurnResourceSummaryApi,
  TurnStorageIoResourcesApi,
} from '@/lib/api/adminRuns';

//: The display state of a turn's resource accounting. `unavailable` is a lack
//: of measurement, which is a different statement from a measured zero.
export type ResourceCompleteness =
  | 'complete'
  | 'partial'
  | 'collecting'
  | 'interrupted'
  | 'unavailable';

//: A value shown in the compact Runs list. `text` is the fix for "null must not
//: read as zero"; `title` carries the sentences that do not fit in a cell.
export interface ResourceCell {
  text: string;
  title?: string;
}

//: Measurement context for the detail panel's concise rows.
export interface ResourceReading {
  value: string;
  /** How the number was measured, retained in the value's accessible description. */
  method: string | null;
  /** Why the number is short or missing, as fixed sentences. */
  notes: string[];
}

//: One concise row of the detail panel's resource summary.
export interface TurnSummaryRow {
  label: string;
  value: string;
  /** Measurement context available on hover and to assistive technology. */
  detail: string | null;
}

//: Said when a run has no summary at all, and for a metric with no number.
export const NOT_COLLECTED = 'Not collected';
export const UNAVAILABLE = 'Unavailable';

/**
 * One fixed sentence per reason code.
 *
 * This is a security boundary, not a copy choice: the wire field is a bounded
 * enum so that nothing else can be said through it. Sentences name the effect
 * on the reader's number and never the mechanism's identifiers.
 */
const REASON_SENTENCES: Record<TurnResourceReasonCode, string> = {
  missing_thread_baseline:
    'Some threads had no starting counter, so their CPU before the first reading is not counted.',
  counter_regressed:
    'A CPU counter moved backwards between readings, so the affected part is a lower bound.',
  thread_identity_conflict:
    'Two readings could not be confirmed as the same thread, so the affected part is a lower bound.',
  observer_restarted:
    'The guest observer restarted during the turn, so part of the window was not observed.',
  ring_loss: 'The observer lost events during the turn, so fewer processes were seen than ran.',
  filesystem_attribution_unverified:
    'The kernel counters do not prove which filesystem this I/O reached, so no value is reported.',
  pss_unreadable:
    'Some memory readings failed, so the peak is the highest complete reading rather than the true highest.',
  address_space_share_unverified:
    'The member\u2019s memory could not be separated from its siblings beyond doubt, so the peak is a lower bound.',
  sampling_over_budget:
    'Memory sampling fell behind its schedule during the turn, so some of the window was not sampled.',
  window_prefix_retained:
    'Collection stopped before the end of the turn, so only the observed prefix is reported.',
  observer_unavailable:
    'No guest observer was available, so this part of the measurement was not collected.',
  not_a_kata_guest:
    'This execution did not run in a Kata guest, where these counters do not exist.',
  source_mismatch:
    'These executions were measured by different collector versions, so their totals are not combined.',
};

//: The one thing an unrecognized code is allowed to say.
export const UNKNOWN_REASON_SENTENCE =
  'Part of this measurement is incomplete for an unreported reason.';

/** The fixed sentence for one reason code, never the code itself. */
export function reasonSentence(code: TurnResourceReasonCode): string {
  return Object.hasOwn(REASON_SENTENCES, code) ? REASON_SENTENCES[code] : UNKNOWN_REASON_SENTENCE;
}

/**
 * The sentences for a metric's reason codes, in the order the server sent them.
 *
 * A code that arrives twice is said once. The list is capped so a pathological
 * payload cannot push the rest of the panel off the screen; the cap is stated
 * rather than silent.
 */
export function reasonSentences(codes: readonly TurnResourceReasonCode[], max = 3): string[] {
  const sentences: string[] = [];
  for (const code of codes) {
    const sentence = reasonSentence(code);
    if (!sentences.includes(sentence)) sentences.push(sentence);
  }
  if (sentences.length <= max) return sentences;
  return [...sentences.slice(0, max), `And ${sentences.length - max} more reasons.`];
}

/**
 * The legible form of a CPU time, always in CPU-seconds.
 *
 * CPU time is summed scheduler time: a thread that ran on two cores for ten
 * seconds contributes about twenty, so a value above the turn's wall-clock
 * duration is correct and is never clamped. Sub-second values keep enough
 * places to stay distinguishable from zero, because a small non-zero CPU time
 * is a different statement from no measurement at all.
 */
export function formatCpuSeconds(usec: number | null | undefined): string | null {
  if (typeof usec !== 'number' || !Number.isFinite(usec) || usec < 0) return null;
  const seconds = usec / 1_000_000;
  if (seconds >= 100) return `${Math.round(seconds)} CPU-s`;
  // Under 100 the tenth still carries meaning — "18.2 CPU-s" is a different
  // statement from "18 CPU-s" — and a whole number keeps no trailing zero.
  if (seconds >= 1) return `${trim(seconds)} CPU-s`;
  // Below one second the value must stay distinguishable from a true zero: a
  // measured millisecond of CPU is not "0 CPU-s", and rounding it into one
  // would render a measurement and an absence identically. Three significant
  // places are kept instead.
  if (seconds === 0) return '0 CPU-s';
  const places = Math.min(9, Math.max(3, 2 - Math.floor(Math.log10(seconds))));
  return `${seconds.toFixed(places)} CPU-s`;
}

/** Round as the reader will see it, so "1023.6 MiB" climbs to GiB rather than printing as 1024. */
function rounded(value: number): number {
  return value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
}

function trim(value: number): string {
  const shown = rounded(value);
  return Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
}

/**
 * A span of time as a reader would say it.
 *
 * Kept local to this module: it is only ever used for windows and sampling
 * periods, and a page must not reach into a client component for a formatter.
 * A missing span returns null rather than a zero, so "no cut-off was reported"
 * never renders as "reached 0ms".
 */
export function formatSpan(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = rounded(ms / 1000);
  if (seconds < 60) return `${trim(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

/**
 * The legible form of a memory quantity, in binary units.
 *
 * Binary because the sandbox measures pages: "1.2 GiB" is a number a reader
 * can compare against a process's own report, while a decimal relabelling of
 * the same bytes is not.
 */
export function formatMemoryBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return null;
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
  let value = bytes;
  let unit = 0;
  while (rounded(value) >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${trim(value)} ${units[unit]}`;
}

/** The sampling period a memory peak was measured at, as its method sentence. */
export function memoryMethod(intervalMs: number): string {
  const period = formatSpan(intervalMs) ?? `${intervalMs}ms`;
  return `sampled every ${period}`;
}

/**
 * Whether a metric is a lower bound, and therefore whether it may read ">=".
 *
 * Only additive quantities can: CPU and same-method I/O sum across threads and
 * processes, so a missing part leaves a floor. A memory peak is a maximum, so
 * a missing part leaves it observed at best — never a floor — and prefixing it
 * with ">=" would claim a bound the number does not have.
 */
function isLowerBound(metric: { status: ResourceMetricStatus }): boolean {
  return metric.status === 'partial';
}

function withLowerBound(value: string, partial: boolean): string {
  return partial ? `\u2265 ${value}` : value;
}

/** The suffix a still-running or interrupted window puts on every value it has. */
function windowNote(state: TurnResourceState | 'interrupted'): string[] {
  if (state === 'collecting') return ['still collecting'];
  if (state === 'interrupted') return ['collection was interrupted'];
  return [];
}

/**
 * The compact Runs-list CPU cell.
 *
 * A value above the run's duration is deliberately not clamped and not
 * explained away: it is the sum of the member's own scheduler time across
 * every thread it ran, which is exactly what an additive CPU bill is.
 */
export function cpuCell(
  usec: number | null,
  metric: ResourceMetricBriefApi,
  state: TurnResourceState | 'interrupted',
): ResourceCell {
  const title = reasonSentences(metric.reason_codes).join(' ') || undefined;
  const shown = usec === null || metric.status === 'unavailable' ? null : formatCpuSeconds(usec);
  if (shown === null) return { text: UNAVAILABLE, title };
  return {
    text: [withLowerBound(shown, isLowerBound(metric)), ...windowNote(state)].join(' \u00b7 '),
    title,
  };
}

/**
 * The compact Runs-list memory cell.
 *
 * "sampled" is part of the value, not a footnote: the peak comes from a census
 * that cannot see between ticks, so it is never presented as a physical total
 * or as a simultaneous platform peak. A partial peak adds "observed" and
 * deliberately not ">=".
 */
export function memoryCell(
  bytes: number | null,
  metric: ResourceMetricBriefApi,
  state: TurnResourceState | 'interrupted',
): ResourceCell {
  const title = reasonSentences(metric.reason_codes).join(' ') || undefined;
  const shown = bytes === null || metric.status === 'unavailable' ? null : formatMemoryBytes(bytes);
  if (shown === null) return { text: UNAVAILABLE, title };
  const markers = isLowerBound(metric) ? ['observed'] : [];
  return {
    text: [shown, 'sampled', ...markers, ...windowNote(state)].join(' \u00b7 '),
    title,
  };
}

/** How much of the run the numbers cover, when the run has more than one execution. */
export function attemptScope(measured = 0, total = 0): { covered: boolean; label: string } {
  return {
    covered: total > 1 && measured < total,
    label: `${measured.toLocaleString()} of ${total.toLocaleString()} executions measured`,
  };
}

/**
 * Whether the numbers cover every execution of the run.
 *
 * A run whose summary covers three of five attempts must say so wherever it is
 * shown; otherwise it reads as the run's total. A single-execution run says
 * nothing, because there is no gap to report.
 */
export function attemptCoverageNote(measured = 0, total = 0): string | null {
  const scope = attemptScope(measured, total);
  return scope.covered ? `${scope.label}; this is not the run\u2019s total.` : null;
}

/**
 * The state of a turn's resource accounting, from the server's own state.
 *
 * Independence is the point: an I/O metric that no sandbox can produce must
 * not turn a complete CPU measurement into a partial one, and a complete CPU
 * measurement must not promote an unmeasured one. So the metrics are not
 * folded in here — `runStateLabel` decides what a finished window reads as.
 */
export function completenessOf(state: TurnResourceState | 'interrupted'): ResourceCompleteness {
  if (state === 'collecting') return 'collecting';
  if (state === 'interrupted') return 'interrupted';
  return 'complete';
}

/** The word the detail panel shows for each metric status. */
export function metricStatusLabel(status: ResourceMetricStatus): string {
  if (status === 'available') return 'complete';
  if (status === 'partial') return 'partial';
  return 'unavailable';
}

/**
 * The coarse state word for the Runs list.
 *
 * `final` is downgraded to `partial` as soon as any one metric is short or
 * missing: "Complete" is a claim about every metric, and it is never made from
 * a finished turn alone.
 */
export function runStateLabel(summary: {
  state: TurnResourceState | 'interrupted';
  cpu: ResourceMetricBriefApi;
  memory: ResourceMetricBriefApi;
  storage_io: ResourceMetricBriefApi;
}): ResourceCompleteness {
  const state = completenessOf(summary.state);
  if (state !== 'complete') return state;
  const statuses = [summary.cpu.status, summary.memory.status, summary.storage_io.status];
  return statuses.every((status) => status === 'available') ? 'complete' : 'partial';
}

/** The capitalized form of a completeness, for a table cell. */
export function completenessLabel(completeness: ResourceCompleteness): string {
  if (completeness === 'complete') return 'Complete';
  if (completeness === 'partial') return 'Partial';
  if (completeness === 'collecting') return 'Collecting';
  if (completeness === 'interrupted') return 'Interrupted';
  return NOT_COLLECTED;
}

/**
 * The sentence an interrupted collection must always be read with.
 *
 * The window ended without a final report, so what is stored is a prefix of
 * the turn. The cut-off is named because it is the difference between a short
 * turn and a truncated measurement.
 */
export function interruptedSentence(observedThroughMs: number | null): string {
  const reached = formatSpan(observedThroughMs);
  if (reached === null) {
    return 'Collection was interrupted before the turn ended; the cut-off is not reported, and this is not the turn\u2019s total.';
  }
  return `Collection interrupted: data reached ${reached} of the turn, and this is not the turn\u2019s total.`;
}

/** The sentence a still-running collection must always be read with. */
export function collectingSentence(observedThroughMs: number | null): string {
  const reached = formatSpan(observedThroughMs);
  if (reached === null) {
    return 'Collection is still running, so these numbers are a snapshot, not the turn\u2019s total.';
  }
  return `Collection is still running: data has reached ${reached}, and this is a snapshot, not the turn\u2019s total.`;
}

/**
 * The Runs-list state cell, which also carries how far collection actually got.
 *
 * An interrupted or still-collecting run says so here, in the cell a reader
 * scans, so a short prefix can never be read as the run's final total. The
 * text is the coarse state alone; whether the window is over and whether its
 * metrics agree are separate questions, and both are left to the cells beside
 * it so a settled turn with an unsupported metric is not called complete.
 */
export function stateCell(summary: {
  state: TurnResourceState | 'interrupted';
  observed_through_ms: number | null;
  attempts_measured?: number;
  attempts_total?: number;
}): ResourceCell {
  const completeness = completenessOf(summary.state);
  const titles: string[] = [];
  if (completeness === 'interrupted') titles.push(interruptedSentence(summary.observed_through_ms));
  else if (completeness === 'collecting') {
    titles.push(collectingSentence(summary.observed_through_ms));
  } else titles.push('Collection finished with the turn.');
  const coverage = attemptCoverageNote(summary.attempts_measured, summary.attempts_total);
  if (coverage) titles.push(coverage);
  return { text: completenessLabel(completeness), title: titles.join(' ') };
}

/** The cell for a run whose attempts carry no summary at all. */
export function notCollectedStateCell(attempts_measured = 0, attempts_total = 0): ResourceCell {
  const coverage = attemptCoverageNote(attempts_measured, attempts_total);
  return {
    text: NOT_COLLECTED,
    title: coverage
      ? `No whole-turn measurement was recorded for this run. ${coverage}`
      : 'No whole-turn measurement was recorded for this run.',
  };
}

export interface TurnResourceCells {
  completeness: ResourceCompleteness;
  cpu: ResourceCell;
  memory: ResourceCell;
  state: ResourceCell;
}

/**
 * The compact cells for one run, or the not-collected cell when it has none.
 *
 * `title` on each cell carries the sentences that do not fit in a column, so a
 * reader who wants the caveat does not have to leave the list to find it. The
 * cells are derived only from the summary they are given, so a cell can never
 * carry a previous row's value.
 */
export function runResourceCells(
  summary: RunResourceSummaryApi | null | undefined,
): TurnResourceCells {
  if (!summary) {
    return {
      completeness: 'unavailable',
      cpu: { text: NOT_COLLECTED },
      memory: { text: NOT_COLLECTED },
      state: notCollectedStateCell(),
    };
  }
  return {
    completeness: runStateLabel(summary),
    cpu: cpuCell(summary.cpu_usec, summary.cpu, summary.state),
    memory: memoryCell(summary.memory_peak_bytes, summary.memory, summary.state),
    state: stateCell(summary),
  };
}

/**
 * The per-metric completeness sentence for the detail panel.
 *
 * Every metric is stated on its own, with its own reasons. A reader must be
 * able to see that the CPU number is complete and the I/O number does not
 * exist without either fact hiding the other.
 */
export function metricCompletenessSentence(label: string, metric: ResourceMetricBriefApi): string {
  const reasons = reasonSentences(metric.reason_codes);
  const head = `${label} ${metricStatusLabel(metric.status)}.`;
  return reasons.length ? `${head} ${reasons.join(' ')}` : head;
}

/** The one-line completeness summary of the whole turn, per metric. */
export function completenessSentence(summary: {
  cpu: ResourceMetricBriefApi;
  memory: ResourceMetricBriefApi;
  storage_io: ResourceMetricBriefApi;
}): string {
  return [
    metricCompletenessSentence('CPU', summary.cpu),
    metricCompletenessSentence('memory sampling', summary.memory),
    metricCompletenessSentence('I/O', summary.storage_io),
  ].join(' ');
}

/** What a peak is, so a sampled maximum is never mistaken for a physical total. */
export const MEMORY_BASIS =
  'Proportional resident memory, shared between users of the same pages; it is not machine memory and not a sum over executions';

function memoryBasis(metric: TurnMemoryResourcesApi): string {
  return isLowerBound(metric)
    ? `${MEMORY_BASIS}. This peak was observed, so the true peak may be higher.`
    : `${MEMORY_BASIS}.`;
}

/** Read/write byte counters, stated per direction because either can stand alone. */
function ioValue(storage: TurnStorageIoResourcesApi): string | null {
  const read = formatMemoryBytes(storage.read_bytes);
  const write = formatMemoryBytes(storage.write_bytes);
  if (read === null && write === null) return null;
  return `${read ?? UNAVAILABLE} read / ${write ?? UNAVAILABLE} written`;
}

/** Average share of this sandbox's CPU quota, only for a complete execution. */
export function averageCpuUsage(summary: TurnResourceSummaryApi): number | null {
  const { cpu, window } = summary;
  const cores = cpu.capacity_cores;
  const duration = window?.duration_ms;
  if (
    summary.state !== 'final' ||
    cpu.status !== 'available' ||
    cpu.usec === null ||
    !Number.isFinite(cpu.usec) ||
    cpu.usec < 0 ||
    typeof cores !== 'number' ||
    !Number.isFinite(cores) ||
    cores <= 0 ||
    typeof duration !== 'number' ||
    !Number.isFinite(duration) ||
    duration <= 0 ||
    window.observed_through_ms !== duration
  )
    return null;
  const percent = (cpu.usec / 1000 / duration / cores) * 100;
  // A contradictory counter or quota must not be clamped into a plausible reading.
  return Number.isFinite(percent) && percent <= 100 ? percent : null;
}

function cpuReading(summary: TurnResourceSummaryApi): ResourceReading {
  const percent = averageCpuUsage(summary);
  const notes = reasonSentences(summary.cpu.reason_codes);
  if (summary.state === 'interrupted')
    notes.unshift(interruptedSentence(summary.window?.observed_through_ms));
  else if (summary.state === 'collecting')
    notes.unshift(collectingSentence(summary.window?.observed_through_ms));
  if (percent === null) {
    notes.unshift(
      'Average CPU usage requires a complete execution measurement and its recorded CPU quota.',
    );
  } else {
    notes.unshift(
      'Average share of the sandbox CPU quota used by this execution, including the agent and its tools.',
    );
  }
  return {
    value:
      percent === null ? '—' : percent > 0 && percent < 0.1 ? '<0.1%' : `${percent.toFixed(1)}%`,
    method: 'CPU time / (execution duration × sandbox CPU quota)',
    notes,
  };
}

function memoryReading(summary: TurnResourceSummaryApi): ResourceReading {
  const notes = reasonSentences(summary.memory.reason_codes);
  const shown =
    summary.memory.status === 'unavailable' ? null : formatMemoryBytes(summary.memory.peak_bytes);
  if (shown === null) {
    return { value: UNAVAILABLE, method: memoryMethod(summary.memory.sample_interval_ms), notes };
  }
  return {
    value: [shown, 'sampled', ...(isLowerBound(summary.memory) ? ['observed'] : [])].join(
      ' \u00b7 ',
    ),
    method: memoryMethod(summary.memory.sample_interval_ms),
    notes: [memoryBasis(summary.memory), ...notes],
  };
}

function storageReading(summary: TurnResourceSummaryApi): ResourceReading {
  const notes: string[] = [];
  if (summary.state === 'interrupted')
    notes.push(interruptedSentence(summary.window?.observed_through_ms));
  else if (summary.state === 'collecting') {
    notes.push(collectingSentence(summary.window?.observed_through_ms));
  }
  notes.push(...reasonSentences(summary.storage_io.reason_codes));
  const shown = summary.storage_io.status === 'unavailable' ? null : ioValue(summary.storage_io);
  if (shown === null) {
    return { value: UNAVAILABLE, method: summary.storage_io.method, notes };
  }
  return {
    value: withLowerBound(shown, isLowerBound(summary.storage_io)),
    method: summary.storage_io.method,
    notes,
  };
}

/** Turn-level measurement context, in the contract's order. */
export function turnSummaryReadings(summary: TurnResourceSummaryApi): ResourceReading[] {
  return [cpuReading(summary), memoryReading(summary), storageReading(summary)];
}

/** Four scannable values for one execution; reasons stay attached to their metric. */
export function turnSummaryRows(summary: TurnResourceSummaryApi): TurnSummaryRow[] {
  const [cpu, memory, storage] = turnSummaryReadings(summary);
  const memoryValue =
    summary.memory.status === 'unavailable' ? null : formatMemoryBytes(summary.memory.peak_bytes);
  const memoryObserved = summary.memory.status === 'partial' || summary.state !== 'final';
  const storagePartial = summary.storage_io.status === 'partial' || summary.state !== 'final';
  const storageValue = (bytes: number | null) => {
    const shown = summary.storage_io.status === 'unavailable' ? null : formatMemoryBytes(bytes);
    return shown === null ? UNAVAILABLE : withLowerBound(shown, storagePartial);
  };
  const context = (reading: ResourceReading) => {
    const method =
      reading.method === 'task_io_accounting' ? 'Process I/O accounting' : reading.method;
    return [method ? `${method}.` : null, ...reading.notes].filter(Boolean).join(' ');
  };
  return [
    { label: 'CPU usage (avg)', value: cpu.value, detail: context(cpu) },
    {
      label: 'Peak memory (sampled)',
      value:
        memoryValue === null ? UNAVAILABLE : `${memoryValue}${memoryObserved ? ' · observed' : ''}`,
      detail: context(memory),
    },
    {
      label: 'Storage read',
      value: storageValue(summary.storage_io.read_bytes),
      detail: context(storage),
    },
    {
      label: 'Storage write',
      value: storageValue(summary.storage_io.write_bytes),
      detail: context(storage),
    },
  ];
}
