'use client';

// The whole conversation's account, in one line under the composer.
//
// Its sibling `TurnFooter` answers "how did *this turn* go"; this answers
// "what has this thread cost so far", which is the question someone asks
// before sending another message. Both obey the same two rules from the
// observability design's §11:
//
//   1. A measure we do not have is a dash, never a zero. A runtime that
//      reports no token counts is not a runtime that used no tokens, and the
//      bar's title says which it is.
//   2. Derived timings say they are derived. Tool time here is the interval
//      between a tool being requested and its result arriving, which includes
//      the model's own thinking; the split is honest about that rather than
//      presenting itself as execution time.
//
// Every number comes from events the platform already stored, so a thread
// that ran months ago fills this bar in as completely as one running now.

import { useEffect, useState } from 'react';

import { formatDuration } from './TurnFooter';
import { isActiveConversationState, isActiveJobState } from './conversationJob';
import type { AgentJob } from './types';
import { getAgentThreadStats, type AgentThreadStatsApi } from '@/lib/api/agents';

/** Compact count: 5_800_000 → "5.8M", 89_700 → "89.7k". */
export function formatCount(value: number | null | undefined): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return null;
  if (value < 1000) return String(Math.round(value));
  const units: Array<[number, string]> = [
    [1e9, 'B'],
    [1e6, 'M'],
    [1e3, 'k'],
  ];
  for (const [scale, suffix] of units) {
    if (value >= scale) {
      const scaled = value / scale;
      // One decimal below 100 is where it still distinguishes 5.8M from 5.2M;
      // above that the tenth is noise in a one-line summary.
      return `${scaled >= 100 ? Math.round(scaled) : Number(scaled.toFixed(1))}${suffix}`;
    }
  }
  return String(Math.round(value));
}

/** A count with its noun, pluralized: "1 turn", "3 turns". */
function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

interface Segment {
  key: string;
  text: string;
  title: string;
}

/**
 * Build the bar's segments, dropping any whose numbers are unavailable.
 *
 * Exported for the tests, which assert on the segments rather than on
 * rendered text: what matters is which facts a given thread can state, and
 * that an unavailable one is absent rather than zero.
 */
export function statSegments(stats: AgentThreadStatsApi): Segment[] {
  const segments: Segment[] = [
    {
      key: 'shape',
      text: `${plural(stats.turns, 'turn')} · ${plural(stats.tool_calls, 'tool call')}`,
      title:
        stats.failed_tool_calls > 0
          ? `${plural(stats.failed_tool_calls, 'tool call')} returned an error.`
          : 'Turns you sent, and tool calls the agent made across all of them.',
    },
  ];

  const modelTime = formatDuration(stats.model_seconds * 1000);
  const toolTime = formatDuration(stats.tool_seconds * 1000);
  if (modelTime && toolTime) {
    segments.push({
      key: 'time',
      text: `Model ${modelTime} · Tools ${toolTime}`,
      title:
        'Split from event timestamps: tool time runs from a call being requested to its ' +
        'result arriving, so it includes the model’s thinking before the tool ran. ' +
        'Overlapping calls are counted once.',
    });
  }

  const rate = stats.output_tokens_per_second;
  const ttft = stats.ttft_seconds;
  const speed: string[] = [];
  if (typeof ttft === 'number') speed.push(`TTFT ${formatDuration(ttft * 1000)}`);
  if (typeof rate === 'number') speed.push(`${Math.round(rate)} tok/s`);
  if (speed.length > 0) {
    segments.push({
      key: 'speed',
      text: speed.join(' · '),
      title:
        'Mean time from a turn starting to its first output, and output tokens per ' +
        'second of model time.',
    });
  }

  if (typeof stats.cache_hit_ratio === 'number') {
    segments.push({
      key: 'cache',
      text: `Cache hit ${Math.round(stats.cache_hit_ratio * 100)}%`,
      title: 'Share of input tokens served from the provider’s prompt cache.',
    });
  }

  const input = formatCount(stats.input_tokens);
  const output = formatCount(stats.output_tokens);
  if (input || output) {
    segments.push({
      key: 'tokens',
      text: [input && `Input ${input} tok`, output && `Output ${output} tok`]
        .filter(Boolean)
        .join(' · '),
      title: 'Tokens this thread has sent and received, summed over every turn.',
    });
  } else if (stats.tokens_source === 'unavailable' && stats.turns > 0) {
    segments.push({
      key: 'tokens',
      text: 'Tokens —',
      title: 'This thread’s harness does not report token counts, so none are shown.',
    });
  }

  return segments;
}

export function ThreadStatsBar({ job }: { job: AgentJob }) {
  const [snapshot, setSnapshot] = useState<{
    jobId: string;
    stats: AgentThreadStatsApi | null;
  } | null>(null);
  const active = isActiveJobState(job.state) || isActiveConversationState(job.conversationState);

  // The aggregate endpoint includes the running turn, but streaming events
  // do not change job.state. Refresh all totals together while work is active,
  // including a follow-up running while an older turn is selected.
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || controller.signal.aborted) return;
      inFlight = true;
      try {
        const stats = await getAgentThreadStats(job.id, controller.signal);
        if (!controller.signal.aborted) setSnapshot({ jobId: job.id, stats });
      } catch {
        // Keep the last snapshot through a transient failure and retry on the
        // next tick. An unavailable summary does not need a composer banner.
      } finally {
        inFlight = false;
      }
    };
    const refreshVisible = () => {
      if (!document.hidden) void refresh();
    };

    // State transitions also fetch immediately, including the final totals.
    void refresh();
    const timer = active ? setInterval(refreshVisible, 2_000) : undefined;
    if (active) document.addEventListener('visibilitychange', refreshVisible);
    return () => {
      controller.abort();
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refreshVisible);
    };
  }, [job.id, job.state, job.conversationState, active]);

  // A delayed response or failed load for another conversation must never
  // leave that conversation displaying the previous one's totals.
  const stats = snapshot?.jobId === job.id ? snapshot.stats : null;
  if (!stats) return null;
  const segments = statSegments(stats);
  if (segments.length === 0) return null;

  return (
    <div
      className="mt-1.5 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 px-2 text-[11px] text-gray-400 dark:text-gray-500"
      aria-label="Conversation totals"
    >
      {segments.map((segment, index) => (
        <span key={segment.key} className="flex items-center gap-2">
          {index > 0 ? (
            <span aria-hidden="true" className="text-gray-300 dark:text-gray-600">
              |
            </span>
          ) : null}
          <span title={segment.title}>{segment.text}</span>
        </span>
      ))}
    </div>
  );
}
