'use client';

import { useT } from '@/components/providers';
import {
  protocolName,
  type AgentPath,
  type ModelProtocol,
  type ModelStatus,
} from '@/lib/api/modelConnections';

type Translate = ReturnType<typeof useT>;

function resultKey(status: string | undefined) {
  if (status === 'supported') return 'connections.result.supported' as const;
  if (status === 'failed') return 'connections.result.failed' as const;
  return 'connections.result.unsupported' as const;
}

function pathText(path: AgentPath, t: Translate): string {
  if (path.mode === 'direct') {
    return t('connections.pathDirect', {
      runtime: path.runtime,
      protocol: protocolName(path.upstream_protocol),
    });
  }
  return t('connections.pathTranslated', {
    runtime: path.runtime,
    client: protocolName(path.client_protocol),
    upstream: protocolName(path.upstream_protocol),
  });
}

/** One model's read-only diagnostics: each API's native result and each agent's path. */
function Diagnostics({ status }: { status: ModelStatus }) {
  const t = useT();
  return (
    <ul className="space-y-0.5 text-xs leading-5 text-muted">
      {Object.entries(status.protocols).map(([protocol, result]) => (
        <li key={protocol}>
          {t(resultKey(result?.status), {
            protocol: protocolName(protocol as ModelProtocol),
            message: result?.message ?? '',
          })}
        </li>
      ))}
      {status.paths.map((path) => (
        <li key={path.runtime}>{pathText(path, t)}</li>
      ))}
    </ul>
  );
}

/**
 * Each saved model and the agents it works with. Every saved model was
 * verified before it was saved; how each agent reaches it is in the
 * collapsed technical details, for troubleshooting only.
 */
export function ModelStatusList({ statuses }: { statuses: ModelStatus[] }) {
  const t = useT();
  return (
    <div className="mt-3">
      <ul className="divide-y divide-border">
        {statuses.map((status) => (
          <li key={status.model} className="py-2">
            <p className="break-all font-mono text-[13px]">{status.model}</p>
            <p
              className={`text-xs ${status.runtimes.length ? 'text-muted' : 'text-status-warning'}`}
            >
              {status.runtimes.length
                ? t('connections.agents', { agents: status.runtimes.join(', ') })
                : t('connections.notUsable')}
            </p>
          </li>
        ))}
      </ul>
      <details className="mt-1">
        <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
          {t('connections.details')}
        </summary>
        <div className="space-y-2 pb-1">
          {statuses.map((status) => (
            <div key={status.model}>
              <p className="break-all font-mono text-xs font-medium">{status.model}</p>
              <Diagnostics status={status} />
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}
