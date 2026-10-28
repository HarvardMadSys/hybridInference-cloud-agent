'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import {
  discoverConnectionModels,
  refusedModels,
  updateModelConnection,
  type ModelConnection,
} from '@/lib/api/modelConnections';

const buttonClass = 'ui-button ui-button-secondary min-h-11';

/**
 * Discovering model IDs from the provider's list, a convenience. Adding the
 * picked IDs is an edit like any other: they are verified before they are
 * saved, and a refusal leaves the connection as it was.
 */
export function ModelConnectionTools({
  connection,
  available,
  maxModels,
  busy,
  run,
  onChange,
}: {
  connection: ModelConnection;
  available: boolean;
  maxModels: number;
  busy: boolean;
  run: (action: () => Promise<void>) => Promise<void>;
  onChange: (connection: ModelConnection) => void;
}) {
  const t = useT();
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [refused, setRefused] = useState<string[]>([]);

  async function addSelected() {
    setRefused([]);
    try {
      onChange(
        await updateModelConnection(connection.id, {
          models: [...connection.models, ...selected],
        }),
      );
    } catch (cause) {
      const models = refusedModels(cause);
      if (!models) throw cause;
      setRefused(
        models
          .filter((status) => !status.runtimes.length)
          .map((status) =>
            t('connections.modelRefusedNamed', {
              model: status.model,
              reason: status.message || t('connections.error'),
            }),
          ),
      );
    }
  }

  return (
    <details className="mt-1">
      <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
        {t('connections.discover')}
      </summary>
      <p className="my-2 text-xs leading-5 text-muted">{t('connections.discoverHint')}</p>
      <button
        type="button"
        className={buttonClass}
        disabled={busy || !available || !connection.enabled}
        onClick={() =>
          void run(async () => {
            setDiscovered(await discoverConnectionModels(connection.id));
            setSelected([]);
            setRefused([]);
          })
        }
      >
        {t(busy ? 'connections.busy' : 'connections.discover')}
      </button>
      {discovered?.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t('connections.discoverEmpty')}</p>
      ) : null}
      {discovered?.length ? (
        <div className="mt-3 space-y-3">
          <div className="max-h-60 space-y-2 overflow-y-auto">
            {discovered.map((value) => (
              <label key={value} className="flex min-h-9 items-center gap-2 break-all text-sm">
                <input
                  type="checkbox"
                  disabled={busy || connection.models.includes(value)}
                  checked={connection.models.includes(value) || selected.includes(value)}
                  onChange={(event) =>
                    setSelected((current) =>
                      event.target.checked
                        ? [...current, value]
                        : current.filter((item) => item !== value),
                    )
                  }
                />
                <span className="font-mono text-[13px]">{value}</span>
              </label>
            ))}
          </div>
          <button
            type="button"
            className={buttonClass}
            disabled={
              busy ||
              !available ||
              !selected.length ||
              connection.models.length + selected.length > maxModels
            }
            onClick={() => void run(addSelected)}
          >
            {t(busy ? 'connections.verifyingShort' : 'connections.addModels')}
          </button>
          <p className="text-xs leading-5 text-muted">{t('connections.billing')}</p>
          {refused.length ? (
            <div role="alert" className="space-y-1 text-sm text-status-warning">
              <p>{t('connections.refused')}</p>
              {refused.map((line) => (
                <p key={line}>{line}</p>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}
