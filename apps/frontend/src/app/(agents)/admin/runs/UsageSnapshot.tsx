import type { AgentJobUsageApi } from '@/lib/api/agents';

import { CARD, number } from './shared';

/** Pure presentation: what the turn's agent reported, and who bills it. */
export function UsageSnapshot({ usage }: { usage: AgentJobUsageApi }) {
  const { runtime } = usage;
  const rows = [
    { label: 'Input tokens', value: number(runtime.input_tokens) },
    { label: 'Output tokens', value: number(runtime.output_tokens) },
    { label: 'Cache read tokens', value: number(runtime.cache_read_tokens) },
    { label: 'Cost (USD)', value: "Billed by the owner's provider" },
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
                <td className="px-5 py-2.5 text-right tabular-nums">{row.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
