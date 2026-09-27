import { formatDuration } from '@/components/features/agents/TurnFooter';
import type { AdminSessionStatistics } from '@/lib/api/adminRuns';

import { CARD, number } from './shared';

function duration(seconds: number | null | undefined): string {
  return formatDuration(typeof seconds === 'number' ? seconds * 1000 : null) ?? 'Unavailable';
}

/** Run-scoped presentation only; selecting an execution must not change these totals. */
export function SessionStatistics({ statistics }: { statistics?: AdminSessionStatistics | null }) {
  const rate = statistics?.output_tokens_per_second;
  const rows = [
    {
      label: 'LLM time',
      value: duration(statistics?.model_seconds),
      description:
        'Estimated execution time outside tool intervals, including agent harness overhead.',
    },
    {
      label: 'Tool time',
      value: duration(statistics?.tool_seconds),
      description:
        'Estimated from recorded tool calls and results. Overlapping intervals are counted once per execution.',
    },
    {
      label: 'Avg time to first token (TTFT)',
      value: duration(statistics?.ttft_seconds),
      description:
        'Average time from an execution starting to its first recorded output event, across executions with a recorded sample; includes startup overhead.',
    },
    {
      label: 'Tokens per second (TPS)',
      value:
        typeof rate === 'number' && Number.isFinite(rate) && rate >= 0
          ? `${number(rate)} tok/s`
          : 'Unavailable',
      description:
        'Runtime-reported output tokens divided by estimated LLM time, using executions with both measurements.',
    },
  ];

  return (
    <section aria-labelledby="turn-statistics-heading" className="mt-8">
      <h2
        id="turn-statistics-heading"
        className="text-lg font-semibold text-gray-900 dark:text-gray-50"
      >
        Turn statistics
      </h2>
      <dl
        className={`${CARD} mt-4 divide-y divide-gray-100 overflow-hidden text-sm dark:divide-white/5`}
      >
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-5 px-5 py-3">
            <dt className="text-gray-600 dark:text-gray-400" title={row.description}>
              {row.label}
            </dt>
            <dd className="shrink-0 tabular-nums text-gray-900 dark:text-gray-100">{row.value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}
