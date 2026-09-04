'use client';

// The per-turn account: how this turn was continued, how long it took, and
// what it consumed on a machine its owner cannot see.
//
// Two rules run through every line of this file, both from the observability
// design's §11, and both are the reason it is worth showing numbers at all:
//
//   1. A number we did not measure is never rendered as a number. The
//      sampler reports `coverage`, and anything short of a full window is
//      labelled a floor rather than quietly rounded into a total.
//   2. Timings derived from message arrival are labelled approximate. A tool
//      row's window starts after the model has produced the call and brackets
//      execution with platform event-processing delay until per-call
//      boundaries land.

import { useMemo, useState } from 'react';

import type {
  AgentEvent,
  AgentJob,
  ProcessObservation,
  ToolCallResources,
  TurnResources,
} from './types';

/** One tool call's derived window, paired from the event stream. */
export interface ToolTiming {
  tool: string;
  detail: string;
  ms: number | null;
  isError: boolean;
  /** Joins this row to the host's measurement of the same call, when there is one. */
  toolUseId: string | null;
}

const CONTINUITY_LABEL: Record<string, { text: string; tone: string; title: string }> = {
  hot: {
    text: 'hot',
    tone: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-emerald-600/20 dark:ring-emerald-500/25',
    title:
      'Continued in the same live process as the previous turn. The agent kept everything it had in mind, and its workspace was never rebuilt.',
  },
  warm_native: {
    text: 'warm',
    tone: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400 ring-emerald-600/20 dark:ring-emerald-500/25',
    title:
      'Reopened the agent\u2019s own saved session in the workspace this thread already had. Its memory of earlier turns and the files it was working on are both intact; only its process was restarted.',
  },
  cold_native: {
    text: 'cold',
    tone: 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400 ring-amber-600/20 dark:ring-amber-500/25',
    title:
      'Rebuilt in a fresh sandbox from the saved session and the saved code. The agent still remembers earlier turns, but anything it had installed or generated outside version control is gone.',
  },
  replay: {
    text: 'replay',
    tone: 'bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-400 ring-rose-600/20 dark:ring-rose-500/25',
    title:
      'The agent\u2019s working memory of earlier turns was not available for this turn, so it was given the written transcript instead. It can read what was said, but it may not recall why it did something, and it will not know about anything it set up that the transcript does not mention. This happens when the previous turn\u2019s session could not be saved, or when the thread moved to another machine.',
  },
};

const CONTEXT_LABEL: Record<string, { text: string; tone: string; title: string }> = {
  new: {
    text: 'new',
    tone: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-300 ring-gray-500/20 dark:ring-white/10',
    title: 'This is the first turn in the chat, so there is no earlier agent context to restore.',
  },
  live: {
    text: 'context live',
    tone: 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400 ring-sky-600/20 dark:ring-sky-500/25',
    title:
      'The supervisor still held the live native agent session and workspace, so it could continue without reconstructing the session. Process ids are deliberately not part of the public contract.',
  },
  restored: {
    text: 'restored',
    tone: 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400 ring-sky-600/20 dark:ring-sky-500/25',
    title:
      'The Agent CLI process was reopened and loaded its exact native session from the same retained sandbox. The container may have been restarted, or only this member process may have been re-keyed; the prior transcript was not pasted in as a substitute.',
  },
  lost: {
    text: 'context lost',
    tone: 'bg-rose-50 dark:bg-rose-500/10 text-rose-700 dark:text-rose-400 ring-rose-600/20 dark:ring-rose-500/25',
    title:
      'The platform could not verify the retained native session. It did not silently start a blank session and call that a continuation.',
  },
};

/**
 * Round the way the reader will see it, so callers can branch on the printed
 * value rather than the one before rounding — the gap between the two is how
 * "60.0s" and "1024MB" happen.
 *
 * The decimal is kept below 100 because that is where it still carries
 * meaning — "18.2s" of CPU is a different statement from "18s" — and dropped
 * above it, where a tenth of a gigabyte is noise in a footer.
 */
function rounded(value: number): number {
  return value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
}

function trim(value: number): string {
  const shown = rounded(value);
  return Number.isInteger(shown) ? String(shown) : shown.toFixed(1);
}

export function formatDuration(ms: number | null | undefined): string | null {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  // Rounding is decided once, before the branch: 59.96s rounds to 60, and a
  // branch taken on the unrounded value renders it as "60.0s" — or, one
  // minute later, as "1m 60s".
  const seconds = rounded(ms / 1000);
  if (seconds < 60) return `${trim(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds - minutes * 60);
  return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
}

export function formatBytes(bytes: number | null | undefined): string | null {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 0;
  // Compared after rounding, so a value that will *print* as 1024 climbs to
  // the next unit instead of rendering as "1024MB".
  while (rounded(value) >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${trim(value)}${units[unit]}`;
}

/** CPU time, reported in the same unit a person would say it in. */
export function formatCpu(usec: number | null | undefined): string | null {
  if (typeof usec !== 'number' || !Number.isFinite(usec) || usec < 0) return null;
  return formatDuration(usec / 1000);
}

/**
 * Time each tool call in one attempt.
 *
 * The window normally arrives already resolved on the `tool_use` row: the
 * adapter folds a result into its call and keeps the span there, because by
 * the time anything downstream sees the stream the result row is gone. The
 * pairing below is the fallback for rows that were never folded — a call
 * whose payload carried its own output, or a stream that ended mid-call.
 *
 * Message-level by construction: the window opens when the platform parses
 * the model's call, not when the sandbox begins running it, so it brackets
 * execution with platform delay on either side. Unpaired calls keep their row
 * with a null duration rather than being dropped — a call that never returned
 * is exactly what a reader is looking for.
 */
export function toolTimings(events: AgentEvent[], attemptNo?: number): ToolTiming[] {
  const scoped =
    attemptNo === undefined
      ? events
      : events.filter((event) => event.attemptNo === undefined || event.attemptNo === attemptNo);
  // Results that survived folding, keyed within their own attempt: a retried
  // turn repeats its tool ids, and pairing on the bare id would time a call
  // against a different attempt's result.
  const results = new Map<string, { at: string | null; isError: boolean }>();
  for (const event of scoped) {
    if (event.kind === 'tool_result' && event.toolUseId) {
      results.set(`${event.attemptNo ?? ''}:${event.toolUseId}`, {
        at: event.at ?? null,
        isError: event.isError,
      });
    }
  }
  const timings: ToolTiming[] = [];
  for (const event of scoped) {
    if (event.kind !== 'tool_use') continue;
    // The common case: the adapter folded this call's result into the row and
    // resolved the window there, because the result row no longer exists by
    // the time anything downstream sees the stream.
    const result = event.id ? results.get(`${event.attemptNo ?? ''}:${event.id}`) : undefined;
    let ms = event.durationMs ?? null;
    if (ms === null) {
      const start = event.at ? Date.parse(event.at) : NaN;
      const end = result?.at ? Date.parse(result.at) : NaN;
      ms = Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
    }
    timings.push({
      tool: event.tool,
      detail: event.detail,
      ms,
      toolUseId: event.id ?? null,
      // Both sources: folding moves the flag onto the call's own row, while
      // a row that was never folded still has its result standing beside it.
      isError: event.outputIsError === true || result?.isError === true,
    });
  }
  return timings;
}

/** The measured stats, in display order, skipping anything unmeasured. */
function resourceStats(resources: TurnResources): { label: string; value: string }[] {
  const net =
    formatBytes(resources.netRxBytes) || formatBytes(resources.netTxBytes)
      ? `↓${formatBytes(resources.netRxBytes) ?? '—'} ↑${formatBytes(resources.netTxBytes) ?? '—'}`
      : null;
  // Disk is deliberately absent. The counter behind it is block io, and a
  // container that wrote a known 64MiB and called `sync` reported that
  // exactly on one run and zero on the next — the write absorbed above the
  // block layer, never reaching the counter (pinned in
  // tests/external/test_resource_sampler_live.py). A reader shown `disk w0B`
  // concludes the sandbox wrote nothing, which is the one thing this number
  // cannot say, and there is no way to tell the two cases apart at read
  // time. It is still recorded; it is no longer presented as a measurement.
  return [
    { label: 'CPU', value: formatCpu(resources.cpuUsec) },
    { label: 'peak mem', value: formatBytes(resources.memPeakBytes) },
    { label: 'net', value: net },
  ].filter((stat): stat is { label: string; value: string } => Boolean(stat.value));
}

/** A measured cell, or the dash that says nothing was measured here. */
function MeasuredCell({ value, shared }: { value: string | null; shared: boolean }) {
  if (value === null) {
    return <span className="text-gray-400 dark:text-gray-600">—</span>;
  }
  return (
    <span className={shared ? 'text-gray-500 dark:text-gray-400' : undefined}>
      {value}
      {shared ? <span className="ml-0.5">*</span> : null}
    </span>
  );
}

/**
 * One call's CPU or memory, from whichever source could actually say.
 *
 * The sampler's reading is shown when it spanned the call. Otherwise the
 * guest kernel's exit-time accounting of the call's own processes is shown,
 * marked so the reader knows it counts child processes only — work the
 * agent did inside its own resident process is in neither number. A floor,
 * where a process the call may own was left out, is prefixed rather than
 * silently rounded into a total.
 */
function ConsumptionCell({
  sampled,
  shared,
  accounted,
  partial,
}: {
  sampled: string | null;
  shared: boolean;
  accounted: string | null;
  partial: boolean;
}) {
  if (sampled !== null) {
    return <MeasuredCell value={sampled} shared={shared} />;
  }
  if (accounted === null) {
    return <MeasuredCell value={null} shared={false} />;
  }
  return (
    <span>
      {partial ? '≥' : ''}
      {accounted}
      <span className="ml-0.5 text-gray-400 dark:text-gray-500">ᵖ</span>
    </span>
  );
}

/** Read and write bytes as one cell, or the dash when neither was accounted. */
function formatIo(
  read: number | null | undefined,
  write: number | null | undefined,
): string | null {
  const shownRead = formatBytes(read);
  const shownWrite = formatBytes(write);
  if (shownRead === null && shownWrite === null) return null;
  return `r${shownRead ?? '—'} w${shownWrite ?? '—'}`;
}

/**
 * The longest call in the turn, which is as close to "the expensive one" as
 * this measurement gets.
 *
 * Ranking by what a call consumed would be the question a reader actually
 * has, and it is not answerable: almost no call is long enough for the
 * sampler to have measured it, so a CPU ranking would quietly order the
 * handful that were and ignore the rest. Duration is known for every call
 * that returned, so that is what this ranks by — and the label says so
 * rather than implying a cost ranking it did not perform.
 */
function longestCall(timings: ToolTiming[]): ToolTiming | null {
  let longest: ToolTiming | null = null;
  for (const timing of timings) {
    if (timing.ms === null) continue;
    if (longest === null || timing.ms > (longest.ms ?? -1)) longest = timing;
  }
  return longest;
}

/**
 * Where the turn's wall clock went.
 *
 * The most explanatory number the footer can carry, and it costs no new
 * measurement: both halves were already recorded. On the production turns
 * this was built against, the sandbox was busy for around one percent of the
 * turn. The remainder was dominated by model work, but this measurement also
 * includes harness and platform overhead and does not pretend to separate it.
 *
 * Tool time is the union of the calls' windows, never their sum, so it can
 * never exceed the turn it belongs to.
 */
function TimeSplit({
  durationMs,
  toolTimeMs,
}: {
  durationMs: number | null;
  toolTimeMs: number | null;
}) {
  if (durationMs === null || toolTimeMs === null) return null;
  const rest = Math.max(0, durationMs - toolTimeMs);
  return (
    <p className="mb-1.5 text-[12px] text-gray-600 dark:text-gray-400">
      Of <span className="tabular-nums">{formatDuration(durationMs)}</span>:{' '}
      <span className="tabular-nums text-gray-800 dark:text-gray-200">
        {formatDuration(toolTimeMs)}
      </span>{' '}
      in tools, <span className="tabular-nums">{formatDuration(rest)}</span> outside tools (model +
      platform overhead).
    </p>
  );
}

/**
 * One sentence saying what the durations in the table actually measure.
 *
 * Two different things can be in one table: a call the sandbox's own hooks
 * bracketed, and a call timed from when the platform parsed its events. The
 * second is wider than the execution by however long parsing took, and its
 * windows overlap far more readily — which is why a third of production's
 * calls could only be reported as sharing a measurement.
 */
export function timingSourceNote(calls: ToolCallResources[], rows: number): string {
  const exact = calls.filter((call) => call.timingSource === 'exact').length;
  if (exact === 0) {
    return (
      'Durations are measured from when each call\u2019s events were parsed, which brackets the ' +
      'execution from outside rather than timing it.'
    );
  }
  if (exact >= rows) {
    return 'Durations are the sandbox\u2019s own tool boundaries, recorded as each call ran.';
  }
  return (
    `${exact} of ${rows} durations are the sandbox\u2019s own tool boundaries; the rest are ` +
    'bracketed from outside, from when each call\u2019s events were parsed.'
  );
}

function ToolTimingTable({
  timings,
  calls,
  truncated,
  processObservationComplete,
}: {
  timings: ToolTiming[];
  calls: ToolCallResources[];
  truncated: boolean;
  processObservationComplete: boolean;
}) {
  const byId = useMemo(() => {
    const index = new Map<string, ToolCallResources[]>();
    for (const call of calls) {
      const matches = index.get(call.toolUseId) ?? [];
      matches.push(call);
      index.set(call.toolUseId, matches);
    }
    return index;
  }, [calls]);
  const usedById = new Map<string, number>();
  const rows = timings.map((timing) => {
    const matches = timing.toolUseId ? byId.get(timing.toolUseId) : undefined;
    const used = timing.toolUseId ? (usedById.get(timing.toolUseId) ?? 0) : 0;
    const recorded = matches?.[used];
    if (timing.toolUseId && recorded) usedById.set(timing.toolUseId, used + 1);
    return {
      timing: {
        ...timing,
        // A recorded null means the call never returned. Fall back only when
        // this attempt has no resource row for the call at all.
        ms: recorded ? recorded.durationMs : timing.ms,
      },
      measurement: recorded && recorded.sampleCount >= 2 ? recorded : null,
      processCount: recorded?.processCount ?? 0,
      timingSource: recorded?.timingSource ?? 'message',
      // Exit-time accounting of the call's own processes: present whenever
      // something that belonged to this call alone exited, however short.
      accountedCpuUsec: recorded?.processCpuUsec ?? null,
      accountedMemPeakBytes: recorded?.processMemPeakBytes ?? null,
      accountedPartial: recorded?.processResourcesPartial === true,
    };
  });
  const longest = longestCall(rows.map((row) => row.timing));
  const anyAccounted = rows.some(
    (row) =>
      row.measurement === null &&
      (row.accountedCpuUsec !== null || row.accountedMemPeakBytes !== null),
  );
  const anyAccountedPartial = rows.some((row) => row.measurement === null && row.accountedPartial);
  const anyUnmeasured = rows.some(
    (row) =>
      row.measurement === null &&
      row.accountedCpuUsec === null &&
      row.accountedMemPeakBytes === null,
  );
  const anyShared = rows.some((row) => row.measurement !== null && !row.measurement.exclusive);

  return (
    <div className="flex flex-col gap-1.5">
      {longest && timings.length > 1 ? (
        <p className="text-[12px] text-gray-600 dark:text-gray-400">
          Longest call: <span className="text-gray-800 dark:text-gray-200">{longest.tool}</span>
          {' · '}
          <span className="tabular-nums">{formatDuration(longest.ms)}</span>
        </p>
      ) : null}
      <table className="w-full text-[12px]">
        <thead>
          <tr className="text-left text-gray-500 dark:text-gray-400">
            <th className="py-1 font-medium">Tool</th>
            <th className="py-1 font-medium">Detail</th>
            <th className="py-1 text-right font-medium">Duration</th>
            <th className="py-1 text-right font-medium">Process starts</th>
            <th className="py-1 text-right font-medium">CPU</th>
            <th className="py-1 text-right font-medium">Peak mem</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => {
            const {
              measurement: call,
              processCount,
              timing,
              timingSource,
              accountedCpuUsec,
              accountedMemPeakBytes,
              accountedPartial,
            } = row;
            return (
              <tr
                key={`${timing.tool}-${index}`}
                className="border-t border-gray-100 dark:border-white/5"
              >
                <td className="py-1 pr-3 align-top text-gray-800 dark:text-gray-200">
                  {timing.tool}
                  {timing.isError ? (
                    <span className="ml-1 text-rose-600 dark:text-rose-400">failed</span>
                  ) : null}
                </td>
                <td className="max-w-0 truncate py-1 pr-3 align-top font-mono text-gray-500 dark:text-gray-400">
                  {timing.detail}
                </td>
                <td className="py-1 text-right align-top tabular-nums text-gray-700 dark:text-gray-300">
                  {formatDuration(timing.ms) ?? 'unfinished'}
                  <span className="block text-[10px] text-gray-400 dark:text-gray-500">
                    {timingSource === 'exact' ? 'sandbox timing' : 'outside bracket'}
                  </span>
                </td>
                <td className="py-1 pl-3 text-right align-top tabular-nums text-gray-700 dark:text-gray-300">
                  {processObservationComplete
                    ? processCount
                    : processCount > 0
                      ? `≥${processCount}`
                      : '—'}
                </td>
                <td className="py-1 pl-3 text-right align-top tabular-nums text-gray-700 dark:text-gray-300">
                  <ConsumptionCell
                    sampled={call ? formatCpu(call.cpuUsec) : null}
                    shared={call ? !call.exclusive : false}
                    accounted={formatCpu(accountedCpuUsec)}
                    partial={accountedPartial}
                  />
                </td>
                <td className="py-1 pl-3 text-right align-top tabular-nums text-gray-700 dark:text-gray-300">
                  <ConsumptionCell
                    sampled={call ? formatBytes(call.memPeakBytes) : null}
                    shared={call ? !call.exclusive : false}
                    accounted={formatBytes(accountedMemPeakBytes)}
                    partial={accountedPartial}
                  />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {anyAccounted ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          ᵖ CPU and peak memory accounted by the guest kernel as the call&apos;s own processes
          exited, exact for a call of any length. They cover child processes only, not work done
          inside the agent&apos;s own process; peak memory is the largest single process.
        </p>
      ) : null}
      {anyAccountedPartial ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          ≥ marks a call whose numbers leave out a process it may own: one shared with an
          overlapping call, or one still running when the turn ended.
        </p>
      ) : null}
      {anyUnmeasured ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          — in CPU or memory means the periodic sampler did not span the call and no exited child
          process was accounted to it alone. Process-start counts come from guest-kernel lifecycle
          events and do not have that sampling-window gap.
        </p>
      ) : null}
      {anyShared ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          * ran alongside other calls, which share the same reading. Those numbers cover all of
          them, so each is an upper bound.
        </p>
      ) : null}
      {truncated ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          This turn made more calls than are listed here.
        </p>
      ) : null}
    </div>
  );
}

function ProcessObservationTable({
  processes,
  complete,
  truncated,
  lost,
}: {
  processes: ProcessObservation[];
  complete: boolean;
  truncated: boolean;
  lost: number;
}) {
  return (
    <div className="mt-3">
      <p className="mb-1 text-[11px] font-medium text-gray-600 dark:text-gray-300">
        Guest process starts
      </p>
      {processes.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-left text-gray-500 dark:text-gray-400">
                <th className="py-1 font-medium">Process</th>
                <th className="py-1 text-right font-medium">PID</th>
                <th className="py-1 text-right font-medium">Started</th>
                <th className="py-1 text-right font-medium">Lifetime</th>
                <th className="py-1 text-right font-medium">CPU</th>
                <th className="py-1 text-right font-medium">Peak RSS</th>
                <th className="py-1 text-right font-medium">I/O</th>
                <th className="py-1 text-right font-medium">Exit</th>
                <th className="py-1 text-right font-medium">Tool rows</th>
              </tr>
            </thead>
            <tbody>
              {processes.map((process) => (
                <tr
                  key={process.processSeq}
                  className="border-t border-gray-100 dark:border-white/5"
                >
                  <td
                    className="max-w-48 truncate py-1 pr-3 font-mono text-gray-700 dark:text-gray-300"
                    title={process.executable ?? process.comm}
                  >
                    {process.executable ?? process.comm}
                  </td>
                  <td className="py-1 text-right tabular-nums">{process.pid}</td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    +{formatDuration(process.startedOffsetMs)}
                  </td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    {formatDuration(process.durationMs) ?? 'still running'}
                  </td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    {formatCpu(process.cpuUsec) ?? '—'}
                  </td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    {formatBytes(process.memPeakBytes) ?? '—'}
                  </td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    {formatIo(process.ioReadBytes, process.ioWriteBytes) ?? '—'}
                  </td>
                  <td className="py-1 pl-3 text-right tabular-nums">{process.exitCode ?? '—'}</td>
                  <td className="py-1 pl-3 text-right tabular-nums">
                    {process.toolCallSeqs.length ? process.toolCallSeqs.join(', ') : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            CPU, peak RSS and I/O are the guest kernel&apos;s accounting of each process at exit, so
            a process still running shows none. I/O is bytes the process itself read from and
            dirtied to storage, so a write served from cache can still count and a read hit in cache
            does not.
          </p>
        </div>
      ) : complete ? (
        <p className="text-[11px] text-gray-500 dark:text-gray-400">
          The guest kernel observed no child-process start during this turn.
        </p>
      ) : null}
      {!complete ? (
        <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
          Process observation is partial
          {lost > 0 ? ` (${lost} process lifecycle event${lost === 1 ? '' : 's'} lost)` : ''}
          {truncated ? '; bounded observer data was incomplete' : ''}.
        </p>
      ) : null}
    </div>
  );
}

type Explained = 'continuity' | 'context' | 'partial' | 'unmeasured' | null;

export function TurnFooter({ job, attemptNo }: { job: AgentJob; attemptNo?: number }) {
  const [open, setOpen] = useState(false);
  // Which chip the reader asked about. A native `title` was the first
  // delivery for these, and it is the wrong one: it wants a second of
  // motionless hover, cannot be read on a touch screen, and holds three
  // sentences badly. The one thing this footer most needs to explain — why a
  // turn forgot — was the least likely to be seen.
  const [explained, setExplained] = useState<Explained>(null);
  const selectedTelemetry =
    attemptNo !== undefined && attemptNo > 0
      ? job.attempts.find((attempt) => attempt.no === attemptNo)?.telemetry
      : job;
  // Scoped to the attempt the reader is looking at: the body above already
  // switches with the attempt picker, and a footer that did not would label
  // one attempt's transcript with another's numbers.
  const timings = useMemo(() => toolTimings(job.events, attemptNo), [job.events, attemptNo]);
  const contextBadge = selectedTelemetry?.contextStatus
    ? CONTEXT_LABEL[selectedTelemetry.contextStatus]
    : undefined;
  // NEW/LIVE/RESTORED/LOST is the user-facing correctness contract. The old
  // HOT/WARM/COLD/REPLAY ladder remains useful for legacy attempts and admin
  // diagnostics, but showing both on one turn made one fact look like two
  // independent states. Fall back to it only for rows that predate context.
  const badge =
    !contextBadge && selectedTelemetry?.continuityMode
      ? CONTINUITY_LABEL[selectedTelemetry.continuityMode]
      : undefined;
  const duration = formatDuration(selectedTelemetry?.durationMs);
  const resources = selectedTelemetry?.resources;
  const stats = resources ? resourceStats(resources) : [];
  const processes = resources?.processes ?? [];
  const hasProcessObservation = Boolean(resources?.processObserver);
  const hasDetails = timings.length > 0 || hasProcessObservation;
  // A sampler that reported at all but covered only part of the window makes
  // every number below a floor. Saying so is the difference between a partial
  // measurement and a wrong one.
  // Warn only when the gap is material. Binding to a container the daemon
  // has not finished creating costs a fraction of a second that no threshold
  // can recover, so a warning at "anything short of perfect" fires on every
  // healthy turn — and a warning that always fires is one nobody reads.
  // Missing a tenth of a turn is worth saying; missing two percent is not.
  const partial = Boolean(resources && resources.coverage < 0.9 && stats.length > 0);
  const explanation =
    explained === 'continuity'
      ? badge?.title
      : explained === 'context'
        ? contextBadge?.title
        : explained === 'partial'
          ? `Only ${Math.round((resources?.coverage ?? 0) * 100)}% of this turn was sampled, so the ` +
            'numbers above are lower bounds rather than totals. This happens when the sandbox ' +
            'took a while to appear, or ended before the turn did.'
          : explained === 'unmeasured'
            ? hasProcessObservation
              ? 'The periodic sandbox resource counters were unavailable. The guest process ' +
                'lifecycle events below are a separate, event-driven measurement.'
              : 'This turn ran where the platform could not read the sandbox\u2019s counters, so ' +
                'nothing was measured. That is not the same as a turn that consumed nothing.'
            : null;
  const unmeasured = Boolean(resources && stats.length === 0);

  // `unmeasured` counts as something to say: a sampler that ran and could
  // read nothing is a different fact from a turn nobody tried to measure, and
  // only the first of the two has anything to tell the reader.
  if (
    !badge &&
    !contextBadge &&
    !duration &&
    stats.length === 0 &&
    timings.length === 0 &&
    !hasProcessObservation &&
    !unmeasured
  ) {
    return null;
  }

  return (
    <section className="mt-3 rounded-lg border border-gray-200 dark:border-white/10 bg-gray-50/60 dark:bg-gray-900/60 px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-600 dark:text-gray-400">
        {badge ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'continuity' ? null : 'continuity')}
            aria-expanded={explained === 'continuity'}
            title={badge.title}
            className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset transition-opacity hover:opacity-80 ${badge.tone}`}
          >
            {badge.text}
          </button>
        ) : null}
        {contextBadge ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'context' ? null : 'context')}
            aria-expanded={explained === 'context'}
            title={contextBadge.title}
            className={`inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset transition-opacity hover:opacity-80 ${contextBadge.tone}`}
          >
            {contextBadge.text}
          </button>
        ) : null}
        {duration ? <span className="tabular-nums">{duration}</span> : null}
        {stats.map((stat) => (
          <span key={stat.label} className="tabular-nums">
            <span className="text-gray-500 dark:text-gray-400">{stat.label} </span>
            {stat.value}
          </span>
        ))}
        {partial ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'partial' ? null : 'partial')}
            aria-expanded={explained === 'partial'}
            className="text-amber-700 dark:text-amber-400 underline-offset-2 hover:underline"
          >
            partial
          </button>
        ) : null}
        {unmeasured ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'unmeasured' ? null : 'unmeasured')}
            aria-expanded={explained === 'unmeasured'}
            className="text-gray-400 dark:text-gray-500 underline-offset-2 hover:underline"
          >
            resource data unavailable
          </button>
        ) : null}
        {hasDetails ? (
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="ml-auto text-gray-500 dark:text-gray-400 underline-offset-2 hover:text-gray-800 dark:hover:text-gray-200 hover:underline"
          >
            {open
              ? 'Hide'
              : timings.length > 0
                ? `${timings.length} tool ${timings.length === 1 ? 'call' : 'calls'}`
                : 'Process details'}
          </button>
        ) : null}
      </div>
      {explanation ? (
        <p className="mt-2 border-t border-gray-200 dark:border-white/10 pt-2 text-[12px] leading-relaxed text-gray-600 dark:text-gray-400">
          {explanation}
        </p>
      ) : null}
      {open && hasDetails ? (
        <div className="mt-2 border-t border-gray-200 dark:border-white/10 pt-2">
          {timings.length > 0 ? (
            <>
              <TimeSplit
                durationMs={selectedTelemetry?.durationMs ?? null}
                toolTimeMs={resources?.toolTimeMs ?? null}
              />
              <ToolTimingTable
                timings={timings}
                calls={resources?.toolCalls ?? []}
                truncated={resources?.toolCallsTruncated === true}
                processObservationComplete={resources?.processObservationComplete === true}
              />
            </>
          ) : null}
          {resources?.processObserver ? (
            <ProcessObservationTable
              processes={processes}
              complete={resources.processObservationComplete === true}
              truncated={resources.processesTruncated === true}
              lost={resources.processEventsLost ?? 0}
            />
          ) : null}
          {timings.length > 0 ? (
            <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
              {timingSourceNote(resources?.toolCalls ?? [], timings.length)}
            </p>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
