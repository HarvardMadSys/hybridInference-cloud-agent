import type { AgentJobUsageApi } from '@/lib/api/agents';

import { CARD, dateTime, number } from './shared';

/** Pure presentation: admin reads never invoke owner loaders or refresh accounting. */
export function UsageSnapshot({ usage }: { usage: AgentJobUsageApi }) {
  const { ledger, runtime } = usage;
  const totals = ledger.totals;
  const rows = [
    { label: 'Model calls', gateway: number(totals?.calls), runtime: 'Unavailable' },
    {
      label: 'Input tokens (including cache)',
      gateway: number(totals?.tokens_in),
      runtime: number(runtime.normalized_input_tokens),
    },
    {
      label: 'Output tokens',
      gateway: number(totals?.tokens_out),
      runtime: number(runtime.output_tokens),
    },
    {
      label: 'Cache read tokens',
      gateway: number(totals?.cache_read),
      runtime: number(runtime.cache_read_tokens),
    },
    { label: 'Cache write tokens', gateway: number(totals?.cache_write), runtime: 'Unavailable' },
    { label: 'Reasoning tokens', gateway: number(totals?.reasoning), runtime: 'Unavailable' },
    {
      label: 'Cost (USD)',
      gateway: totals?.spent_usd != null ? `$${totals.spent_usd}` : 'Unavailable',
      runtime: 'Unavailable',
    },
  ];
  const unknown = Object.entries(ledger.unknown_rows ?? {}).filter(([, count]) => count > 0);

  return (
    <section aria-labelledby="usage-heading" className="mt-8">
      <h2 id="usage-heading" className="text-lg font-semibold text-gray-900 dark:text-gray-50">
        Usage
      </h2>
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Totals for this run across all its executions, independent of the execution selected above.
      </p>
      <div className={`${CARD} mt-4 overflow-hidden`}>
        <div className="flex flex-wrap gap-x-5 gap-y-2 border-b border-gray-100 px-5 py-4 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
          <span>
            Gateway: <strong className="font-medium">{ledger.status}</strong>
          </span>
          <span>Snapshot: {dateTime(ledger.queried_at)}</span>
          <span>{ledger.finalized ? 'Finalized' : 'Not finalized'}</span>
          {ledger.refreshing && <span>Accounting update in progress</span>}
        </div>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-gray-500 dark:text-gray-400">
              <tr>
                <th className="px-5 py-3 font-medium">Metric</th>
                <th className="px-5 py-3 font-medium">Gateway ledger</th>
                <th className="px-5 py-3 font-medium">Runtime reported</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {rows.map((row) => (
                <tr key={row.label}>
                  <th
                    scope="row"
                    className="px-5 py-2.5 font-normal text-gray-600 dark:text-gray-400"
                  >
                    {row.label}
                  </th>
                  <td className="px-5 py-2.5 tabular-nums">{row.gateway}</td>
                  <td className="px-5 py-2.5 tabular-nums">{row.runtime}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="space-y-2 border-t border-gray-100 px-5 py-4 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
          <p>
            Gateway totals use each execution’s grant time window. Runtime totals are deduplicated
            reports; activity messages are not model-call counts.
          </p>
          {ledger.missing_attempts > 0 && (
            <p className="text-amber-700 dark:text-amber-400">
              {ledger.missing_attempts} execution(s) have no gateway totals.
            </p>
          )}
          {ledger.estimated_calls > 0 && (
            <p>{number(ledger.estimated_calls)} calls contain estimated usage.</p>
          )}
          {unknown.length > 0 && (
            <p>
              Rows missing metrics: {unknown.map(([key, count]) => `${key}: ${count}`).join(' · ')}
            </p>
          )}
          {ledger.attempts.some((attempt) => attempt.reason) && (
            <p>
              Availability:{' '}
              {Array.from(
                new Set(ledger.attempts.map((attempt) => attempt.reason).filter(Boolean)),
              ).join(', ')}
            </p>
          )}
        </div>
        <details className="border-t border-gray-100 px-5 py-4 text-xs dark:border-white/5">
          <summary className="cursor-pointer font-medium text-gray-600 dark:text-gray-400">
            Attribution windows
          </summary>
          <ul className="mt-3 space-y-3 text-gray-500 dark:text-gray-400">
            {ledger.attempts.map((attempt) => (
              <li key={attempt.attempt_id}>
                <p>
                  Execution {attempt.attempt_no} · {attempt.status}
                </p>
                {attempt.windows.length ? (
                  attempt.windows.map((window, index) => (
                    <p key={index} className="mt-1">
                      {dateTime(window.since)} – {dateTime(window.until)} · request start time
                    </p>
                  ))
                ) : (
                  <p className="mt-1">Attribution window unavailable.</p>
                )}
              </li>
            ))}
          </ul>
        </details>
      </div>
    </section>
  );
}
