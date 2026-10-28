'use client';

import { useId, useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers';
import {
  createModelConnection,
  MODEL_PROTOCOLS,
  updateModelConnection,
  type ModelConnection,
  type ModelProtocol,
} from '@/lib/api/modelConnections';
import { connectionChanges, newConnection, parseModels } from './modelConnectionChanges';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

/** Protocol names, shown only inside the advanced override. */
const PROTOCOL_LABELS = {
  chat_completions: 'connections.chat',
  responses: 'connections.responses',
  anthropic_messages: 'connections.anthropic',
} as const satisfies Record<ModelProtocol, string>;

/**
 * The advanced override, collapsed by default: automatic detection, or a
 * manual declaration that every listed model runs on without verification.
 */
function ApiDetection({
  manual,
  protocols,
  onManual,
  onChange,
}: {
  manual: boolean;
  protocols: ModelProtocol[];
  onManual: (manual: boolean) => void;
  onChange: (protocols: ModelProtocol[]) => void;
}) {
  const t = useT();
  const name = useId();
  return (
    <details className="rounded-lg border border-border px-3" open={manual}>
      <summary className="ui-focus-ring cursor-pointer rounded-md py-2 text-sm font-medium">
        {t('connections.advanced')}
      </summary>
      <fieldset className="space-y-2 pb-3">
        <legend className="mb-2 text-sm font-medium">{t('connections.mode')}</legend>
        <label className="flex min-h-9 items-center gap-2 text-sm">
          <input
            type="radio"
            name={name}
            checked={!manual}
            onChange={() => {
              onManual(false);
              onChange([]);
            }}
          />
          {t('connections.modeAuto')}
        </label>
        <label className="flex min-h-9 items-center gap-2 text-sm">
          <input type="radio" name={name} checked={manual} onChange={() => onManual(true)} />
          {t('connections.modeManual')}
        </label>
        {manual ? (
          <div className="space-y-1 ps-6">
            {MODEL_PROTOCOLS.map((protocol) => (
              <label key={protocol} className="flex min-h-9 items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={protocols.includes(protocol)}
                  onChange={() =>
                    onChange(
                      MODEL_PROTOCOLS.filter((value) =>
                        value === protocol ? !protocols.includes(value) : protocols.includes(value),
                      ),
                    )
                  }
                />
                {t(PROTOCOL_LABELS[protocol])}
              </label>
            ))}
            <p className="text-xs leading-5 text-muted">{t('connections.modeManualHint')}</p>
          </div>
        ) : null}
      </fieldset>
    </details>
  );
}

/** Whether Save must wait: a field is missing, or a manual declaration is empty. */
function isIncomplete(fields: {
  name: string;
  models: string;
  key: string;
  needsKey: boolean;
  manual: boolean;
  protocols: ModelProtocol[];
}): boolean {
  if (!fields.name.trim() || !fields.models.trim()) return true;
  if (fields.needsKey && !fields.key.trim()) return true;
  // A manual declaration with nothing declared would silently mean automatic.
  return fields.manual && fields.protocols.length === 0;
}

/** "Save and verify", "Save" and "Cancel". Only the first leads to provider calls. */
function FormActions({
  busy,
  incomplete,
  verifyAfter,
  onIntent,
  onCancel,
}: {
  busy: boolean;
  incomplete: boolean;
  verifyAfter: boolean;
  onIntent: (verify: boolean) => void;
  onCancel: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap gap-2">
      <button
        type="submit"
        disabled={busy || incomplete}
        onClick={() => onIntent(true)}
        className="ui-button ui-button-primary min-h-11"
      >
        {t(busy && verifyAfter ? 'connections.busy' : 'connections.saveVerify')}
      </button>
      <button
        type="submit"
        disabled={busy || incomplete}
        onClick={() => onIntent(false)}
        className="ui-button ui-button-secondary min-h-11"
      >
        {t(busy && !verifyAfter ? 'connections.busy' : 'connections.save')}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="ui-button ui-button-secondary min-h-11"
      >
        {t('connections.cancel')}
      </button>
    </div>
  );
}

/**
 * Add or edit a connection: a name, a Base URL, a key and model IDs. Saving
 * never calls the provider; "Save and verify" saves, then asks the card to
 * verify its unverified models (`onSaved`'s `verify`).
 */
export function ModelConnectionForm({
  connection,
  onSaved,
  onCancel,
}: {
  connection?: ModelConnection;
  onSaved: (connection: ModelConnection, options: { verify: boolean }) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const id = useId();
  const [name, setName] = useState(connection?.name ?? '');
  const [url, setUrl] = useState(connection?.base_url ?? '');
  const [key, setKey] = useState('');
  const [models, setModels] = useState(connection?.models.join('\n') ?? '');
  const [protocols, setProtocols] = useState<ModelProtocol[]>(connection?.manual_protocols ?? []);
  const [manual, setManual] = useState(protocols.length > 0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [verifyAfter, setVerifyAfter] = useState(false);
  // Which button submitted: the submit handler runs in the same event as the
  // click, before a state update from it would be visible.
  const verifyIntent = useRef(false);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(false);
    const fields = {
      name: name.trim(),
      base_url: url.trim(),
      models: parseModels(models),
      key: key.trim(),
      protocols,
    };
    try {
      let saved = connection;
      if (!connection) {
        saved = await createModelConnection(newConnection(fields));
      } else {
        const changes = connectionChanges(connection, fields);
        if (Object.keys(changes).length)
          saved = await updateModelConnection(connection.id, changes);
      }
      setKey('');
      if (saved) onSaved(saved, { verify: verifyIntent.current });
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  const incomplete = isIncomplete({ name, models, key, needsKey: !connection, manual, protocols });
  return (
    <form
      onSubmit={(event) => void save(event)}
      className="mt-4 space-y-4 rounded-xl border border-border p-4"
    >
      <fieldset disabled={busy} className="space-y-4">
        <legend className="sr-only">
          {t(connection ? 'connections.edit' : 'connections.add')}
        </legend>
        <label className="block text-sm font-medium">
          {t('connections.name')}
          <input
            className={inputClass}
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={64}
            autoComplete="off"
          />
        </label>
        <label className="block text-sm font-medium">
          {t('connections.url')}
          <input
            className={inputClass}
            type="url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            required
            maxLength={512}
            placeholder="https://api.example.com/v1"
            aria-describedby={`${id}-url`}
            autoComplete="off"
          />
        </label>
        <p id={`${id}-url`} className="text-xs leading-5 text-muted">
          {t('connections.urlHint')}
        </p>
        <label className="block text-sm font-medium">
          {t('connections.key')}
          <input
            className={inputClass}
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            required={!connection}
            maxLength={4096}
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby={`${id}-key`}
          />
        </label>
        <p id={`${id}-key`} className="text-xs leading-5 text-muted">
          {connection ? `${t('connections.keyHint')} ` : ''}
          {t('connections.secretHint')}
        </p>
        <label className="block text-sm font-medium">
          {t('connections.models')}
          <textarea
            className={inputClass}
            value={models}
            onChange={(event) => setModels(event.target.value)}
            required
            rows={4}
            spellCheck={false}
            aria-describedby={`${id}-models`}
          />
        </label>
        <p id={`${id}-models`} className="text-xs leading-5 text-muted">
          {t('connections.modelsHint')}
        </p>
        <ApiDetection
          manual={manual}
          protocols={protocols}
          onManual={setManual}
          onChange={setProtocols}
        />
        {connection ? (
          <p className="text-sm leading-6 text-muted">{t('connections.editHint')}</p>
        ) : null}
        <p className="text-xs leading-5 text-muted">{t('connections.verifyHint')}</p>
      </fieldset>
      {error ? (
        <p role="alert" className="text-sm text-status-error">
          {t('connections.error')}
        </p>
      ) : null}
      <FormActions
        busy={busy}
        incomplete={incomplete}
        verifyAfter={verifyAfter}
        onIntent={(verify) => {
          verifyIntent.current = verify;
          setVerifyAfter(verify);
        }}
        onCancel={onCancel}
      />
    </form>
  );
}
