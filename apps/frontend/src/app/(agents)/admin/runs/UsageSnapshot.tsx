import type { AgentJobUsageApi } from '@/lib/api/agents';

import { CARD, metric, money, number } from './shared';

/** Pure presentation: admin reads never invoke owner loaders or refresh accounting. */
export function UsageSnapshot({ usage }: { usage: AgentJobUsageApi }) {
  const { ledger } = usage;
  const totals = ledger.totals;
  const unknownRows = ledger.unknown_rows;
  const cost = money(totals?.spent_usd);
  const rows: { label: string; gateway: string; title?: string }[] = [
    { label: 'Model calls', gateway: number(totals?.calls) },
    {
      label: 'Input tokens (including cache)',
      gateway: metric(totals?.tokens_in, unknownRows?.tokens_in),
    },
    {
      label: 'Output tokens',
      gateway: metric(totals?.tokens_out, unknownRows?.tokens_out),
    },
    {
      label: 'Cache read tokens',
      gateway: metric(totals?.cache_read, unknownRows?.cache_read),
    },
    {
      label: 'Cache write tokens',
      gateway: metric(totals?.cache_write, unknownRows?.cache_write),
    },
    {
      label: 'Reasoning tokens',
      gateway: metric(totals?.reasoning, unknownRows?.reasoning),
    },
    {
      label: 'Cost (USD)',
      gateway:
        cost.exact && unknownRows?.spent_usd
          ? `${cost.display} (${number(unknownRows.spent_usd)} unknown)`
          : cost.display,
      title: cost.exact ?? undefined,
    },
  ];

  return (
    <section aria-labelledby="token-usage-heading" className="mt-8">
      <h2
        id="token-usage-heading"
        className="text-lg font-semibold text-gray-900 dark:text-gray-50"
      >
        Token usage
      </h2>
      <div className={`${CARD} mt-4 overflow-hidden`}>
        {(ledger.missing_attempts > 0 || ledger.estimated_calls > 0) && (
          <div className="flex flex-wrap gap-x-5 gap-y-2 border-b border-gray-100 px-5 py-4 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
            {ledger.missing_attempts > 0 && (
              <span className="text-amber-700 dark:text-amber-400">
                Gateway totals are missing for part of this turn.
              </span>
            )}
            {ledger.estimated_calls > 0 && (
              <span>{number(ledger.estimated_calls)} calls contain estimated usage.</span>
            )}
          </div>
        )}
        <table aria-label="Token usage totals" className="w-full text-left text-sm">
          <tbody className="divide-y divide-gray-100 dark:divide-white/5">
            {rows.map((row) => (
              <tr key={row.label}>
                <th
                  scope="row"
                  className="px-5 py-2.5 font-normal text-gray-600 dark:text-gray-400"
                >
                  {row.label}
                </th>
                <td className="px-5 py-2.5 text-right tabular-nums" title={row.title}>
                  {row.gateway}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
