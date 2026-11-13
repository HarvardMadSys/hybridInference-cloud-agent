'use client';

import { useT } from '@/components/providers';
import { connectionAgentNames } from './modelConnectionDisplay';
import type { EffortFact, ModelStatus } from '@/lib/api/modelConnections';

/**
 * What the provider's listings say about a model's reasoning levels, in one
 * line: the levels any of its APIs declares, else that it declares none, else
 * that it does not say. Which API declared what is the platform's concern; the
 * composer offers each agent only what its own API declared.
 */
export function effortSummary(efforts: ModelStatus['efforts']): {
  key: 'connections.effortLevels' | 'connections.effortNone' | 'connections.effortUnknown';
  levels: string;
} {
  const facts = Object.values(efforts ?? {}).filter(Boolean) as EffortFact[];
  const levels: string[] = [];
  for (const fact of facts) {
    if (fact.status !== 'supported') continue;
    for (const level of fact.levels) if (!levels.includes(level)) levels.push(level);
  }
  if (levels.length) return { key: 'connections.effortLevels', levels: levels.join(', ') };
  if (facts.length && facts.every((fact) => fact.status === 'unsupported')) {
    return { key: 'connections.effortNone', levels: '' };
  }
  return { key: 'connections.effortUnknown', levels: '' };
}

/**
 * Each saved model, the agents it works with and the reasoning levels its
 * provider declares. Every saved model was verified before it was saved; which
 * APIs the provider offers, and how each agent reaches it, are the platform's
 * concern and are not shown here.
 */
export function ModelStatusList({ statuses }: { statuses: ModelStatus[] }) {
  const t = useT();
  return (
    <ul className="mt-3 divide-y divide-border">
      {statuses.map((status) => {
        const effort = effortSummary(status.efforts);
        return (
          <li key={status.model} className="py-2">
            <p className="break-all font-mono text-[13px]">{status.model}</p>
            <p
              className={`text-xs ${status.runtimes.length ? 'text-muted' : 'text-status-warning'}`}
            >
              {status.runtimes.length
                ? t('connections.agents', { agents: connectionAgentNames(status.runtimes) })
                : t('connections.notUsable')}
            </p>
            {status.runtimes.length ? (
              <p className="text-xs text-muted">{t(effort.key, { levels: effort.levels })}</p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}
