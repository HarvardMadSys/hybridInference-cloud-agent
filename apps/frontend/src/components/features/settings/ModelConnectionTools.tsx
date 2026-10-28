'use client';

import { useId, useState } from 'react';
import { useT } from '@/components/providers';
import { Select } from '@/components/ui/Select';
import {
  checkModelConnection,
  checkTargets,
  discoverConnectionModels,
  updateModelConnection,
  type ModelConnection,
  type ModelConnectionCheck,
  protocolName,
  type ModelProtocol,
} from '@/lib/api/modelConnections';

const buttonClass = 'ui-button ui-button-secondary min-h-11';
const selectClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

function CheckResult({ result }: { result: ModelConnectionCheck }) {
  const t = useT();
  return (
    <div role="status" className="mt-3 space-y-1 rounded-lg border border-border p-3 text-sm">
      <p className="font-medium">
        {t(result.status === 'supported' ? 'connections.checkPassed' : 'connections.checkFailed')}
      </p>
      <p>{result.message}</p>
      <p className="break-words text-xs text-muted">
        {t('connections.checkScope', {
          model: result.model,
          protocol:
            result.mode === 'translated'
              ? t('connections.checkTranslated', { protocol: protocolName(result.protocol) })
              : protocolName(result.protocol),
        })}
      </p>
      <p className="text-xs text-muted">
        {t('connections.checkDetails', {
          streaming: t(result.streaming ? 'connections.yes' : 'connections.no'),
          tool: t(result.tool_call ? 'connections.yes' : 'connections.no'),
          final: t(result.final_answer ? 'connections.yes' : 'connections.no'),
        })}
      </p>
      {result.recorded === false ? (
        <p className="text-status-warning">{t('connections.checkStale')}</p>
      ) : null}
    </div>
  );
}

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
  const fieldId = useId();
  const [model, setModel] = useState(connection.models[0] ?? '');
  const [protocol, setProtocol] = useState<ModelProtocol>(
    connection.protocols[0] ?? 'chat_completions',
  );
  const [result, setResult] = useState(connection.last_check);
  const [discovered, setDiscovered] = useState<string[] | null>(null);
  const [selected, setSelected] = useState<string[]>([]);

  return (
    <>
      <details className="mt-3">
        <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
          {t('connections.check')}
        </summary>
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          <div className="text-sm">
            {/* Associated by id rather than wrapped, so the label's text is
                its name alone and not the chosen value inside it. */}
            <label htmlFor={`${fieldId}-model`}>{t('connections.checkModel')}</label>
            <Select
              id={`${fieldId}-model`}
              className={selectClass}
              value={model}
              onValueChange={setModel}
              disabled={busy}
              options={connection.models.map((value) => ({ value, label: value }))}
            />
          </div>
          <div className="text-sm">
            <label htmlFor={`${fieldId}-protocol`}>{t('connections.checkProtocol')}</label>
            <Select
              id={`${fieldId}-protocol`}
              className={selectClass}
              value={protocol}
              onValueChange={setProtocol}
              disabled={busy}
              options={checkTargets(connection.protocols).map(
                ({ protocol: value, translated }) => ({
                  value,
                  label: translated
                    ? t('connections.checkTranslated', { protocol: protocolName(value) })
                    : protocolName(value),
                }),
              )}
            />
          </div>
        </div>
        <p className="my-3 text-xs leading-5 text-muted">{t('connections.checkHint')}</p>
        <button
          type="button"
          className={buttonClass}
          disabled={busy || !available || !connection.enabled || !model}
          onClick={() =>
            void run(async () =>
              setResult(await checkModelConnection(connection.id, model, protocol)),
            )
          }
        >
          {t(busy ? 'connections.busy' : 'connections.check')}
        </button>
      </details>
      {result ? <CheckResult result={result} /> : null}
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
