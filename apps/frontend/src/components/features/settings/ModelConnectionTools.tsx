'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import {
  discoverConnectionModels,
  updateModelConnection,
  type ModelConnection,
} from '@/lib/api/modelConnections';

const buttonClass = 'ui-button ui-button-secondary min-h-11';

/** Discovering model IDs from the provider's list; a convenience, never a check. */
export function ModelConnectionTools({
  connection,
  available,
  busy,
  run,
  onChange,
}: {
  connection: ModelConnection;
  available: boolean;
  busy: boolean;
  run: (action: () => Promise<void>) => Promise<void>;
  onChange: (connection: ModelConnection) => void;
}) {
  const t = useT();
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);

  return (
    <>
      <details className="mt-2">
        <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
          {t('connections.discover')}
        </summary>
        <p className="my-3 text-xs leading-5 text-muted">{t('connections.discoverHint')}</p>
        <button
          type="button"
          className={buttonClass}
          disabled={busy || !available || !connection.enabled}
          onClick={() =>
            void run(async () => {
              setDiscovered(await discoverConnectionModels(connection.id));
              setSelected([]);
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
                  {value}
                </label>
              ))}
            </div>
            <button
              type="button"
              className={buttonClass}
              disabled={busy || !selected.length || connection.models.length + selected.length > 64}
              onClick={() =>
                void run(async () =>
                  onChange(
                    await updateModelConnection(connection.id, {
                      models: [...connection.models, ...selected],
                    }),
                  ),
                )
              }
            >
              {t('connections.addModels')}
            </button>
          </div>
        ) : null}
      </details>
    </>
  );
}
