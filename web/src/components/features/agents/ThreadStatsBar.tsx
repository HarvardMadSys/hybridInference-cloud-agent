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
  const [stats, setStats] = useState<AgentThreadStatsApi | null>(null);

  // Reloads when the turn ends: `job.state` changes on the reload that the
  // finished-stream handler triggers, which is the same signal the workspace
  // panels refresh on.
  useEffect(() => {
    let cancelled = false;
    getAgentThreadStats(job.id)
      .then((next) => {
        if (!cancelled) setStats(next);
      })
      // A summary that cannot load is not worth an error banner over the
      // composer; the bar simply stays absent.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [job.id, job.state]);

  if (!stats) return null;
  const segments = statSegments(stats);
  if (segments.length === 0) return null;

  return (
    <div
      className="mt-2 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 px-2 text-[11px] text-gray-400 dark:text-gray-500"
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
