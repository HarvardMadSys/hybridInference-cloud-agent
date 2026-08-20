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
//      row's window starts when the model asked for the call, which includes
//      the model's own thinking before the tool ever ran, until per-call
//      boundaries land.

import { useMemo, useState } from 'react';

import type { AgentEvent, AgentJob, TurnResources } from './types';

/** One tool call's derived window, paired from the event stream. */
export interface ToolTiming {
  tool: string;
  detail: string;
  ms: number | null;
  isError: boolean;
}

const CONTINUITY_LABEL: Record<string, { text: string; tone: string; title: string }> = {
  hot: {
    text: 'hot',
    tone: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    title:
      'Continued in the same live process as the previous turn. The agent kept everything it had in mind, and its workspace was never rebuilt.',
  },
  warm_native: {
    text: 'warm',
    tone: 'bg-emerald-50 text-emerald-700 ring-emerald-600/20',
    title:
      'Reopened the agent\u2019s own saved session in the workspace this thread already had. Its memory of earlier turns and the files it was working on are both intact; only its process was restarted.',
  },
  cold_native: {
    text: 'cold',
    tone: 'bg-amber-50 text-amber-700 ring-amber-600/20',
    title:
      'Rebuilt in a fresh sandbox from the saved session and the saved code. The agent still remembers earlier turns, but anything it had installed or generated outside version control is gone.',
  },
  replay: {
    text: 'replay',
    tone: 'bg-rose-50 text-rose-700 ring-rose-600/20',
    title:
      'The agent\u2019s working memory of earlier turns was not available for this turn, so it was given the written transcript instead. It can read what was said, but it may not recall why it did something, and it will not know about anything it set up that the transcript does not mention. This happens when the previous turn\u2019s session could not be saved, or when the thread moved to another machine.',
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
 * Message-level by construction: the window opens when the model asked for
 * the call, not when the sandbox began running it, so it carries whatever
 * thinking preceded execution. Unpaired calls keep their row with a null
 * duration rather than being dropped — a call that never returned is exactly
 * what a reader is looking for.
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
  const io =
    formatBytes(resources.ioReadBytes) || formatBytes(resources.ioWriteBytes)
      ? `r${formatBytes(resources.ioReadBytes) ?? '—'} w${formatBytes(resources.ioWriteBytes) ?? '—'}`
      : null;
  return [
    { label: 'CPU', value: formatCpu(resources.cpuUsec) },
    { label: 'peak mem', value: formatBytes(resources.memPeakBytes) },
    { label: 'net', value: net },
    { label: 'disk', value: io },
  ].filter((stat): stat is { label: string; value: string } => Boolean(stat.value));
}

function ToolTimingTable({ timings }: { timings: ToolTiming[] }) {
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr className="text-left text-gray-500">
          <th className="py-1 font-medium">Tool</th>
          <th className="py-1 font-medium">Detail</th>
          <th className="py-1 text-right font-medium">Duration</th>
        </tr>
      </thead>
      <tbody>
        {timings.map((timing, index) => (
          <tr key={`${timing.tool}-${index}`} className="border-t border-gray-100">
            <td className="py-1 pr-3 align-top text-gray-800">
              {timing.tool}
              {timing.isError ? <span className="ml-1 text-rose-600">failed</span> : null}
            </td>
            <td className="max-w-0 truncate py-1 pr-3 align-top font-mono text-gray-500">
              {timing.detail}
            </td>
            <td className="py-1 text-right align-top tabular-nums text-gray-700">
              {formatDuration(timing.ms) ?? 'unfinished'}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

type Explained = 'continuity' | 'partial' | 'unmeasured' | null;

export function TurnFooter({ job, attemptNo }: { job: AgentJob; attemptNo?: number }) {
  const [open, setOpen] = useState(false);
  // Which chip the reader asked about. A native `title` was the first
  // delivery for these, and it is the wrong one: it wants a second of
  // motionless hover, cannot be read on a touch screen, and holds three
  // sentences badly. The one thing this footer most needs to explain — why a
  // turn forgot — was the least likely to be seen.
  const [explained, setExplained] = useState<Explained>(null);
  // Scoped to the attempt the reader is looking at: the body above already
  // switches with the attempt picker, and a footer that did not would label
  // one attempt's transcript with another's numbers.
  const timings = useMemo(() => toolTimings(job.events, attemptNo), [job.events, attemptNo]);
  const badge = job.continuityMode ? CONTINUITY_LABEL[job.continuityMode] : undefined;
  const duration = formatDuration(job.durationMs);
  const resources = job.resources;
  const stats = resources ? resourceStats(resources) : [];
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
      : explained === 'partial'
        ? `Only ${Math.round((resources?.coverage ?? 0) * 100)}% of this turn was sampled, so the ` +
          'numbers above are lower bounds rather than totals. This happens when the sandbox ' +
          'took a while to appear, or ended before the turn did.'
        : explained === 'unmeasured'
          ? 'This turn ran where the platform could not read the sandbox\u2019s counters, so ' +
            'nothing was measured. That is not the same as a turn that consumed nothing.'
          : null;
  const unmeasured = Boolean(resources && stats.length === 0);

  // `unmeasured` counts as something to say: a sampler that ran and could
  // read nothing is a different fact from a turn nobody tried to measure, and
  // only the first of the two has anything to tell the reader.
  if (!badge && !duration && stats.length === 0 && timings.length === 0 && !unmeasured) {
    return null;
  }

  return (
    <section className="mt-3 rounded-lg border border-gray-200 bg-gray-50/60 px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-gray-600">
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
        {duration ? <span className="tabular-nums">{duration}</span> : null}
        {stats.map((stat) => (
          <span key={stat.label} className="tabular-nums">
            <span className="text-gray-500">{stat.label} </span>
            {stat.value}
          </span>
        ))}
        {partial ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'partial' ? null : 'partial')}
            aria-expanded={explained === 'partial'}
            className="text-amber-700 underline-offset-2 hover:underline"
          >
            partial
          </button>
        ) : null}
        {unmeasured ? (
          <button
            type="button"
            onClick={() => setExplained(explained === 'unmeasured' ? null : 'unmeasured')}
            aria-expanded={explained === 'unmeasured'}
            className="text-gray-400 underline-offset-2 hover:underline"
          >
            resource data unavailable
          </button>
        ) : null}
        {timings.length > 0 ? (
          <button
            type="button"
            onClick={() => setOpen((value) => !value)}
            className="ml-auto text-gray-500 underline-offset-2 hover:text-gray-800 hover:underline"
          >
            {open ? 'Hide' : `${timings.length} tool ${timings.length === 1 ? 'call' : 'calls'}`}
          </button>
        ) : null}
      </div>
      {explanation ? (
        <p className="mt-2 border-t border-gray-200 pt-2 text-[12px] leading-relaxed text-gray-600">
          {explanation}
        </p>
      ) : null}
      {open && timings.length > 0 ? (
        <div className="mt-2 border-t border-gray-200 pt-2">
          <ToolTimingTable timings={timings} />
          <p className="mt-2 text-[11px] text-gray-500">
            Durations are measured from when the model requested each call to when its result
            arrived, so they include the model’s own thinking time. Per-call execution timing
            arrives with the next step of this work.
          </p>
        </div>
      ) : null}
    </section>
  );
}
